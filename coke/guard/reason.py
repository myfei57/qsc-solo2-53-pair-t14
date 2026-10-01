"""结构化拒绝原因。

拒绝原因在三处必须保持同一口径：状态门判定时产生它、控制台响应展示它、
审计流水落盘它。因此原因不是自由文本，而是一个固定的 ``(code, message,
component)`` 三元组；``code`` 供程序判断，``message`` 供巡检与控制台展示，
``component`` 标明责任组件。原因码只允许在
:mod:`coke.guard.policy` 的前置条件里产生。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reason:
    """一次状态不允许判定的原因。"""

    code: str
    message: str
    component: str

    def as_dict(self) -> dict:
        """转成响应与审计可直接使用的字典。"""

        return {"code": self.code, "message": self.message, "component": self.component}
