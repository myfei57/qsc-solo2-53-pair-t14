"""提交前状态门：策略、快照、幂等登记与命令队列。

控制台把"同一动作连点两下 / 网络抖动重发"与"当前平台状态不允许的操作"都
收敛到这一层，约定只有一份：

* 状态判定一律基于 :class:`~coke.guard.snapshot.StateSnapshot`，由平台当场
  从各设备读回后冻结；拒绝原因、排队判定、审计落盘共用同一份快照；
* 策略表 :data:`POLICIES` 按动作写死"当场拒绝还是排队等"与前置条件，是
  拒绝原因的唯一事实源，控制台展示与审计都只引用这里产生的原因码；
* 同一 ``request_id`` 的并发提交由 :class:`~coke.guard.registry.CommandRegistry`
  幂等去重，保证动作体只执行一次，重发者拿到同一份终态。
"""

from __future__ import annotations

from ..errors import IdempotencyConflictError
from .policy import ACTIONS, POLICIES, ActionPolicy, evaluate, is_queueable
from .reason import Reason
from .registry import Command, CommandRegistry
from .snapshot import StateSnapshot, readback_failure

__all__ = [
    "ACTIONS",
    "POLICIES",
    "ActionPolicy",
    "Command",
    "CommandRegistry",
    "IdempotencyConflictError",
    "Reason",
    "StateSnapshot",
    "evaluate",
    "is_queueable",
    "readback_failure",
]
