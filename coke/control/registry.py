"""提交标识登记表：同一标识并发提交只执行一次。

每次动作提交带一个 ``request_id``。登记表负责三件事：

1. **并发仲裁**：同一标识的多个提交并发到达时，只有第一个成为执行者，
   其余在条件变量上等待执行完成，拿到与执行者完全相同的结果
   （``idempotent=true``），绝不重复落设备。
2. **重放去重**：标识到达终态（completed / failed / rejected）后再提交，
   直接返回登记的终态结果，动作不会再执行第二遍。
3. **重启留存**：终态结果随平台状态一起落到 JSON 文档，重启后同一标识
   重放仍然只执行一次。排队中的提交只活在内存（进程没了排队本就无意义）。

同一标识若携带不同的动作或参数指纹，不是重试而是标识被复用，直接以
:class:`~coke.errors.IdempotencyError` 拒绝，并留审计。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..errors import IdempotencyError
from ..store.jsonfile import JsonStore

LEDGER_DOC = "request_registry"
LEDGER_LIMIT = 256
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")

#: 提交尚未进入终态。
STATE_RUNNING = "running"
#: 排队等状态满足。
STATE_QUEUED = "queued"
#: 被准入策略当场拒绝（终态）。
STATE_REJECTED = "rejected"
#: 动作执行完成（可能 accepted=false，即执行了但中途未成功）（终态）。
STATE_COMPLETED = "completed"
#: 执行阶段抛出异常（终态）。
STATE_FAILED = "failed"

TERMINAL_STATES = frozenset({STATE_REJECTED, STATE_COMPLETED, STATE_FAILED})


def canonical_fingerprint(action: str, params: dict[str, Any]) -> str:
    """计算动作参数的规范指纹，用于识别同一标识下被改动的请求。"""

    payload = {"action": action, "params": {key: params[key] for key in sorted(params)}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_request_id(request_id: str) -> str:
    """校验提交标识格式，非法时抛出 :class:`IdempotencyError`。"""

    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.match(request_id):
        raise IdempotencyError(
            "提交标识必须是 4–128 位字母数字及 . _ : - 组成的非空字符串", component="control"
        )
    return request_id


@dataclass
class _Entry:
    request_id: str
    action: str
    fingerprint: str
    state: str
    result: dict | None = None
    condition: threading.Condition | None = None


class RequestRegistry:
    """提交标识登记表（内存 + 终态落盘）。"""

    def __init__(self, store: JsonStore, ledger_limit: int = LEDGER_LIMIT) -> None:
        self._store = store
        self._limit = max(1, ledger_limit)
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        raw = self._store.read(LEDGER_DOC)
        if not isinstance(raw, dict):
            return
        records = raw.get("records")
        if not isinstance(records, list):
            return
        for item in records:
            if not isinstance(item, dict):
                continue
            request_id = item.get("request_id")
            action = item.get("action")
            fingerprint = item.get("fingerprint")
            result = item.get("result")
            if not (isinstance(request_id, str) and isinstance(action, str)
                    and isinstance(fingerprint, str) and isinstance(result, dict)):
                continue
            self._entries[request_id] = _Entry(
                request_id=request_id,
                action=action,
                fingerprint=fingerprint,
                state=str(result.get("status", STATE_COMPLETED)),
                result=result,
            )

    def begin(self, request_id: str, action: str, fingerprint: str) -> tuple[str, dict | None]:
        """登记一次提交，返回 ``(角色, 已有终态结果)``。

        角色为 ``leader`` 表示本次提交是该标识的第一个提交，需要执行动作；
        ``duplicate`` 表示重复提交，调用方应等待并回传终态结果。终态重放时
        直接在 ``结果`` 里带回登记结果。
        """

        validate_request_id(request_id)
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                # 条件变量复用登记表自身的锁，使 begin/wait/finish 在同一把
                # 锁上同步，重复提交可以安全地等待执行者写入终态。
                self._entries[request_id] = _Entry(
                    request_id, action, fingerprint, STATE_RUNNING,
                    condition=threading.Condition(self._lock),
                )
                return "leader", None
            if entry.action != action or entry.fingerprint != fingerprint:
                raise IdempotencyError(
                    f"提交标识 {request_id} 已用于 {entry.action}，"
                    f"不能再提交不同的 {action} 或不同参数",
                    component="control",
                )
            if entry.state in TERMINAL_STATES:
                return "duplicate", dict(entry.result or {})
            return "duplicate", None

    def mark_queued(self, request_id: str) -> None:
        """把执行者标记为排队等状态满足。"""

        with self._lock:
            entry = self._entries[request_id]
            entry.state = STATE_QUEUED

    def await_terminal(self, request_id: str, timeout: float | None = None) -> dict:
        """重复提交等待该标识进入终态，返回终态结果。"""

        with self._lock:
            entry = self._entries[request_id]
            if entry.state not in TERMINAL_STATES and entry.condition is not None:
                entry.condition.wait(timeout)
            if entry.state not in TERMINAL_STATES or entry.result is None:
                raise IdempotencyError(
                    f"等待提交 {request_id} 完成超时", component="control"
                )
            return dict(entry.result)

    def finish(self, request_id: str, state: str, result: dict) -> dict:
        """记录终态结果，唤醒所有重复提交并落盘。"""

        if state not in TERMINAL_STATES:
            raise ValueError(f"终态必须是 {sorted(TERMINAL_STATES)} 之一")
        with self._lock:
            entry = self._entries[request_id]
            entry.state = state
            entry.result = dict(result)
            if entry.condition is not None:
                entry.condition.notify_all()
            self._persist_locked()
        return dict(result)

    def get(self, request_id: str) -> dict | None:
        """查询某标识的当前登记视图（控制台展示用）。"""

        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return None
            view = {
                "request_id": entry.request_id,
                "action": entry.action,
                "status": entry.state,
            }
            if entry.result is not None:
                view["result"] = entry.result
            return view

    def recent(self, limit: int = 50) -> list[dict]:
        """返回最近的登记视图（终态在前按登记顺序）。"""

        with self._lock:
            entries = list(self._entries.values())[-limit:]
        return [
            {"request_id": item.request_id, "action": item.action, "status": item.state}
            for item in entries
        ]

    def summary(self) -> dict:
        """返回登记表计数，供控制台与 readiness 使用。"""

        with self._lock:
            total = len(self._entries)
            running = sum(1 for item in self._entries.values() if item.state not in TERMINAL_STATES)
        return {"tracked": total, "in_flight": running, "ledger_limit": self._limit}

    def _persist(self) -> None:
        with self._lock:
            self._persist_locked()

    def _persist_locked(self) -> None:
        terminal = [item for item in self._entries.values() if item.state in TERMINAL_STATES]
        trimmed = terminal[-self._limit :]
        records = [
            {
                "request_id": item.request_id,
                "action": item.action,
                "fingerprint": item.fingerprint,
                "result": item.result,
            }
            for item in trimmed
        ]
        self._store.write(LEDGER_DOC, {"records": records})
