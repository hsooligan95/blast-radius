"""Inverse recording: every mutating step must record its exact undo."""

from pathlib import Path

from blastradius.actions import StepExecutor
from blastradius.audit import AuditLog
from blastradius.cloud import get_instance, tags_of
from blastradius.policy import Policy
from blastradius.scenarios import seed_failed_deployment

POLICIES = Path(__file__).resolve().parent.parent / "policies"


def _executor(ec2, tmp_path, policy_file):
    audit = AuditLog(Path(tmp_path) / "audit.jsonl", run_id="t", actor="pytest")
    return StepExecutor(ec2, Policy.load(POLICIES / policy_file), audit,
                        approved_by="pytest")


def test_stop_instance_records_start_inverse(idle_seed, ec2, tmp_path):
    ex = _executor(ec2, tmp_path, "idle-remediation-policy.json")
    step = {"action": "stop_instance", "resource_id": idle_seed["instances"]["idle"],
            "resource_type": "instance", "params": {}, "description": "stop"}
    outcome = ex.apply(step)
    assert outcome["inverse"]["action"] == "start_instance"
    assert outcome["inverse"]["resource_id"] == step["resource_id"]
    assert get_instance(ec2, step["resource_id"])["State"]["Name"] == "stopped"


def test_delete_volume_records_full_spec(idle_seed, ec2, tmp_path):
    ex = _executor(ec2, tmp_path, "idle-remediation-policy.json")
    vid = idle_seed["volumes"]["orphan_data"]
    step = {"action": "delete_volume", "resource_id": vid,
            "resource_type": "volume", "params": {}, "description": "delete"}
    outcome = ex.apply(step)
    inverse = outcome["inverse"]
    assert inverse["action"] == "create_volume"
    assert inverse["params"]["size"] == 20
    assert inverse["params"]["type"] == "gp3"
    assert inverse["params"]["az"] == "us-east-1a"
    assert inverse["params"]["tags"]["Name"] == "orphan-data"


def test_set_tags_records_previous_values(deployment_seed, ec2, tmp_path):
    ex = _executor(ec2, tmp_path, "deployment-recovery-policy.json")
    node2 = deployment_seed["instances"]["node2"]
    step = {"action": "set_tags", "resource_id": node2, "resource_type": "instance",
            "params": {"tags": {"version": "2.0"}}, "description": "retag"}
    outcome = ex.apply(step)
    assert outcome["inverse"]["action"] == "restore_tags"
    assert outcome["inverse"]["params"]["tags"] == {"version": "1.0"}
    assert outcome["inverse"]["params"]["delete_keys"] == []
    assert tags_of(get_instance(ec2, node2))["version"] == "2.0"


def test_health_check_with_fault_mutates_nothing(deployment_seed, ec2, tmp_path):
    ex = _executor(ec2, tmp_path, "deployment-recovery-policy.json")
    node3 = deployment_seed["instances"]["node3"]
    step = {"action": "health_check", "resource_id": node3,
            "resource_type": "instance", "params": {}, "description": "gate"}
    outcome = ex.apply(step)
    assert outcome["result"] == "health_not_cleared"
    assert outcome["inverse"] is None
    assert tags_of(get_instance(ec2, node3))["health"] == "failed"


def test_health_check_without_fault_records_inverse(ec2, tmp_path, aws):
    seed = seed_failed_deployment(ec2)
    node2 = seed["instances"]["node2"]
    ex = _executor(ec2, tmp_path, "deployment-recovery-policy.json")
    step = {"action": "health_check", "resource_id": node2,
            "resource_type": "instance", "params": {}, "description": "gate"}
    outcome = ex.apply(step)
    assert outcome["result"] == "ok"
    assert tags_of(get_instance(ec2, node2))["health"] == "healthy"
    assert outcome["inverse"]["params"]["tags"] == {"health": "unknown"}


def test_executor_denies_protected_volume(idle_seed, ec2, tmp_path):
    from blastradius.policy import PolicyDenied
    import pytest

    ex = _executor(ec2, tmp_path, "idle-remediation-policy.json")
    step = {"action": "delete_volume", "resource_id": idle_seed["volumes"]["protected"],
            "resource_type": "volume", "params": {}, "description": "delete protected"}
    with pytest.raises(PolicyDenied):
        ex.apply(step)
