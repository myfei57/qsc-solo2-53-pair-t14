"""提交闸口：幂等 + 准入 + 排队 + 审计的统一入口。

平台的五个动作（装煤、推焦、熄焦、换向、清扫）都经 :meth:`ControlGate.submit`
进入，流程是：

1. 登记 ``request_id``：并发同标识只有一个执行者，重复提交等待并回传同一
   结果；终态重放直接返回登记结果，设备动作绝不重做。
2. 锁内现读设备状态（:mod:`coke.control.readback`），交策略模块
   （:mod:`coke.control.policy`）判定。回读失败按不允许处理；策略判为排队
   的，释放平台锁按控制周期推进等待，直到状态满足或排队超时。
3. 放行后执行动作；拒绝原因、执行违约都通过 :mod:`coke.control.reasons`
   的同一张原因表翻译成控制台展示与审计文案。

状态判定取数口径在 :data:`~coke.control.policy.STATE_BASIS` 定死为锁内现读。
"""

from __future__ import annotations

import threading
from typing import Callable

from ..eventbus import Event
from ..ids import IdFactory
from . import policy, readback, reasons
from .policy import STATE_BASIS
from .readback import LiveState
from .registry import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_REJECTED,
    RequestRegistry,
    canonical_fingerprint,
)

#: 排队等待状态满足的最长时间（秒）。
QUEUE_TIMEOUT_S = 600.0
#: 排队时的控制周期步长，也是轮询间隔（秒）：每等一个步长就按同一时长
#: 推进一次控制周期，保证 ManualClock 下"经过的时间"与"状态推进量"一致。
QUEUE_STEP_DT = 0.5


