"""设备状态现读（状态判定的唯一数据来源）。

准入判定**只认现读**，不认调用方随请求带来的快照：快照在网络上可能已经
过期，按旧快照放行会让设备在错误的状态下动作。状态口径由此定死——

* 每次准入在持有平台锁的情况下新建一个 :class:`LiveState`，按动作所需的
  组件逐个回读；回读到的就是判定时刻的真实状态。
* 任一所需组件回读失败（现场注入或将来接真实 PLC 时的通信失败）都属于
  **部分失败**：其余组件即使读成功也不放行，统一产出
  :data:`coke.control.reasons.READBACK_FAILED`，按不允许处理。

请求参数里的实测位置（如煤斗/导焦栅实测毫米数）属于现场喂入的采样值，
仍按参数传入；它们的公差核对在策略模块里完成，不经过这里的组件回读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..charger.machine import ChargingCar
from ..door.machine import DoorCleaner
from ..dust.system import DustSystem
from ..gas.collector import CollectorMain
from ..guide.machine import GuideCar
from ..heating.machine import HeatingReversal

#: 按动作需要回读的组件；组件名同时是审计与故障注入用的标识。
COMPONENTS_BY_ACTION: dict[str, tuple[str, ...]] = {
    "charge": ("charger", "collector"),
    "push": ("guide", "dust"),
    "quench": (),
    "reverse": ("reversal",),
    "clean": ("door",),
}


@dataclass(frozen=True)
class LiveState:
    """一次准入判定使用的现读快照（由平台在锁内构造）。

    ``failed`` 非空即表示部分组件回读失败，策略模块直接按不允许处理，
    不读取 ``values``。
    """

    values: dict[str, object] = field(default_factory=dict)
    failed: tuple[str, ...] = ()


def read_live(
    action: str,
    *,
    charger: ChargingCar,
    guide: GuideCar,
    collector: CollectorMain,
    reversal: HeatingReversal,
    dust: DustSystem,
    door: DoorCleaner,
    faults: frozenset[str] | Callable[[], frozenset[str]] = frozenset(),
) -> LiveState:
    """在平台锁内按动作所需组件现读，构造 :class:`LiveState`。

    ``faults`` 给出当前模拟回读失败的组件集合（交付后可替换成真实的通信
    失败探测）。任一所需组件落在故障集合里，整体按部分失败处理。
    """

    active = set(faults() if callable(faults) else set(faults))
    wanted = COMPONENTS_BY_ACTION.get(action, ())
    failed = [name for name in wanted if name in active]

    readers: dict[str, Callable[[], object]] = {
        "charger": lambda: {
            "aligned": charger.is_aligned(),
            "oven": charger.snapshot().oven,
        },
        "guide": lambda: {"aligned": guide.is_aligned(), "locked": guide.is_locked()},
        "collector": lambda: {
            "settled": collector.settled(),
            "pressure_pa": round(collector.pressure_pa, 2),
        },
        "reversal": lambda: {"due": reversal.due(), "count": reversal.count()},
        "dust": lambda: {"fan_running": dust.is_running()},
        "door": lambda: {
            "door_open": door.door_open(),
            "latch_engaged": door.latch.is_engaged(),
        },
    }
    values: dict[str, object] = {}
    for name in wanted:
        if name in active:
            continue
        try:
            values[name] = readers[name]()
        except Exception:  # noqa: BLE001 - 现读阶段任何异常都按部分失败处理
            failed.append(name)
    if failed:
        return LiveState(values=values, failed=tuple(failed))
    return LiveState(values=values)
