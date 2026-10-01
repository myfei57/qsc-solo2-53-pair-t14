"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（装煤、
推焦、熄焦、换向、炉门清扫）、控制循环、并发仲裁探测和状态快照。控制循环
每个周期做三件事：把集气管压力调向目标带、按推焦联动时序驱动除尘挡板、
采集趋势；换向与推焦这类跨模块动作都由编排一次性走完并写审计。

动作提交统一走状态门（:mod:`coke.guard`）：每次提交带调用方 ``request_id``
做并发幂等去重（同一标识只执行一次）；状态判定基于当场读回后冻结的一份
快照，状态不允许时按动作策略当场拒绝或排队等控制周期放行，拒绝原因、
控制台展示与审计落盘共用同一份原因与快照口径。
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Callable

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .charger.machine import ChargingCar
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config
from .door.latch import DoorLatch
from .door.machine import DoorCleaner
from .dust.damper import DustDamper
from .dust.schedule import DamperSchedule
from .dust.snapshot import DustSnapshot
from .dust.system import DustSystem
from .errors import CokeError
from .eventbus import Event, EventBus
from .gas.collector import CollectorMain
from .gas.flap import BridgeFlap
from .gas.latch import FlapLatch
from .guide.machine import GuideCar
from .guard import (
    ACTIONS,
    POLICIES,
    Command,
    CommandRegistry,
    StateSnapshot,
    evaluate,
    is_queueable,
)
from .guard.policy import QUEUE, READBACK
from .guard.registry import COMPLETE, QUEUED, REJECTED, RUNNING
from .guard.snapshot import IDLE, readback_failure
from .heating.machine import HeatingReversal
from .heating.snapshot import HeatingSnapshot
from .heating.temperature import OvenTemperature
from .heating.valvetrain import ValveTrain
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .pusher.machine import PusherMachine
from .pusher.speed import SpeedArbiter
from .pusher.stroke import StrokeBaseline
from .quench.machine import QuenchCar
from .sequence import charge_oven, clean_door, push_oven, quench_oven, reverse_heating
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .telemetry import TrendBuffer

DEFAULT_UNIT = "battery1"
DEFAULT_OVEN = "oven01"


