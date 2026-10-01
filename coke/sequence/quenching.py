"""熄焦顺序：走行 → 喷淋。

接焦不在这里做：红焦由推焦完成时由平台交给熄焦车（``QuenchCar.receive``），
状态门据此放行/排队熄焦提交，避免编排自行复位把"未接焦"这道联锁架空。
"""

from __future__ import annotations

from ..quench.machine import QuenchCar

MAX_STEPS = 2400


def quench_oven(quench: QuenchCar, oven: str, dt: float = 1.0) -> dict:
    """执行一炉熄焦，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    if not quench.snapshot().loaded:
        # 状态门已按 quench_not_loaded 排队；这里兜底防止绕过编排直调。
        violations.append("quench_not_loaded")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    steps.append({"component": "quench", "action": "receive", "detail": {"oven": oven}})

    for _ in range(MAX_STEPS):
        if quench.at_tower():
            break
        quench.travel(dt)
    steps.append(
        {
            "component": "quench",
            "action": "travel",
            "detail": {"travelled_m": round(quench.snapshot().travelled_m, 2)},
        }
    )
    if not quench.at_tower():
        violations.append("travel_incomplete")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    quench.start_spray()
    steps.append({"component": "quench", "action": "start_spray", "detail": {"duration_s": quench.spray.duration_s}})
    for _ in range(MAX_STEPS):
        if quench.spray.complete():
            break
        quench.spray_step(dt)
    steps.append(
        {
            "component": "quench",
            "action": "spray",
            "detail": {
                "water_m3": round(quench.spray.water_m3(), 3),
                "complete": quench.spray.complete(),
            },
        }
    )
    if not quench.spray.complete():
        violations.append("spray_incomplete")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    return {"oven": oven, "ok": True, "steps": steps, "violations": violations}
