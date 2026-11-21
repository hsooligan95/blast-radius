"""Planner shape: the right steps, in the right order, traps untouched."""

from blastradius.planner import plan_failed_deployment, plan_idle_remediation


def test_idle_plan_targets_only_waste(idle_seed, ec2):
    plan = plan_idle_remediation(ec2)
    actions = [(s["action"], s["resource_id"]) for s in plan["steps"]]

    assert actions == [
        ("delete_volume", idle_seed["volumes"]["orphan_data"]),
        ("delete_volume", idle_seed["volumes"]["orphan_scratch"]),
        ("stop_instance", idle_seed["instances"]["idle"]),
    ]
    # Every step is a complete, reviewable record.
    for step in plan["steps"]:
        assert step["description"]
        assert step["resource_type"] in ("instance", "volume")
        assert "params" in step
    # The traps are reported as findings, never as steps.
    findings = " ".join(plan["findings"])
    assert "protected=true" in findings
    assert "attached" in findings
    assert idle_seed["volumes"]["protected"] not in [s["resource_id"] for s in plan["steps"]]
    assert idle_seed["instances"]["active"] not in [s["resource_id"] for s in plan["steps"]]


def test_deployment_plan_shape(deployment_seed, ec2):
    plan = plan_failed_deployment(ec2)
    by_resource: dict[str, list[str]] = {}
    for step in plan["steps"]:
        by_resource.setdefault(step["resource_id"], []).append(step["action"])

    node1 = deployment_seed["instances"]["node1"]
    node2 = deployment_seed["instances"]["node2"]
    node3 = deployment_seed["instances"]["node3"]

    # node-1 is already compliant: findings only, no steps.
    assert node1 not in by_resource
    assert any(node1 in f for f in plan["findings"])
    # node-2: start, retag to 2.0, health gate — in that order.
    assert by_resource[node2] == ["start_instance", "set_tags", "health_check"]
    retag = [s for s in plan["steps"]
             if s["resource_id"] == node2 and s["action"] == "set_tags"][0]
    assert retag["params"]["tags"] == {"version": "2.0"}
    # node-3: restart (stop, start), then the health gate.
    assert by_resource[node3] == ["stop_instance", "start_instance", "health_check"]


def test_plans_are_deterministic(idle_seed, ec2):
    assert plan_idle_remediation(ec2) == plan_idle_remediation(ec2)