class Platform:
    """焦炉推装熄顺序控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        self.pusher = PusherMachine(self.config.pusher)
        self.stroke = StrokeBaseline(
            self.config.pusher.stroke_baseline_mm, self.config.pusher.stroke_tolerance_mm
        )
        self.speed = SpeedArbiter(
            self.config.pusher.speed_min_mm_s,
            self.config.pusher.speed_max_mm_s,
            self.config.pusher.speed_default_mm_s,
        )
        self.guide = GuideCar(self.config.guide)
        self.charger = ChargingCar(self.config.charger)
        self.quench_car = QuenchCar(self.config.quench)
        self.collector = CollectorMain(self.config.collector)
        self.flap = BridgeFlap(FlapLatch())
        self.door = DoorCleaner(self.config.door, DoorLatch(self.config.door.latch_travel_mm))
        self.reversal = HeatingReversal(self.config.heating, self.clock)
        self.valves = ValveTrain()
        self.temperature = OvenTemperature(self.config.heating)
        self.dust = DustSystem()
        self.damper = DustDamper(self.config.dust.damper_open_deg, self.config.dust.damper_rate_deg_per_s)
        self.schedule = DamperSchedule(self.config.dust.linkage_lag_s)

        self._cycles = 0
        self._operations = 0
        self._lock = threading.RLock()
        self.commands = CommandRegistry(store)
        # 当前正在执行的动作占用的碳化室号，供快照做同炉串行判定。
        self._active_ovens: set[str] = set()
        # 状态回读故障注入：设备名在集合里时，快照组装按回读失败处理。
        self._readback_faults: set[str] = set()
        self._register_namespaces()
        self._restore()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("battery", "pusher", "pusher-machine"),
            ("battery", "guide", "guide-car"),
            ("battery", "charger", "charging-car"),
            ("battery", "quench", "quench-car"),
            ("battery", "collector", "collector-main"),
            ("battery", "bridge_flap", "bridge-flap"),
            ("battery", "door", "door-cleaner"),
            ("battery", "reversal", "reversing-valve"),
            ("battery", "dust", "dust-damper"),
        ):
            self.namespaces.register(area, device, kind)

    def _restore(self) -> None:
        """从落盘审计回放最后一次换向，恢复换向间隔计时。"""

        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except CokeError:
            return
        reversals = [item for item in records if item.action == "reverse_complete"]
        # 只接受不晚于当前时钟的回放记录；可推进时钟重置后，上一轮留下的未来
        # 时间戳属于过期状态，不能拿它把换向间隔计时顶到当前时刻之前。
        if reversals and reversals[-1].ts <= self.clock.now():
            self.reversal.mark_reversed(reversals[-1].ts)
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    # ------------------------------------------------------------------ 状态门

    def _state_snapshot(self) -> StateSnapshot:
        """当场从各设备读回并冻结一份状态快照。

        口径定死为快照：调用方拿到之后，状态判定与审计落盘都只用这一份。
        任一所需设备回读失败（含故障注入）都属部分失败，按不允许处理。
        """

        failures: tuple[str, ...] = ()

        def read(device: str, reader: Callable[[], object]) -> object | None:
            nonlocal failures
            if device in self._readback_faults:
                failures = (*failures, device)
                return None
            try:
                return reader()
            except CokeError:
                failures = (*failures, device)
                return None

        fan = read("dust", self.dust.is_running)
        pusher_state = read("pusher", lambda: self.pusher.state)
        pusher_ram = read("pusher", lambda: self.pusher.ram_mm)
        guide_locked = read("guide", self.guide.is_locked)
        flap_engaged = read("gas", lambda: self.flap.latch.is_engaged())
        quench_loaded = read("quench", lambda: self.quench_car.snapshot().loaded)
        quench_oven = read("quench", lambda: self.quench_car.snapshot().oven)
        reversal_due = read("heating", self.reversal.due)
        door_open = read("door", self.door.door_open)
        door_latch = read("door", self.door.latch.is_engaged)

        snapshot = StateSnapshot(
            fan_running=bool(fan) if fan is not None else False,
            pusher_state=str(pusher_state) if pusher_state is not None else IDLE,
            pusher_ram_mm=float(pusher_ram) if pusher_ram is not None else 0.0,
            guide_locked=bool(guide_locked) if guide_locked is not None else True,
            flap_engaged=bool(flap_engaged) if flap_engaged is not None else True,
            quench_loaded=bool(quench_loaded) if quench_loaded is not None else False,
            quench_oven=str(quench_oven) if quench_oven is not None else "",
            reversal_due=bool(reversal_due) if reversal_due is not None else False,
            door_open=bool(door_open) if door_open is not None else False,
            door_latch_engaged=bool(door_latch) if door_latch is not None else True,
            active_ovens=tuple(sorted(self._active_ovens)),
            readback_failures=failures,
        )
        if failures:
            return readback_failure(failures, snapshot)
        return snapshot

    def inject_readback_fault(self, *devices: str, clear: bool = False) -> dict:
        """注入/清除状态回读故障（验证 fail-closed 用）。"""

        with self._lock:
            if clear:
                self._readback_faults.clear()
            self._readback_faults.update(devices)
            return {"faults": sorted(self._readback_faults)}

    def _publish_progress(
        self,
        command: Command,
        *,
        state: str,
        severity: str,
        message: str,
        snapshot: StateSnapshot,
        reason: dict | None = None,
    ) -> None:
        """命令状态迁移统一从这里进总线，审计口径与响应口径一致。"""

        payload: dict = {
            "request_id": command.request_id,
            "action_name": command.action,
            "oven": command.oven,
            "state": state,
            "progress": command.progress,
            "snapshot_basis": "frozen",
            "snapshot": snapshot.as_dict(),
        }
        if reason:
            payload["reason_code"] = reason["code"]
            payload["message"] = message or reason["message"]
        else:
            payload["message"] = message
        self.bus.publish(Event.of("audit", "audit", "command_progress", severity=severity, **payload))

    def submit(self, request_id: str, action: str, oven: str, params: dict) -> dict:
        """动作提交统一入口：幂等去重 → 快照状态门 → 立即执行或排队。

        同一 ``request_id`` 的并发提交由平台大锁串行化：只有第一个调用执行
        动作体，其余调用拿到锁时首个提交已落终态或排队，直接回显同一份记录
        （``duplicate=True``），动作体不会再跑一遍。状态不允许时按动作策略
        当场拒绝或入队，拒绝原因与所用快照都写审计。
        """

        if action not in POLICIES:
            raise CokeError(f"未知动作 {action!r}", component="guard")

        with self._lock:
            command, acquired, duplicate = self.commands.begin(request_id, action, oven, params)
            if not acquired:
                # 平台大锁已把并发提交串行化：第二个同标识请求拿到锁时，首个
                # 提交必然已落终态或排队。直接回显已有记录，动作体不再执行。
                return command.envelope(duplicate=True)

            snapshot = self._state_snapshot()
            command.snapshot = snapshot.as_dict()
            reason = evaluate(action, snapshot, oven)
            if reason is None:
                self.bus.publish(
                    Event.of(
                        topic="audit",
                        component="audit",
                        action="command_submitted",
                        severity="notice",
                        request_id=request_id,
                        action_name=action,
                        oven=oven,
                        state=RUNNING,
                        snapshot_basis="frozen",
                        reason_code="",
                    )
                )
                return self._run_command(command, params, snapshot)

            reason_dict = reason.as_dict()
            if POLICIES[action].mode == QUEUE and is_queueable(reason):
                self.commands.enqueue(command, reason.code, snapshot.as_dict())
                self._publish_progress(
                    command,
                    state=QUEUED,
                    severity="notice",
                    message=f"{action} 排队等待：{reason.message}",
                    snapshot=snapshot,
                    reason=reason_dict,
                )
                return command.envelope()

            # REJECT 策略：当场拒绝；QUEUE 动作遇到非可排队原因（炉号不符、
            # 回读失败、同炉忙）也当场拒绝。
            self.commands.reject(command, reason_dict, snapshot.as_dict())
            self._publish_progress(
                command,
                state=REJECTED,
                severity="alarm",
                message=f"{action} 被拒绝：{reason.message}",
                snapshot=snapshot,
                reason=reason_dict,
            )
            return command.envelope()

    def _run_command(self, command: Command, params: dict, snapshot: StateSnapshot) -> dict:
        """在平台大锁内实际执行一个已放行命令并落终态。"""

        action = command.action
        oven = command.oven
        self._active_ovens.add(oven)
        command.state = RUNNING
        command.progress = 0.5
        runners: dict[str, Callable[[], dict]] = {
            "charge": lambda: self._execute_charge(oven, params),
            "push": lambda: self._execute_push(oven, params),
            "quench": lambda: self._execute_quench(oven, params),
            "reverse": lambda: self._execute_reverse(oven, params),
            "clean": lambda: self._execute_clean(oven, params),
        }
        try:
            report = runners[action]()
        finally:
            self._active_ovens.discard(oven)

        op_id = str(report.get("op_id", ""))
        if report.get("ok"):
            self.commands.finish(command, state=COMPLETE, result=report, op_id=op_id)
            self._publish_progress(
                command,
                state=COMPLETE,
                severity="notice",
                message=f"{action} 完成",
                snapshot=snapshot,
            )
        else:
            # 执行期违约（对位超差等）同样是终态拒绝，原因沿用编排违约项。
            violations = list(report.get("violations", []))
            reason = {
                "code": violations[0] if violations else "execution_incomplete",
                "message": f"{action} 执行未完成：{', '.join(violations)}",
                "component": action,
            }
            self.commands.finish(command, state=REJECTED, reason=reason, result=report, op_id=op_id)
            self._publish_progress(
                command,
                state=REJECTED,
                severity="alarm",
                message=reason["message"],
                snapshot=snapshot,
                reason=reason,
            )
        return command.envelope()

    def _drain_queue(self) -> list[dict]:
        """用当场新取的快照按 FIFO 判定排队命令，一次至多放行一个。

        排队命令遇回读失败属"暂时无法判定"，保持排队（fail-closed 但不丢）；
        只有在状态能读清、且确定遇到不可排队的原因时才当场拒绝。
        """

        released: list[dict] = []
        with self._lock:
            while True:
                command = self.commands.take_next()
                if command is None:
                    break
                snapshot = self._state_snapshot()
                command.snapshot = snapshot.as_dict()
                reason = evaluate(command.action, snapshot, command.oven)
                if reason is None:
                    released.append(self._run_command(command, command.params, snapshot))
                    # 交焦等状态跃迁可能立刻放行下一条（推焦→熄焦联动）。
                    continue
                if is_queueable(reason) or reason.code == READBACK:
                    self.commands.keep_queued(command, reason.code, snapshot.as_dict())
                else:
                    reason_dict = reason.as_dict()
                    self.commands.finish(command, state=REJECTED, reason=reason_dict)
                    self._publish_progress(
                        command,
                        state=REJECTED,
                        severity="alarm",
                        message=f"{command.action} 排队后被拒绝：{reason.message}",
                        snapshot=snapshot,
                        reason=reason_dict,
                    )
                    released.append(command.envelope())
                break
        return released

    def command(self, request_id: str) -> dict | None:
        """查询单条命令的最新状态（控制台轮询用）。"""

        command = self.commands.get(request_id)
        return None if command is None else command.envelope()

    def commands_describe(self, limit: int = 100) -> dict:
        """命令登记与排队摘要，供控制台展示。"""

        return {
            "policies": {action: POLICIES[action].describe() for action in ACTIONS},
            "pending": self.commands.pending_count(),
            "commands": self.commands.list_commands(limit=limit),
        }

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        oven = str(report.get("oven", ""))
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in step.get("detail", {}).items()
                if not isinstance(value, (dict, list))
                and key not in ("oven", "op_id", "severity", "message")
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    oven=oven,
                    op_id=op_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    oven=oven,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "oven": oven,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
        }
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                f"{operation}_complete",
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    def start(self) -> dict:
        """启动平台：启动除尘风机并核对拓扑。"""

        with self._lock:
            self.dust.start()
            report = self.topology.verify(set())
            return {
                "fan_running": self.dust.is_running(),
                "topology_missing": report["missing"],
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：停除尘风机。"""

        with self._lock:
            self.dust.stop()
            return {"fan_running": self.dust.is_running(), "operations": self._operations}

    def charge(
        self,
        oven: str = DEFAULT_OVEN,
        hopper_target_mm: float = 0.0,
        hopper_measured_mm: float = 0.0,
        dt: float = 1.0,
        request_id: str | None = None,
    ) -> dict:
        """装煤提交：煤斗对位 → 调压 → 开翻板 → 给煤。"""

        return self.submit(
            request_id or self.ids.next(),
            "charge",
            oven,
            {
                "hopper_target_mm": float(hopper_target_mm),
                "hopper_measured_mm": float(hopper_measured_mm),
                "dt": float(dt),
            },
        )

    def _execute_charge(self, oven: str, params: dict) -> dict:
        """装煤编排（状态门已放行）。"""

        op_id = self.ids.next()
        report = charge_oven(
            self.charger,
            self.collector,
            self.flap,
            oven,
            params["hopper_target_mm"],
            params["hopper_measured_mm"],
            params["dt"],
        )
        summary = self._record_operation(op_id, "charge", report)
        summary["op_id"] = op_id
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def push(
        self,
        oven: str = DEFAULT_OVEN,
        guide_target_mm: float = 0.0,
        guide_measured_mm: float = 0.0,
        stroke_target_mm: float | None = None,
        speed_mm_s: float | None = None,
        dt: float = 1.0,
        request_id: str | None = None,
    ) -> dict:
        """推焦提交：导焦栅对位 → 锁定 → 推焦 → 退杆；同时触发除尘联动。"""

        baseline = self.stroke.baseline_mm if stroke_target_mm is None else stroke_target_mm
        return self.submit(
            request_id or self.ids.next(),
            "push",
            oven,
            {
                "guide_target_mm": float(guide_target_mm),
                "guide_measured_mm": float(guide_measured_mm),
                "stroke_target_mm": float(baseline),
                "speed_mm_s": float(
                    self.config.pusher.speed_default_mm_s if speed_mm_s is None else speed_mm_s
                ),
                "dt": float(dt),
            },
        )

    def _execute_push(self, oven: str, params: dict) -> dict:
        """推焦编排（状态门已放行）；成功后交焦并泵一次等待队列。"""

        op_id = self.ids.next()
        self.speed.request("auto_cycle", params["speed_mm_s"], priority=50)
        arbitration = self.speed.resolve()
        self.schedule.start_link(self.clock.now())
        report = push_oven(
            self.pusher,
            self.guide,
            oven,
            params["guide_target_mm"],
            params["guide_measured_mm"],
            params["stroke_target_mm"],
            arbitration.speed_mm_s,
            params["dt"],
        )
        stroke_check = self.stroke.check(self.pusher.ram_mm)
        report["speed_arbitration"] = arbitration.as_dict()
        report["stroke_check"] = stroke_check
        summary = self._record_operation(op_id, "push", report)
        summary["op_id"] = op_id
        self._operations += 1
        self._persist()
        result = {**report, **summary}
        if report.get("ok"):
            # 推焦完成即交焦：熄焦车进入"已接焦"，排队的熄焦命令可立即放行。
            self.quench_car.receive(oven)
            self._drain_queue()
        return result

    def quench(self, oven: str = DEFAULT_OVEN, dt: float = 1.0, request_id: str | None = None) -> dict:
        """熄焦提交：走行 → 喷淋（未接焦时按策略排队等推焦交焦）。"""

        return self.submit(request_id or self.ids.next(), "quench", oven, {"dt": float(dt)})

    def _execute_quench(self, oven: str, params: dict) -> dict:
        """熄焦编排（状态门已放行）。"""

        op_id = self.ids.next()
        report = quench_oven(self.quench_car, oven, params["dt"])
        report["travel_eta_s"] = self.quench_car.plan.eta_s()
        summary = self._record_operation(op_id, "quench", report)
        summary["op_id"] = op_id
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def reverse(
        self,
        oven: str = DEFAULT_OVEN,
        temperature_c: float | None = None,
        request_id: str | None = None,
    ) -> dict:
        """换向提交：废气阀 → 煤气阀 → 炉温确认（未到期按策略排队）。"""

        target = self.config.heating.temp_target_c if temperature_c is None else temperature_c
        return self.submit(
            request_id or self.ids.next(), "reverse", oven, {"temperature_c": float(target)}
        )

    def _execute_reverse(self, oven: str, params: dict) -> dict:
        """换向编排（状态门已放行）。"""

        op_id = self.ids.next()
        report = reverse_heating(
            self.reversal, self.valves, self.temperature, oven, params["temperature_c"]
        )
        report["offset_c"] = self.temperature.offset_c()
        summary = self._record_operation(op_id, "reverse", report)
        summary["op_id"] = op_id
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def clean(self, oven: str = DEFAULT_OVEN, dt: float = 1.0, request_id: str | None = None) -> dict:
        """炉门清扫提交：松开闩锁 → 开炉门 → 清扫 → 复位闩锁。"""

        return self.submit(request_id or self.ids.next(), "clean", oven, {"dt": float(dt)})

    def _execute_clean(self, oven: str, params: dict) -> dict:
        """炉门清扫编排（状态门已放行）。"""

        op_id = self.ids.next()
        report = clean_door(self.door, oven, params["dt"])
        summary = self._record_operation(op_id, "clean", report)
        summary["op_id"] = op_id
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            before = self.collector.pressure_pa
            pressure = self.collector.regulate(dt)
            linkage = self.schedule.due(moment)
            target_deg = self.damper.open_deg if self.schedule.links() else 0.0
            angle = self.damper.drive_to(target_deg, dt)
            if self.damper.at_open():
                self.schedule.clear()
            self.trend.append("collector_pressure", moment, pressure)
            self.trend.append("damper_angle", moment, angle)
            self._persist()
            # 每周期用当场新取的快照判定排队命令（换向到期、熄焦接焦等）。
            released = self._drain_queue()
            return {
                "cycle": self._cycles,
                "pressure_pa": round(pressure, 2),
                "pressure_delta": round(pressure - before, 3),
                "settled": self.collector.settled(),
                "damper_deg": round(angle, 2),
                "linkage_due": linkage,
                "linkage_elapsed_s": round(self.schedule.elapsed_s(moment), 2),
                "released": [item["request_id"] for item in released],
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测推焦速度仲裁的收敛结果。

        两个线程分别代表自动循环与手动控制台，同时向推焦杆提交速度。探测只
        读取最终提交值，用来确认共享执行器始终收敛到唯一值。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, speed: float, priority: int) -> None:
            for _ in range(rounds):
                self.speed.request(writer, speed, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.config.pusher.speed_max_mm_s, 50)),
            threading.Thread(target=worker, args=("manual_console", self.config.pusher.speed_min_mm_s, 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.speed.resolve()
        self.pusher.set_speed(outcome.speed_mm_s)
        return {
            "rounds": rounds,
            "winner": outcome.winner,
            "committed_mm_s": round(outcome.speed_mm_s, 2),
            "contenders": outcome.contenders,
            "writers": self.speed.writers(),
            "committed_on_machine": round(self.pusher.committed_speed(), 2),
        }

    def calibrate_stroke(self, value_mm: float) -> dict:
        """重新标定推焦行程基线，返回标定前后的差值。"""

        if value_mm <= 0:
            raise CokeError("标定值必须为正", component="pusher")
        before = self.stroke.baseline_mm
        after = self.stroke.calibrate(value_mm)
        self.bus.publish(
            Event.of(
                "pusher",
                "pusher",
                "stroke_recalibrated",
                severity="notice",
                baseline_before_mm=before,
                baseline_after_mm=after,
                epoch=self.stroke.epoch,
            )
        )
        return {"before_mm": before, "after_mm": after, "epoch": self.stroke.epoch}

    def disturb_collector(self, delta_pa: float) -> dict:
        """模拟一次集气管扰动，返回扰动后压力。"""

        before = self.collector.pressure_pa
        after = self.collector.disturb(delta_pa)
        return {"before_pa": round(before, 2), "after_pa": round(after, 2), "settled": self.collector.settled()}

    def note_error(self, error: CokeError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform_state"):
            return False
        return self.store.remove("platform_state")

    def heating_snapshot(self) -> HeatingSnapshot:
        """返回加热换向状态快照。"""

        return HeatingSnapshot(
            waste_gas_open=self.valves.waste_open,
            gas_open=self.valves.gas_open,
            temperature_c=self.temperature.value_c,
            confirmed=self.temperature.confirmed(),
            reversals=self.reversal.count(),
            seconds_to_due=self.reversal.seconds_to_due(),
        )

    def dust_snapshot(self) -> DustSnapshot:
        """返回除尘状态快照。"""

        return DustSnapshot(
            fan_running=self.dust.is_running(),
            damper_deg=self.damper.angle_deg,
            damper_open=self.damper.at_open(),
            linkage_pending=self.schedule.links() > 0,
            links=self.schedule.links(),
        )

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "pusher": self.pusher.snapshot().as_dict(),
            "speed": {"committed_mm_s": round(self.speed.committed(), 2)},
            "stroke": {
                "baseline_mm": self.stroke.baseline_mm,
                "tolerance_mm": self.stroke.tolerance_mm,
                "epoch": self.stroke.epoch,
            },
            "guide": self.guide.snapshot().as_dict(),
            "charger": self.charger.snapshot().as_dict(),
            "quench": self.quench_car.snapshot().as_dict(),
            "gas": {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.collector.limits.pressure_target_pa,
                "settled": self.collector.settled(),
                "flap_deg": round(self.flap.angle_deg, 2),
                "flap_open": self.flap.is_open(),
                "latch_engaged": self.flap.latch.is_engaged(),
                "latch_cycles": self.flap.latch.cycles(),
                "latch_resets": self.flap.latch.resets(),
            },
            "door": {**self.door.snapshot().as_dict(), "latch_cycles": self.door.latch.cycles()},
            "heating": self.heating_snapshot().as_dict(),
            "dust": self.dust_snapshot().as_dict(),
            "dust_starts": self.dust.starts(),
            "commands_pending": self.commands.pending_count(),
            "policies": {
                action: {"mode": POLICIES[action].mode} for action in ACTIONS
            },
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
