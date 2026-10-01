"""命令登记：并发幂等去重与排队。

两个保证写死在这里：

1. **同一标识只执行一次**：控制台连点或网络重发会带着同一个 ``request_id``
   并发到达。:meth:`CommandRegistry.begin` 在平台大锁内"查登记 → 占位 →
   返回"，只有第一个调用拿到执行权；并发重发者阻塞等待终态，拿到与首个
   调用完全相同的结果，动作体不会再跑一遍。同一标识带不同参数视为调用方
   bug，抛 :class:`IdempotencyConflictError`。
2. **状态可回放**：命令终态与当前排队都原子落盘，重启后幂等窗口与队列
   仍然成立（排队命令可继续被控制周期放行）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..errors import IdempotencyConflictError
from ..store.jsonfile import JsonStore

QUEUED = "queued"
RUNNING = "running"
COMPLETE = "complete"
REJECTED = "rejected"
TERMINAL = frozenset({COMPLETE, REJECTED})

RECORDS_DOC = "command_records"
QUEUE_DOC = "command_queue"
KEEP_RECORDS = 1024


@dataclass
class Command:
    """一次动作提交的登记表项。"""

    request_id: str
    action: str
    oven: str
    params: dict
    state: str = QUEUED
    progress: float = 0.0
    reason: dict | None = None
    result: dict | None = None
    waiting_for: str | None = None
    op_id: str = ""
    snapshot: dict | None = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        """可落盘的字段（剔除线程同步原语）。"""

        return {
            "request_id": self.request_id,
            "action": self.action,
            "oven": self.oven,
            "params": self.params,
            "state": self.state,
            "progress": self.progress,
            "reason": self.reason,
            "result": self.result,
            "waiting_for": self.waiting_for,
            "op_id": self.op_id,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Command":
        """从落盘字段还原登记表项。"""

        return cls(
            request_id=str(raw["request_id"]),
            action=str(raw["action"]),
            oven=str(raw.get("oven", "")),
            params=dict(raw.get("params", {})),
            state=str(raw.get("state", QUEUED)),
            progress=float(raw.get("progress", 0.0)),
            reason=raw.get("reason"),
            result=raw.get("result"),
            waiting_for=raw.get("waiting_for"),
            op_id=str(raw.get("op_id", "")),
            snapshot=raw.get("snapshot"),
        )

    def envelope(self, duplicate: bool = False) -> dict:
        """控制台响应外壳：拒绝原因与进度都在这里。"""

        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "op_id": self.op_id,
            "action": self.action,
            "oven": self.oven,
            "state": self.state,
            "ok": self.state == COMPLETE,
            "progress": self.progress,
            "duplicate": duplicate,
        }
        if self.waiting_for:
            payload["waiting_for"] = self.waiting_for
        payload["reason"] = self.reason
        payload["result"] = self.result
        return payload


class CommandRegistry:
    """request_id 幂等登记表 + 排队命令队列（均落盘）。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._records: dict[str, Command] = {}
        self._queue: list[Command] = []
        self._lock = threading.RLock()
        self._restore()

    # ------------------------------------------------------------------ 登记

    def begin(self, request_id: str, action: str, oven: str, params: dict) -> tuple[Command, bool, bool]:
        """登记一个命令的提交（只做幂等去重，不入执行队列）。

        返回 ``(command, acquired, duplicate)``：``acquired`` 为真表示本次
        调用拿到执行权（由调用方决定立即执行、入队等待或拒绝，并
        :meth:`finish`）；为假表示标识已存在，调用方应等待后回显已有终态
        （``duplicate=True``）。排队中的命令被控制周期取走执行时，
        ``acquired`` 同样为真。
        """

        with self._lock:
            existing = self._records.get(request_id)
            if existing is None:
                command = Command(request_id=request_id, action=action, oven=oven, params=dict(params))
                self._records[request_id] = command
                self._persist()
                return command, True, False
            if existing.action != action or existing.oven != oven or existing.params != dict(params):
                raise IdempotencyConflictError(request_id)
            if existing.state in TERMINAL:
                return existing, False, True
            # 并发重发：首调仍在执行/排队，等其终态后回显。
            return existing, False, False

    def enqueue(self, command: Command, waiting_for: str, snapshot: dict) -> None:
        """把命令放进等待队列（队尾），记录当前不满足的原因。"""

        with self._lock:
            command.state = QUEUED
            command.progress = 0.0
            command.waiting_for = waiting_for
            command.snapshot = snapshot
            if command not in self._queue:
                self._queue.append(command)
            self._persist()

    def reject(self, command: Command, reason: dict, snapshot: dict) -> None:
        """当场拒绝：不进队列，直接落终态。"""

        with self._lock:
            command.state = REJECTED
            command.progress = 1.0
            command.reason = reason
            command.waiting_for = None
            command.snapshot = snapshot
            command.event.set()
            self._persist()

    def finish(
        self,
        command: Command,
        *,
        state: str,
        reason: dict | None = None,
        result: dict | None = None,
        op_id: str = "",
    ) -> None:
        """写入终态并唤醒等待同一标识的并发提交。"""

        if state not in TERMINAL:
            raise ValueError("终态只能是 complete 或 rejected")
        with self._lock:
            command.state = state
            command.progress = 1.0
            command.reason = reason
            command.result = result
            command.waiting_for = None
            if op_id:
                command.op_id = op_id
            command.event.set()
            self._persist()

    # ------------------------------------------------------------------ 队列

    def queued(self) -> list[Command]:
        """返回按提交顺序排列的排队命令。"""

        with self._lock:
            return list(self._queue)

    def take_next(self) -> Command | None:
        """取出队首命令进入执行态并出队（一次只放行一个）。

        命令的终态仍由 :meth:`finish` 落到登记记录里，因此"已放行但尚未
        写完终态"的窗口不会让同标识重发被当成排队中。
        """

        with self._lock:
            if not self._queue:
                return None
            command = self._queue.pop(0)
            command.state = RUNNING
            command.progress = 0.5
            # 立即落盘"已出队+RUNNING"，否则崩溃后队列文档仍旧会把它当排队。
            self._persist()
            return command

    def keep_queued(self, command: Command, waiting_for: str, snapshot: dict) -> None:
        """条件仍不满足：命令放回队首等下一周期。"""

        with self._lock:
            command.state = QUEUED
            command.progress = 0.0
            command.waiting_for = waiting_for
            command.snapshot = snapshot
            if command not in self._queue:
                self._queue.insert(0, command)
            self._persist()

    # ------------------------------------------------------------------ 查询

    def get(self, request_id: str) -> Command | None:
        with self._lock:
            return self._records.get(request_id)

    def list_commands(self, limit: int = 100) -> list[dict]:
        """最近的登记命令（最新在前）。"""

        with self._lock:
            commands = list(self._records.values())[-limit:]
        return [command.to_dict() for command in reversed(commands)]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    # ------------------------------------------------------------------ 落盘

    def _persist(self) -> None:
        # 记录只留最近 KEEP_RECORDS 条；队列文档始终按内存队列重建，避免崩溃
        # 窗口里残留的已出队 id 在重启后被重复入队。
        kept = list(self._records.values())[-KEEP_RECORDS:]
        self._store.write(RECORDS_DOC, {"commands": [command.to_dict() for command in kept]})
        live_ids = {command.request_id for command in kept}
        queue_ids = [command.request_id for command in self._queue if command.request_id in live_ids]
        self._store.write(QUEUE_DOC, {"queue": queue_ids})

    def _restore(self) -> None:
        raw = self._store.read(RECORDS_DOC)
        if isinstance(raw, dict):
            for item in raw.get("commands", []):
                try:
                    command = Command.from_dict(item)
                except (KeyError, TypeError, ValueError):
                    continue
                self._records[command.request_id] = command
                if command.state in TERMINAL:
                    command.event.set()
        queued_ids: list[str] = []
        queued_raw = self._store.read(QUEUE_DOC)
        if isinstance(queued_raw, dict):
            queued_ids = [str(item) for item in queued_raw.get("queue", [])]

        # 队列文档里的明确排队命令按序恢复；此外，崩溃时停在 RUNNING（已出队、
        # 未落终态）的命令也按未完成重新排队，保证重启不丢命令。
        recovered_ids = list(queued_ids)
        for command in self._records.values():
            if command.state == RUNNING and command.request_id not in recovered_ids:
                recovered_ids.append(command.request_id)
        for request_id in recovered_ids:
            command = self._records.get(request_id)
            if command is not None and command.state not in TERMINAL:
                command.state = QUEUED
                command.progress = 0.0
                self._queue.append(command)
