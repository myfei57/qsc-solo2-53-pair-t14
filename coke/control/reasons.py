"""统一拒绝原因表。

拒绝原因、状态判定与审计三处必须一致，因此原因码、说明文字、归属组件和
"是否属于可排队的瞬时条件"只允许在这一张表里定义。状态判定模块
（:mod:`coke.control.policy`）只产出原因码；控制台展示与审计落盘都通过
:func:`describe` 取同一份说明。

每条原因固定四列：

``message``  控制台提示与审计说明共用的中文描述；
``component`` 归属组件，审计按组件分流；
``transient`` 是否是"等一等状态就能满足"的瞬时条件——只有瞬时原因才可能
             被策略表映射成排队，非瞬时原因永远当场拒绝。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 准入阶段（动作尚未落到设备）
# ---------------------------------------------------------------------------

#: 煤斗没有对准目标炉位（装煤）。
HOPPER_NOT_ALIGNED = "hopper_not_aligned"

#: 集气管压力还没进入目标带（装煤的唯一可排队条件）。
PRESSURE_NOT_SETTLED = "pressure_not_settled"

#: 导焦栅没有对准目标炉位（推焦）。
GUIDE_NOT_ALIGNED = "guide_not_aligned"

#: 除尘风机未运行（推焦前必须先启动平台）。
FAN_NOT_RUNNING = "fan_not_running"

#: 加热换向间隔未到（换向）。
REVERSAL_NOT_DUE = "reversal_not_due"

#: 炉门已打开（上一轮清扫尚未结束），不能重复开始清扫。
DOOR_ALREADY_OPEN = "door_already_open"

#: 排队等待状态满足超时。
QUEUE_TIMEOUT = "queue_timeout"

#: 同一 request_id 携带了不同的动作或参数，疑似标识被复用。
REQUEST_CONFLICT = "request_conflict"

#: 状态回读失败：部分失败按不允许处理。
READBACK_FAILED = "readback_failed"

#: 未知动作。
UNKNOWN_ACTION = "unknown_action"

# ---------------------------------------------------------------------------
# 执行阶段（动作已放行，中途未成功）
# ---------------------------------------------------------------------------

#: 推焦行程未走完。
STROKE_INCOMPLETE = "stroke_incomplete"

#: 熄焦走行未到位。
TRAVEL_INCOMPLETE = "travel_incomplete"

#: 喷淋未完成。
SPRAY_INCOMPLETE = "spray_incomplete"

#: 炉温未落在确认带内。
TEMP_NOT_CONFIRMED = "temp_not_confirmed"

#: 清扫未达到设定时长。
CLEAN_INCOMPLETE = "clean_incomplete"

#: 给煤未达到目标吨位。
FEED_INCOMPLETE = "feed_incomplete"

#: 执行阶段抛出的其他异常。
EXECUTION_FAILED = "execution_failed"


_REASON_TABLE: dict[str, tuple[str, str, bool]] = {
    HOPPER_NOT_ALIGNED: ("煤斗未对位，禁止给煤", "charger", False),
    PRESSURE_NOT_SETTLED: ("集气管压力未进入目标带", "gas", True),
    GUIDE_NOT_ALIGNED: ("导焦栅未对位，禁止推焦", "guide", False),
    FAN_NOT_RUNNING: ("除尘风机未运行，禁止推焦", "dust", False),
    REVERSAL_NOT_DUE: ("换向间隔未到，暂不允许换向", "heating", False),
    DOOR_ALREADY_OPEN: ("炉门已打开，上一轮清扫尚未结束", "door", False),
    QUEUE_TIMEOUT: ("排队等待状态满足超时", "gas", False),
    REQUEST_CONFLICT: ("同一提交标识携带了不同的动作或参数", "control", False),
    READBACK_FAILED: ("设备状态回读失败，按不允许处理", "control", False),
    UNKNOWN_ACTION: ("未知动作", "control", False),
    STROKE_INCOMPLETE: ("推焦行程未完成", "pusher", False),
    TRAVEL_INCOMPLETE: ("熄焦走行未到位", "quench", False),
    SPRAY_INCOMPLETE: ("喷淋未完成", "quench", False),
    TEMP_NOT_CONFIRMED: ("炉温未确认，换向不成功", "heating", False),
    CLEAN_INCOMPLETE: ("炉门清扫未完成", "door", False),
    FEED_INCOMPLETE: ("给煤未达到目标吨位", "charger", False),
    EXECUTION_FAILED: ("动作执行失败", "control", False),
}


def describe(code: str) -> dict:
    """把原因码翻译成控制台/审计共用的说明。

    未知码一律落到 :data:`EXECUTION_FAILED` 的口径，并保留原始码，避免新增
    原因时控制台展示出空白。
    """

    message, component, transient = _REASON_TABLE.get(
        code, (f"未登记的拒绝原因: {code}", "control", False)
    )
    return {"code": code, "message": message, "component": component, "transient": transient}


def all_reasons() -> list[str]:
    """返回全部登记的原因码（供自检与文档核对）。"""

    return list(_REASON_TABLE)
