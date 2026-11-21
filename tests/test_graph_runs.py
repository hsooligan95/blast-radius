"""End-to-end graph runs (in-process moto): happy path, rollback, abort."""

from pathlib import Path

from blastradius.audit import read_events
from blastradius.cloud import snapshot
from conftest import run_graph


def test_idle_run_completes_and_verifies(idle_seed, ec2, tmp_path):
    _, result = run_graph(ec2, tmp_path, "run-idle", "idle-remediation")
    assert result["status"] == "completed"
    assert result["verify_passed"] is True

    snap = snapshot(ec2)
    # Orphans deleted, protected + attached survive, idle stopped, active up.
    assert idle_seed["volumes"]["orphan_data"] not in snap["volumes"]
    assert idle_seed["volumes"]["orphan_scratch"] not in snap["volumes"]
    assert idle_seed["volumes"]["protected"] in snap["volumes"]
    assert idle_seed["volumes"]["attached"] in snap["volumes"]
    assert snap["instances"][idle_seed["instances"]["idle"]]["state"] == "stopped"
    assert snap["instances"][idle_seed["instances"]["active"]]["state"] == "running"

    events = read_events(Path(tmp_path) / "run-idle" / "audit.jsonl")
    kinds = [e["event"] for e in events]
    assert "plan_created" in kinds and "approval_granted" in kinds
    assert "verify_passed" in kinds and "run_finished" in kinds
    assert all(e["approved_by"] for e in events if e["event"] == "step_applied")


def test_failed_deployment_rolls_back_to_exact_original_state(
        deployment_seed, ec2, tmp_path):
    before = snapshot(ec2)
    _, result = run_graph(ec2, tmp_path, "run-deploy", "failed-deployment")

    assert result["status"] == "rolled_back"
    assert result["verify_passed"] is False
    # The health gate on node-3 is the failing check.
    failed = [c for c in result["verify_results"] if not c["passed"]]
    assert len(failed) == 1 and "healthy" in failed[0]["check"]
    # Proof, not vibes: post-rollback state equals the pre-run snapshot,
    # resource for resource, tag for tag.
    assert snapshot(ec2) == before

    events = read_events(Path(tmp_path) / "run-deploy" / "audit.jsonl")
    kinds = [e["event"] for e in events]
    assert "verify_failed" in kinds
    assert kinds.count("rollback_step") >= 3  # node-2 start+retag, node-3 restart
    assert kinds[-1] == "run_finished"


def test_denied_approval_mutates_nothing(deployment_seed, ec2, tmp_path):
    before = snapshot(ec2)
    _, result = run_graph(ec2, tmp_path, "run-deny", "failed-deployment",
                          approve=False)
    assert result["status"] == "aborted"
    assert snapshot(ec2) == before
