"""提交状态门：幂等去重、按动作拒绝/排队、回读失败 fail-closed。"""

from __future__ import annotations

import threading

import pytest

from coke.app import build_platform
from coke.config import default_config
from coke.errors import IdempotencyConflictError
from coke.guard import POLICIES, evaluate
from coke.guard.snapshot import readback_failure


def test_policies_pin_reject_or_queue_per_action():
    assert POLICIES["charge"].mode == "reject"
    assert POLICIES["push"].mode == "reject"
    assert POLICIES["clean"].mode == "reject"
    assert POLICIES["quench"].mode == "queue"
    assert POLICIES["reverse"].mode == "queue"


def test_readback_failure_is_treated_as_not_allowed_for_every_action():
    broken = readback_failure(("pusher", "dust"))
    assert broken.readback_ok is False
    assert "pusher" in broken.readback_failures
    for action in POLICIES:
        reason = evaluate(action, broken, "oven01")
        assert reason is not None
        assert reason.code == "state_readback_failed"


def test_charge_rejected_when_fan_off(platform):
    # 平台未 start：除尘风机不运行，charge 当场拒绝。
    result = platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-fan-off"
    )
    assert result["state"] == "rejected"
    assert result["ok"] is False
    assert result["progress"] == 1.0
    assert result["reason"]["code"] == "fan_not_running"
    assert result["result"] is None


def test_duplicate_request_id_executes_once(platform):
    platform.start()
    first = platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-once"
    )
    second = platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-once"
    )
    assert first["state"] == "complete" and first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["op_id"] == first["op_id"]
    # 给煤只发生一次：煤量计数不翻倍。
    assert platform.charger.snapshot().fed_t == platform.config.charger.coal_target_t
    assert platform.status()["operations"] == 1


def test_same_request_id_with_different_params_conflicts(platform):
    platform.start()
    platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-mismatch"
    )
    with pytest.raises(IdempotencyConflictError):
        platform.charge(
            oven="oven02", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-mismatch"
        )


def test_concurrent_same_id_only_runs_action_once(platform):
    platform.start()
    outcomes: list[dict] = []

    def submit() -> None:
        outcomes.append(platform.push(oven="oven02", request_id="r-conc"))

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 10
    leaders = [item for item in outcomes if not item["duplicate"]]
    assert len(leaders) == 1
    assert len({item["op_id"] for item in outcomes}) == 1
    assert platform.pusher.snapshot().strokes == 1
    assert platform.status()["operations"] == 1


def test_quench_queues_until_push_hands_off_coke(platform):
    platform.start()
    queued = platform.quench(oven="oven04", request_id="r-q-wait")
    assert queued["state"] == "queued"
    assert queued["waiting_for"] == "quench_not_loaded"
    assert platform.commands.pending_count() == 1

    # 排队期间的同标识重发仍是排队回显，不会执行。
    replay = platform.quench(oven="oven04", request_id="r-q-wait")
    assert replay["state"] == "queued" and replay["duplicate"] is True
    assert platform.quench_car.snapshot().spray_complete is False

    pushed = platform.push(oven="oven04", request_id="r-q-push")
    assert pushed["state"] == "complete"
    done = platform.command("r-q-wait")
    assert done["state"] == "complete"
    assert done["duplicate"] is False
    assert platform.commands.pending_count() == 0


def test_quench_wrong_oven_is_rejected_not_queued(platform):
    platform.start()
    platform.push(oven="oven04", request_id="r-wrong-push")
    # 熄焦车接的是 oven04，申请 oven05：炉号不符属不可排队原因，当场拒绝。
    result = platform.quench(oven="oven05", request_id="r-wrong-q")
    assert result["state"] == "rejected"
    assert result["reason"]["code"] == "quench_oven_mismatch"


def test_reverse_queues_until_due_then_cycle_releases(platform, clock):
    platform.start()
    waiting = platform.reverse(oven="oven01", request_id="r-rev-wait")
    assert waiting["state"] == "queued"
    assert waiting["waiting_for"] == "reversal_not_due"
    assert platform.reversal.count() == 0

    assert platform.cycle(dt=1.0)["released"] == []
    clock.advance(platform.config.heating.reversal_interval_s)
    cycle = platform.cycle(dt=1.0)
    assert "r-rev-wait" in cycle["released"]
    assert platform.command("r-rev-wait")["state"] == "complete"
    assert platform.reversal.count() == 1


def test_queued_command_survives_readback_failure(platform):
    platform.start()
    platform.quench(oven="oven06", request_id="r-q-fault")
    platform.inject_readback_fault("quench")
    platform.cycle(dt=1.0)
    # 回读失败期间：不丢、不执行，保持排队且原因切到回读失败。
    waiting = platform.command("r-q-fault")
    assert waiting["state"] == "queued"
    assert waiting["waiting_for"] == "state_readback_failed"
    assert platform.quench_car.snapshot().spray_complete is False

    platform.inject_readback_fault(clear=True)
    platform.push(oven="oven06", request_id="r-q-fault-push")
    assert platform.command("r-q-fault")["state"] == "complete"


def test_immediate_action_rejected_on_readback_failure(platform):
    platform.start()
    platform.inject_readback_fault("pusher")
    result = platform.push(oven="oven08", request_id="r-push-fault")
    assert result["state"] == "rejected"
    assert result["reason"]["code"] == "state_readback_failed"
    assert platform.pusher.snapshot().strokes == 0
    platform.inject_readback_fault(clear=True)


def test_rejection_reason_and_snapshot_share_one_audit_record(platform):
    platform.start()
    platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-audit-good"
    )
    # 翻板闩锁已置位：第二次装煤当场拒绝。
    rejected = platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0, request_id="r-audit-bad"
    )
    assert rejected["reason"]["code"] == "flap_latch_engaged"

    records = platform.audit.records(limit=400)
    progress = [record for record in records if record.action == "command_progress"]
    rejected_records = [
        record
        for record in progress
        if record.payload.get("request_id") == "r-audit-bad" and record.payload.get("state") == "rejected"
    ]
    assert rejected_records
    record = rejected_records[-1]
    assert record.severity == "alarm"
    # 同一条审计记录里：拒绝原因码与冻结快照同时在场，口径只有一份。
    assert record.payload["reason_code"] == "flap_latch_engaged"
    assert record.payload["snapshot_basis"] == "frozen"
    assert record.payload["snapshot"]["flap_engaged"] is True


def test_queue_and_records_persist_across_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    config = default_config()
    first = build_platform(config, clock=clock, storage_root=storage)
    first.start()
    first.quench(oven="oven09", request_id="r-q-restart")
    assert first.commands.pending_count() == 1

    second = build_platform(config, clock=clock, storage_root=storage)
    second.start()
    assert second.commands.pending_count() == 1
    assert second.command("r-q-restart")["state"] == "queued"
    second.push(oven="oven09", request_id="r-q-restart-push")
    assert second.command("r-q-restart")["state"] == "complete"


def test_empty_snapshot_is_conservative_baseline():
    # 回读失败时未读到的字段取保守值：风机停、推焦机空闲、翻板已置位等，
    # 配合 readback_failures 让所有动作都被按不允许拦下。
    broken = readback_failure(("dust", "pusher", "guide", "gas", "quench", "heating", "door"))
    assert broken.readback_ok is False
    assert evaluate("charge", broken, "oven01").code == "state_readback_failed"
    assert evaluate("push", broken, "oven01").code == "state_readback_failed"
    assert evaluate("clean", broken, "oven01").code == "state_readback_failed"