class ControlGate:
    """所有动作提交共用的幂等与准入闸口。"""

    def __init__(
        self,
        registry: RequestRegistry,
        *,
        clock,
        live_reader: Callable[[str, dict], LiveState],
        cycle_pump: Callable[[float], None],
        settled_checks: dict[str, Callable[[], bool]],
        bus,
        op_ids: IdFactory,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._read_live = live_reader
        self._pump = cycle_pump
        self._settled_checks = dict(settled_checks)
        self._bus = bus
        self._op_ids = op_ids
        self._faults: set[str] = set()
        self._fault_lock = threading.Lock()

    # -- 回读故障注入 ------------------------------------------------------

    def fault_components(self) -> list[str]:
        """返回当前模拟回读失败的组件。"""

        with self._fault_lock:
            return sorted(self._faults)

    def inject_fault(self, components: list[str]) -> list[str]:
        """把指定组件标记为回读失败，返回生效后的故障组件列表。"""

        known = {name for values in readback.COMPONENTS_BY_ACTION.values() for name in values}
        unknown = [name for name in components if name not in known]
        if unknown:
            from ..errors import ConfigError

            raise ConfigError(f"未知的回读组件: {', '.join(unknown)}", component="control")
        with self._fault_lock:
            self._faults = set(components)
            return sorted(self._faults)

    def clear_faults(self) -> list[str]:
        """清除全部回读故障。"""

        with self._fault_lock:
            self._faults.clear()
            return []

    # -- 策略只读视图 ------------------------------------------------------

    def policy(self) -> dict:
        """返回动作处置策略与当前排队/登记计数。"""

        summary = policy.policy_summary()
        summary["registry"] = self._registry.summary()
        summary["readback_faults"] = self.fault_components()
        return summary

    def preview(self, action: str, params: dict, config) -> dict:
        """只判定不执行：返回该动作在当前状态下的准入结论。"""

        live = self._read_live(action, params)
        verdict = policy.evaluate(action, params, live, config)
        return verdict.as_dict()

    def request_view(self, request_id: str) -> dict | None:
        return self._registry.get(request_id)

    def requests_recent(self, limit: int = 50) -> list[dict]:
        return self._registry.recent(limit=limit)

    # -- 主流程 ------------------------------------------------------------

    def submit(
        self,
        action: str,
        params: dict,
        executor: Callable[[], dict],
        *,
        request_id: str | None,
        platform_lock: threading.RLock,
        oven: str,
        config,
    ) -> dict:
        """提交一个动作，返回幂等包装后的结果信封。"""

        request_id = request_id or f"req-{self._op_ids.next()}"
        fingerprint = canonical_fingerprint(action, params)
        role, replay = self._registry.begin(request_id, action, fingerprint)
        if role == "duplicate":
            if replay is not None:
                return {**replay, "idempotent": True}
            outcome = self._registry.await_terminal(request_id, timeout=QUEUE_TIMEOUT_S + 30.0)
            return {**outcome, "idempotent": True}

        try:
            return self._lead(
                action, params, executor, request_id, platform_lock, oven, config
            )
        except Exception as error:
            failure = self._envelope(
                request_id, action, oven, accepted=False, status=STATE_FAILED,
                report={"oven": oven, "ok": False, "steps": [],
                        "violations": [reasons.EXECUTION_FAILED],
                        "error": str(error)},
                reason=reasons.EXECUTION_FAILED, progress=0.0,
            )
            self._registry.finish(request_id, STATE_FAILED, failure)
            raise

    def _lead(
        self, action, params, executor, request_id, platform_lock, oven, config
    ) -> dict:
        verdict = self._evaluate_under_lock(action, params, platform_lock, config)

        if verdict.queued:
            self._registry.mark_queued(request_id)
            self._publish_decision(
                action, oven, request_id, verdict, queued=True,
                message=f"{policy.ACTION_LABELS[action]} 排队等待状态满足",
            )
            verdict, _live = self._wait_until_satisfied(
                action, params, platform_lock, config, request_id, oven
            )

        if not verdict.allowed:
            return self._reject(action, oven, request_id, verdict)

        op_id = self._op_ids.next()
        report = executor(op_id, request_id)
        accepted = bool(report.get("ok"))
        reason = None if accepted else self._dominant_reason(report)
        status = STATE_COMPLETED
        envelope = self._envelope(
            request_id, action, oven,
            accepted=accepted, status=status, report=report,
            reason=reason, progress=self._progress(action, report),
            op_id=op_id, detail=verdict.detail,
        )
        self._registry.finish(request_id, status, envelope)
        return envelope

    # -- 准入与排队 --------------------------------------------------------

    def _evaluate_under_lock(self, action, params, platform_lock, config):
        with platform_lock:
            live = self._read_live(action, params)
            return policy.evaluate(action, params, live, config)

    def _wait_until_satisfied(self, action, params, platform_lock, config, request_id, oven):
        deadline_pumps = max(1, int(QUEUE_TIMEOUT_S / QUEUE_STEP_DT))
        checker = self._settled_checks.get(action, lambda: True)
        verdict = None
        for _ in range(deadline_pumps):
            self._clock.sleep(QUEUE_STEP_DT)
            with platform_lock:
                # 释放锁期间由控制周期把压力调向目标带；这里同样主动推进，
                # 保证即使没有外部周期调用，排队动作也能等到状态满足。
                if not checker():
                    self._pump(QUEUE_STEP_DT)
                live = self._read_live(action, params)
                verdict = policy.evaluate(action, params, live, config)
            if verdict.allowed or not verdict.queued:
                return verdict, live
        # 排队超时：按该原因当场拒绝（非瞬时）。
        timeout = policy.Verdict.reject(
            reasons.QUEUE_TIMEOUT,
            {"waited_s": QUEUE_TIMEOUT_S, **(verdict.detail if verdict else {})},
        )
        return timeout, live

    # -- 拒绝与审计 --------------------------------------------------------

    def _reject(self, action, oven, request_id, verdict) -> dict:
        code = verdict.reason or reasons.READBACK_FAILED
        described = reasons.describe(code)
        self._publish_decision(
            action, oven, request_id, verdict, queued=False,
            message=f"{policy.ACTION_LABELS.get(action, action)} 被拒绝：{described['message']}",
        )
        report = {"oven": oven, "ok": False, "steps": [], "violations": [code], **verdict.detail}
        envelope = self._envelope(
            request_id, action, oven,
            accepted=False, status=STATE_REJECTED, report=report,
            reason=code, progress=0.0, detail=verdict.detail,
        )
        self._registry.finish(request_id, STATE_REJECTED, envelope)
        return envelope

    def _publish_decision(self, action, oven, request_id, verdict, *, queued, message) -> None:
        code = verdict.reason
        described = reasons.describe(code) if code else None
        event_action = f"{action}_queued" if queued else "interlock_rejected"
        payload = {
            "oven": oven,
            "op_id": request_id,
            "request_id": request_id,
            "operation": action,
            "severity": "notice" if queued else "alarm",
            "message": message,
        }
        if described is not None:
            payload["violation"] = code
            payload["reason"] = described
            payload["reason_component"] = described["component"]
        self._bus.publish(Event.of("audit", "audit", event_action, **payload))

    # -- 结果信封 ----------------------------------------------------------

    def _dominant_reason(self, report: dict) -> str:
        violations = list(report.get("violations", []))
        return violations[0] if violations else reasons.EXECUTION_FAILED

    def _progress(self, action: str, report: dict) -> float:
        steps = len(report.get("steps", []))
        expected = policy.expected_steps(action) or max(1, steps)
        if report.get("ok"):
            return 1.0
        return round(min(0.99, steps / float(expected)), 3)

    def _envelope(
        self, request_id, action, oven, *, accepted, status, report, reason,
        progress, op_id=None, detail=None,
    ) -> dict:
        envelope = dict(report)
        envelope.update(
            {
                "request_id": request_id,
                "op_id": op_id or report.get("op_id", ""),
                "operation": action,
                "oven": oven,
                "accepted": accepted,
                "status": status,
                "state_basis": STATE_BASIS,
                "progress": progress,
                "reason": reasons.describe(reason) if reason else None,
                "admission": detail or {},
            }
        )
        return envelope
