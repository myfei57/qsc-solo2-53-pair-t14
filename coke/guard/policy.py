"""动作策略表：按动作定死"拒绝还是排队"与前置条件。

这是拒绝原因、状态判定与审计三处共用的唯一事实源：

* :data:`POLICIES` 为每个动作写死策略（``reject`` 表示状态不允许时当场
  拒绝，``queue`` 表示保留命令等状态满足后由控制周期放行）；
* 每个前置条件只返回一个结构化 :class:`~coke.guard.reason.Reason`，
  :func:`evaluate` 在一份冻结快照上依次核对，返回第一个不满足的原因。

状态回读失败（部分失败）在任何前置条件之前判定，一律按不允许处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .reason import Reason
from .snapshot import IDLE, StateSnapshot

REJECT = "reject"
QUEUE = "queue"

READBACK = "state_readback_failed"
FAN_OFF = "fan_not_running"
PUSHER_BUSY = "pusher_busy"
RAM_NOT_HOME = "ram_not_home"
GUIDE_LOCKED = "guide_apron_locked"
FLAP_ENGAGED = "flap_latch_engaged"
QUENCH_NOT_LOADED = "quench_not_loaded"
QUENCH_OVEN_MISMATCH = "quench_oven_mismatch"
REVERSAL_NOT_DUE = "reversal_not_due"
DOOR_OPEN = "door_open"
DOOR_LATCH_RELEASED = "door_latch_released"
OVEN_BUSY = "oven_busy"

_MESSAGES = {
    READBACK: ("状态回读失败，按不允许处理", "guard"),
    FAN_OFF: ("除尘风机未运行，禁止作业", "dust"),
    PUSHER_BUSY: ("推焦机未回到空闲，禁止再次动作", "pusher"),
    RAM_NOT_HOME: ("推焦杆未归零位，禁止推焦", "pusher"),
    GUIDE_LOCKED: ("导焦栅处于锁定状态，禁止推焦", "guide"),
    FLAP_ENGAGED: ("桥管翻板闩锁未复位，禁止重复装煤", "gas"),
    QUENCH_NOT_LOADED: ("熄焦车尚未接焦，等待推焦交焦", "quench"),
    QUENCH_OVEN_MISMATCH: ("熄焦车所接焦碳与申请炉号不一致", "quench"),
    REVERSAL_NOT_DUE: ("换向间隔未到，等待到期", "heating"),
    DOOR_OPEN: ("炉门处于打开状态，禁止再次清扫", "door"),
    DOOR_LATCH_RELEASED: ("炉门闩锁未压紧，禁止清扫", "door"),
    OVEN_BUSY: ("同一碳化室已有动作在执行", "guard"),
}


def _reason(code: str) -> Reason:
    message, component = _MESSAGES[code]
    return Reason(code, message, component)


# 标记原因：该原因出现时排队策略应保留命令，而不是拒绝。
QUEUEABLE = frozenset({QUENCH_NOT_LOADED, REVERSAL_NOT_DUE})


@dataclass(frozen=True)
class ActionPolicy:
    """一个动作的状态门策略。"""

    action: str
    mode: str
    preconditions: tuple[Callable[[StateSnapshot, str], str | None], ...]

    def describe(self) -> dict:
        """策略说明，供控制台自检与文档核对。"""

        return {"action": self.action, "mode": self.mode, "queueable_codes": sorted(QUEUEABLE)}


def _fan_and_oven(snapshot: StateSnapshot, oven: str) -> str | None:
    if not snapshot.fan_running:
        return FAN_OFF
    if snapshot.oven_busy(oven):
        return OVEN_BUSY
    return None


def _charge_preconditions(snapshot: StateSnapshot, oven: str) -> str | None:
    blocked = _fan_and_oven(snapshot, oven)
    if blocked:
        return blocked
    if snapshot.pusher_state != IDLE:
        return PUSHER_BUSY
    if snapshot.flap_engaged:
        return FLAP_ENGAGED
    return None


def _push_preconditions(snapshot: StateSnapshot, oven: str) -> str | None:
    blocked = _fan_and_oven(snapshot, oven)
    if blocked:
        return blocked
    if snapshot.pusher_state != IDLE:
        return PUSHER_BUSY
    if snapshot.pusher_ram_mm > 1e-6:
        return RAM_NOT_HOME
    if snapshot.guide_locked:
        return GUIDE_LOCKED
    return None


def _quench_preconditions(snapshot: StateSnapshot, oven: str) -> str | None:
    if not snapshot.quench_loaded:
        return QUENCH_NOT_LOADED
    if snapshot.quench_oven != oven:
        return QUENCH_OVEN_MISMATCH
    return None


def _reverse_preconditions(snapshot: StateSnapshot, oven: str) -> str | None:
    if not snapshot.reversal_due:
        return REVERSAL_NOT_DUE
    return None


def _clean_preconditions(snapshot: StateSnapshot, oven: str) -> str | None:
    blocked = _fan_and_oven(snapshot, oven)
    if blocked:
        return blocked
    if snapshot.door_open:
        return DOOR_OPEN
    if not snapshot.door_latch_engaged:
        return DOOR_LATCH_RELEASED
    return None


POLICIES: dict[str, ActionPolicy] = {
    "charge": ActionPolicy("charge", REJECT, (_charge_preconditions,)),
    "push": ActionPolicy("push", REJECT, (_push_preconditions,)),
    "quench": ActionPolicy("quench", QUEUE, (_quench_preconditions,)),
    "reverse": ActionPolicy("reverse", QUEUE, (_reverse_preconditions,)),
    "clean": ActionPolicy("clean", REJECT, (_clean_preconditions,)),
}

ACTIONS = tuple(POLICIES.keys())


def evaluate(action: str, snapshot: StateSnapshot, oven: str) -> Reason | None:
    """在一份冻结快照上判定动作是否放行。

    返回 ``None`` 表示允许；否则返回第一个不满足的原因。回读失败最先判定，
    对所有动作都按不允许处理。
    """

    policy = POLICIES[action]
    if not snapshot.readback_ok:
        return _reason(READBACK)
    for check in policy.preconditions:
        code = check(snapshot, oven)
        if code is not None:
            return _reason(code)
    return None


def is_queueable(reason: Reason) -> bool:
    """该原因是否属于"排队等状态满足"而非当场拒绝。"""

    return reason.code in QUEUEABLE
