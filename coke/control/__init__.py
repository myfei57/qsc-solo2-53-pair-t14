"""提交控制：幂等标识、动作准入、排队仲裁与统一拒绝口径。

控制台点快了或网络重试会让同一个动作被提交多次。本包在动作真正落到设备
之前加一道统一的闸口：

1. 每次提交必须带 ``request_id``（控制台必传，平台直连可代发）；同一标识
   并发或重放只执行一次，后来的提交拿到同一份结果（``idempotent=true``）。
2. 动作是否被允许，由 :mod:`coke.control.policy` 对照**当时现读**的设备状态
   判定，状态口径在 :mod:`coke.control.readback` 里定死，不使用调用方给的
   快照；状态回读失败属于部分失败，一律按不允许处理。
3. 当场拒绝还是排队等状态满足，按动作在策略表里写死；目前只有装煤的
   "集气管压力未稳"允许排队，其余不满足即当场拒绝。
4. 拒绝原因只有 :mod:`coke.control.reasons` 这一张表，状态判定、控制台
   展示和审计落盘三处取同一份口径，不允许各处自己拼字符串。
"""

from __future__ import annotations

from .gate import STATE_BASIS, ControlGate
from .readback import LiveState
from .registry import RequestRegistry, canonical_fingerprint

__all__ = [
    "STATE_BASIS",
    "ControlGate",
    "LiveState",
    "RequestRegistry",
    "canonical_fingerprint",
]
