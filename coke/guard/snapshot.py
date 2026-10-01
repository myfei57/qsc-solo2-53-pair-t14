"""状态门使用的平台快照。

口径定死为"快照"：每次提交判定或队列放行判定，都由平台当场从各设备读回
状态、组装成一份不可变 :class:`StateSnapshot`，之后拒绝原因判定、排队判定
和审计落盘都只用这一份快照，不允许判定中途再去现读设备——否则可能出现
"按旧状态放行、按新状态记审计"的不一致。

状态回读是现场最容易失败的一环（通信抖动、设备无响应）。约定：判定所需
的任一设备回读失败都属于部分失败，一律按"状态不允许"处理（fail-closed），
失败设备写进 :attr:`readback_failures`，原因码固定为
``state_readback_failed``。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StateSnapshot:
    """一次状态判定冻结下来的平台状态。"""

    fan_running: bool
    pusher_state: str
    pusher_ram_mm: float
    guide_locked: bool
    flap_engaged: bool
    quench_loaded: bool
    quench_oven: str
    reversal_due: bool
    door_open: bool
    door_latch_engaged: bool
    active_ovens: tuple[str, ...] = ()
    readback_failures: tuple[str, ...] = field(default=())

    @property
    def readback_ok(self) -> bool:
        """回读是否全部成功（失败即按不允许处理）。"""

        return not self.readback_failures

    def oven_busy(self, oven: str) -> bool:
        """该碳化室是否已有动作在执行（同炉串行，禁止并发推装）。"""

        return oven in self.active_ovens

    def as_dict(self) -> dict:
        """快照字段（含回读失败清单），审计与控制台共用。"""

        return {
            "fan_running": self.fan_running,
            "pusher_state": self.pusher_state,
            "pusher_ram_mm": round(self.pusher_ram_mm, 2),
            "guide_locked": self.guide_locked,
            "flap_engaged": self.flap_engaged,
            "quench_loaded": self.quench_loaded,
            "quench_oven": self.quench_oven,
            "reversal_due": self.reversal_due,
            "door_open": self.door_open,
            "door_latch_engaged": self.door_latch_engaged,
            "active_ovens": list(self.active_ovens),
            "readback_ok": self.readback_ok,
            "readback_failures": list(self.readback_failures),
        }


def readback_failure(devices: tuple[str, ...], partial: StateSnapshot | None = None) -> StateSnapshot:
    """回读失败时构造 fail-closed 快照。

    已经读到的字段保留在 ``partial`` 里供审计参考，但无论它们取值如何，
    :attr:`StateSnapshot.readback_failures` 非空都会让状态门按不允许处理。
    """

    base = partial or EMPTY_SNAPSHOT
    return StateSnapshot(
        fan_running=False,
        pusher_state=IDLE,
        pusher_ram_mm=base.pusher_ram_mm,
        guide_locked=True,
        flap_engaged=True,
        quench_loaded=False,
        quench_oven=base.quench_oven,
        reversal_due=False,
        door_open=False,
        door_latch_engaged=True,
        active_ovens=base.active_ovens,
        readback_failures=tuple(dict.fromkeys((*base.readback_failures, *devices))),
    )


IDLE = "idle"

# 回读全部失败时的保守基线：所有联锁都取最不安全方向的反面。
EMPTY_SNAPSHOT = StateSnapshot(
    fan_running=False,
    pusher_state=IDLE,
    pusher_ram_mm=0.0,
    guide_locked=True,
    flap_engaged=True,
    quench_loaded=False,
    quench_oven="",
    reversal_due=False,
    door_open=False,
    door_latch_engaged=True,
)
