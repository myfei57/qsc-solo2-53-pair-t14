"""提交控制：幂等标识、按动作的准入/排队、统一拒绝原因与审计。"""

from __future__ import annotations

import threading

import pytest

from coke.app import build_platform
from coke.config import default_config
from coke.console.api import Api
from coke.control.policy import STATE_BASIS
from coke.errors import IdempotencyError


def test_duplicate_request_id_executes_once(platform):
    platform.start()
    first = platform.push(oven="oven01", request_id="req-push-once-1")
    second = platform.push(oven="oven01", request_id="req-push-once-1")
    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["idempotent"] is True
    assert first["op_id"] == second["op_id"]
    # 推焦机只记录了一次行程。
    assert platform.pusher.snapshot().strokes == 1
    assert platform.status()["operations"] == 1


def test_duplicate_rejection_is_replayed_not_reevaluated(platform):
    platform.start()
    rejected = platform.push(oven="oven01", guide_measured_mm=90.0, request_id="req-reject-1")
    replay = platform.push(oven="oven01", guide_measured_mm=90.0, request_id="req-reject-1")
    assert rejected["status"] == "rejected"
    assert rejected["reason"]["code"] == "guide_not_aligned"
    assert replay["idempotent"] is True
    assert replay["status"] == "rejected"
    assert replay["reason"]["code"] == "guide_not_aligned"


def test_concurrent_same_request_id_single_execution(platform):
    platform.start()
    barrier = threading.Barrier(8)
    outcomes: list[dict] = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(platform.quench(oven="oven01", request_id="req-concurrent-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 8
    assert sum(1 for item in outcomes if item["accepted"]) == 8
    assert sum(1 for item in outcomes if item.get("idempotent")) == 7
    op_ids = {item["op_id"] for item in outcomes}
    assert len(op_ids) == 1


def test_push_requires_running_fan(platform):
    # 未 start：除尘风机未运行，推焦当场拒绝，不执行任何步骤。
    result = platform.push(oven="oven01", request_id="req-no-fan-001")
    assert result["accepted"] is False
    assert result["status"] == "rejected"
    assert result["reason"]["code"] == "fan_not_running"
    assert result["steps"] == []
    assert platform.pusher.snapshot().strokes == 0


def test_reverse_before_due_is_rejected_not_queued(platform):
    platform.start()
    result = platform.reverse(oven="oven01", request_id="req-rev-early-1")
    assert result["status"] == "rejected"
    assert result["reason"]["code"] == "reversal_not_due"


def test_charge_queues_until_pressure_settles(platform):
    platform.start()
    platform.disturb_collector(-60.0)
    assert platform.collector.settled() is False
    result = platform.charge(oven="oven01", request_id="req-charge-q-1")
    # 装煤遇压力未稳按策略排队，闸口自己推进控制周期等到满足，然后成功。
    assert result["accepted"] is True
    assert result["status"] == "completed"
    assert platform.collector.settled() is True
    assert result["state_basis"] == STATE_BASIS


def test_hopper_misalignment_is_rejected_immediately(platform):
    platform.start()
    result = platform.charge(
        oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=60.0,
        request_id="req-hopper-bad-1",
    )
    assert result["status"] == "rejected"
    assert result["reason"]["code"] == "hopper_not_aligned"


def test_readback_failure_is_partial_failure_deny(platform):
    platform.start()
    platform.inject_readback_fault(["guide"])
    result = platform.push(oven="oven01", request_id="req-readback-01")
    platform.clear_readback_faults()
    assert result["status"] == "rejected"
    assert result["reason"]["code"] == "readback_failed"
    assert result["admission"]["failed_components"] == ["guide"]
    # 恢复后同参数的新提交可正常执行。
    assert platform.push(oven="oven01", request_id="req-readback-02")["accepted"] is True


def test_reason_state_and_audit_share_one_table(platform):
    platform.start()
    result = platform.push(oven="oven01", guide_measured_mm=90.0, request_id="req-consistent-1")
    code = result["reason"]["code"]
    alarms = [
        item for item in platform.audit.alarms()
        if item["action"] == "interlock_rejected"
        and item["payload"].get("request_id") == "req-consistent-1"
    ]
    assert alarms
    recorded = alarms[-1]
    assert recorded["payload"]["reason"]["code"] == code
    assert recorded["payload"]["reason"]["message"] == result["reason"]["message"]
    assert recorded["message"].endswith(result["reason"]["message"])
    # 策略表（控制台展示用）登记了同一个原因码。
    catalog = {item["code"] for item in platform.admission_policy()["reasons"]}
    assert code in catalog


def test_progress_reported_for_console(platform):
    platform.start()
    rejected = platform.push(oven="oven01", guide_measured_mm=90.0, request_id="req-prog-rej-1")
    completed = platform.push(oven="oven01", request_id="req-prog-ok-01")
    assert rejected["progress"] == 0.0
    assert completed["progress"] == 1.0
    assert completed["reason"] is None


def test_same_request_id_with_different_params_conflicts(platform):
    platform.start()
    platform.push(oven="oven01", request_id="req-conflict-01")
    with pytest.raises(IdempotencyError):
        platform.quench(oven="oven01", request_id="req-conflict-01")


def test_invalid_request_id_shape_rejected(platform):
    with pytest.raises(IdempotencyError):
        platform.push(oven="oven01", request_id="x")


def test_idempotency_survives_restart(tmp_path, clock):
    config = default_config()
    storage = str(tmp_path / "state")
    first = build_platform(config, clock=clock, storage_root=storage)
    first.start()
    first.push(oven="oven01", request_id="req-restart-001")
    assert first.pusher.snapshot().strokes == 1

    second = build_platform(config, clock=clock, storage_root=storage)
    replay = second.push(oven="oven01", request_id="req-restart-001")
    assert replay["idempotent"] is True
    assert second.pusher.snapshot().strokes == 0


def test_admission_preview_is_non_mutating(platform):
    platform.start()
    api = Api(platform)
    status, preview = api.handle(
        "GET", "/api/admission", {"action": "push"}, {"guide_measured_mm": 90.0}
    )
    assert status == 200
    assert preview["allowed"] is False
    assert preview["reason"]["code"] == "guide_not_aligned"
    # 只判定不执行：没有新增操作、没有行程。
    assert platform.pusher.snapshot().strokes == 0


def test_policy_endpoint_pins_queueing_by_action(platform):
    policy = platform.admission_policy()
    assert policy["state_basis"] == "live_current"
    assert policy["actions"]["charge"]["queueable_reasons"] == ["pressure_not_settled"]
    assert policy["actions"]["push"]["queueable_reasons"] == []
    assert policy["actions"]["reverse"]["queueable_reasons"] == []


def test_request_status_endpoint_tracks_lifecycle(platform):
    platform.start()
    platform.push(oven="oven01", request_id="req-track-0001")
    view = platform.request_status("req-track-0001")
    assert view["status"] == "completed"
    assert view["result"]["accepted"] is True
    listed = platform.requests_list()["requests"]
    assert any(item["request_id"] == "req-track-0001" for item in listed)


def test_missing_request_id_is_issued_by_platform(platform):
    platform.start()
    result = platform.quench(oven="oven01")
    assert result["accepted"] is True
    assert result["request_id"].startswith("req-")
