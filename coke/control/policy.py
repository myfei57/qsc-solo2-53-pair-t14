"""动作准入策略：当场拒绝还是排队，按动作定死。

策略表回答两个问题：

1. **当前状态允不允许？** 输入是 :mod:`coke.control.readback` 在锁内现读的
   :class:`~coke.control.readback.LiveState`，以及请求里喂入的现场采样
   （煤斗/导焦栅实测位置、换向目标炉温——它们是采样输入，不是状态快照）。
   判定不接受任何调用方自带的"当前状态"。
2. **不允许时怎么处置？** 处置方式按动作写死在 :data:`QUEUABLE_REASONS`
   里：只有"装煤遇到集气管压力未稳"允许排队等状态满足，其余不满足全部
   当场拒绝。状态回读失败永远当场拒绝。

判定只产出原因码（:mod:`coke.control.reasons`），说明文字、控制台展示与
审计文案全部回到同一张原因表取，保证三处一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import PlatformConfig
from ..guide.align import Alignment
from . import reasons
from .readback import LiveState
from .reasons import (
    DOOR_ALREADY_OPEN,
    FAN_NOT_RUNNING,
    GUIDE_NOT_ALIGNED,
    HOPPER_NOT_ALIGNED,
    PRESSURE_NOT_SETTLED,
    READBACK_FAILED,
    REVERSAL_NOT_DUE,
    UNKNOWN_ACTION,
)

#: 状态判定的取数口径，定死为"准入时刻在平台锁内现读"，不接受请求快照。
STATE_BASIS = "live_current"

#: 平台受理的全部动作。
ACTIONS: tuple[str, ...] = ("charge", "push", "quench", "reverse", "clean")

#: 每个动作执行成功时编排应走完的步骤数，用于控制台进度展示。
EXPECTED_STEPS: dict[str, int] = {
    "charge": 5,  # 对位 → 调压 → 开翻板 → 给煤 → 关翻板
    "push": 5,  # 对位 → 锁定 → 推焦 → 退杆 → 解锁
    "quench": 4,  # 接焦 → 走行 → 开喷淋 → 喷淋
    "reverse": 3,  # 废气阀 → 煤气阀 → 炉温确认
    "clean": 5,  # 松闩 → 开门 → 清扫 → 标记完成 → 关门
}

#: 动作 → 允许"排队等状态满足"的原因集合；不在表里的原因一律当场拒绝。
QUEUABLE_REASONS: dict[str, frozenset[str]] = {
    "charge": frozenset({PRESSURE_NOT_SETTLED}),
}

#: 动作 → 中文动作名（控制台展示与审计共用）。
ACTION_LABELS: dict[str, str] = {
    "charge": "装煤",
    "push": "推焦",
    "quench": "熄焦",
    "reverse": "加热换向",
    "clean": "炉门清扫",
}


@dataclass(frozen=True)
class Verdict:
    """一次准入判定。"""

    allowed: bool
    queued: bool = False
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @classmethod
    def permit(cls, detail: dict | None = None) -> "Verdict":
        return cls(True, False, None, dict(detail or {}))

    @classmethod
    def reject(cls, reason: str, detail: dict | None = None) -> "Verdict":
        return cls(False, False, reason, dict(detail or {}))

    @classmethod
    def wait(cls, reason: str, detail: dict | None = None) -> "Verdict":
        return cls(False, True, reason, dict(detail or {}))

    def as_dict(self) -> dict:
        payload = {"allowed": self.allowed, "queued": self.queued, "detail": self.detail}
        if self.reason is not None:
            payload["reason"] = reasons.describe(self.reason)
        return payload


def _queueable(action: str, reason: str) -> bool:
    return reason in QUEUABLE_REASONS.get(action, frozenset())


def _deny(action: str, reason: str, detail: dict) -> Verdict:
    """按动作策略把原因映射成当场拒绝或排队。"""

    if _queueable(action, reason):
        return Verdict.wait(reason, detail)
    return Verdict.reject(reason, detail)


def evaluate(action: str, params: dict, live: LiveState, config: PlatformConfig) -> Verdict:
    """对照现读状态判定动作是否允许，返回准入结论。"""

    if action not in ACTIONS:
        return Verdict.reject(UNKNOWN_ACTION, {"action": action})
    if live.failed:
        # 部分失败按不允许处理：列清失败组件，绝不带伤放行。
        return Verdict.reject(
            READBACK_FAILED, {"failed_components": list(live.failed), "state_basis": STATE_BASIS}
        )

    if action == "charge":
        return _evaluate_charge(params, live, config)
    if action == "push":
        return _evaluate_push(params, live, config)
    if action == "reverse":
        heating = live.values["reversal"]
        if not bool(heating["due"]):
            return _deny(action, REVERSAL_NOT_DUE, {"seconds_to_due": heating.get("seconds_to_due")})
        return Verdict.permit({"due": True})
    if action == "clean":
        # 松闩本身是清扫的第 1 步，闩锁压紧不是拒绝理由；真正要拦的是
        # 上一轮炉门还开着时又开始新一轮清扫。
        door = live.values["door"]
        if bool(door["door_open"]):
            return _deny(action, DOOR_ALREADY_OPEN, {"door_open": True})
        return Verdict.permit({"door_open": False})
    return Verdict.permit({})


def _evaluate_charge(params: dict, live: LiveState, config: PlatformConfig) -> Verdict:
    alignment = Alignment(
        float(params.get("hopper_target_mm", 0.0)),
        float(params.get("hopper_measured_mm", 0.0)),
        config.charger.align_tolerance_mm,
    )
    if not alignment.ok:
        return Verdict.reject(HOPPER_NOT_ALIGNED, {"alignment": alignment.as_dict()})
    collector = live.values["collector"]
    if not bool(collector["settled"]):
        return _deny(
            "charge",
            PRESSURE_NOT_SETTLED,
            {"pressure_pa": collector["pressure_pa"], "target_pa": config.collector.pressure_target_pa},
        )
    return Verdict.permit({"alignment": alignment.as_dict(), "pressure_pa": collector["pressure_pa"]})


def _evaluate_push(params: dict, live: LiveState, config: PlatformConfig) -> Verdict:
    alignment = Alignment(
        float(params.get("guide_target_mm", 0.0)),
        float(params.get("guide_measured_mm", 0.0)),
        config.guide.align_tolerance_mm,
    )
    if not alignment.ok:
        return Verdict.reject(GUIDE_NOT_ALIGNED, {"alignment": alignment.as_dict()})
    dust = live.values["dust"]
    if not bool(dust["fan_running"]):
        return Verdict.reject(FAN_NOT_RUNNING, {"fan_running": False})
    return Verdict.permit({"alignment": alignment.as_dict(), "fan_running": True})


def expected_steps(action: str) -> int:
    """返回动作成功时的预期步骤数（未知动作返回 0）。"""

    return EXPECTED_STEPS.get(action, 0)


def policy_summary() -> dict:
    """返回每个动作的处置策略，供控制台按钮置灰与文档核对。"""

    return {
        "state_basis": STATE_BASIS,
        "reasons": [reasons.describe(code) for code in reasons.all_reasons()],
        "actions": {
            action: {
                "label": ACTION_LABELS[action],
                "queueable_reasons": sorted(QUEUABLE_REASONS.get(action, frozenset())),
                "expected_steps": EXPECTED_STEPS[action],
            }
            for action in ACTIONS
        },
    }
