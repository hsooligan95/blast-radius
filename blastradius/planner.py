"""Deterministic planners and verifiers.

The planner is deliberately *not* an LLM. Given the live cloud state it
produces a structured, reviewable plan: an ordered list of steps, each with
an action, a target resource, parameters, and a human-readable description
— plus findings for resources it inspected and deliberately left alone.
A human approves exactly this list; the executor runs exactly this list.

Verifiers express the post-conditions each scenario must satisfy after
execution. Verification reads live state only — never the agent's own
claims about what it did.
"""

from __future__ import annotations

from .cloud import get_instance, snapshot, tags_of
from .scenarios import SERVICE, TARGET_VERSION

# --------------------------------------------------------------------------
# Plan construction helpers
# --------------------------------------------------------------------------


def _step(action: str, resource_id: str, resource_type: str,
          description: str, params: dict | None = None) -> dict:
    return {
        "action": action,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "params": params or {},
        "description": description,
    }


# --------------------------------------------------------------------------
# Scenario (a): idle / orphaned resource remediation
# --------------------------------------------------------------------------


def plan_idle_remediation(ec2) -> dict:
    snap = snapshot(ec2)
    steps: list[dict] = []
    findings: list[str] = []

    for vid, vol in sorted(snap["volumes"].items(), key=lambda kv: kv[1]["tags"].get("Name", kv[0])):
        name = vol["tags"].get("Name", vid)
        if vol["attachments"]:
            label = vol["tags"].get("Name") or f"root volume of {vol['attachments'][0]}"
            findings.append(f"volume {label} ({vid}): attached to {vol['attachments']} — left alone")
        elif vol["tags"].get("protected") == "true":
            findings.append(f"volume {name} ({vid}): unattached but tagged protected=true — left alone")
        else:
            steps.append(_step(
                "delete_volume", vid, "volume",
                f"Delete unattached volume {name} ({vid}, {vol['size']} GiB {vol['type']})",
            ))

    for iid, inst in sorted(snap["instances"].items(), key=lambda kv: kv[1]["tags"].get("Name", kv[0])):
        name = inst["tags"].get("Name", iid)
        if inst["state"] == "running" and inst["tags"].get("idle") == "true":
            steps.append(_step(
                "stop_instance", iid, "instance",
                f"Stop idle instance {name} ({iid})",
            ))
        else:
            findings.append(
                f"instance {name} ({iid}): state={inst['state']} idle={inst['tags'].get('idle')} — left alone"
            )

    return {"steps": steps, "findings": findings}


def verify_idle_remediation(ec2) -> list[dict]:
    snap = snapshot(ec2)
    checks: list[dict] = []

    remaining_orphans = [
        vid for vid, v in snap["volumes"].items()
        if not v["attachments"] and v["tags"].get("protected") != "true"
    ]
    checks.append({
        "check": "no unattached unprotected volumes remain",
        "expected": "0",
        "actual": str(len(remaining_orphans)),
        "passed": len(remaining_orphans) == 0,
    })

    protected_present = any(
        v["tags"].get("protected") == "true" for v in snap["volumes"].values()
    )
    checks.append({
        "check": "protected volume still exists",
        "expected": "present",
        "actual": "present" if protected_present else "MISSING",
        "passed": protected_present,
    })

    for iid, inst in snap["instances"].items():
        name = inst["tags"].get("Name", iid)
        if inst["tags"].get("idle") == "true":
            checks.append({
                "check": f"idle instance {name} is stopped",
                "expected": "stopped",
                "actual": inst["state"],
                "passed": inst["state"] == "stopped",
            })
        if inst["tags"].get("idle") == "false":
            checks.append({
                "check": f"active instance {name} is still running",
                "expected": "running",
                "actual": inst["state"],
                "passed": inst["state"] == "running",
            })
    return checks


# --------------------------------------------------------------------------
# Scenario (b): failed deployment recovery
# --------------------------------------------------------------------------


def plan_failed_deployment(ec2) -> dict:
    snap = snapshot(ec2)
    steps: list[dict] = []
    findings: list[str] = []

    service_instances = {
        iid: inst for iid, inst in snap["instances"].items()
        if inst["tags"].get("service") == SERVICE
    }
    for iid, inst in sorted(service_instances.items(), key=lambda kv: kv[1]["tags"].get("Name", kv[0])):
        tags = inst["tags"]
        name = tags.get("Name", iid)
        needs_start = inst["state"] != "running"
        needs_retag = tags.get("version") != TARGET_VERSION
        needs_health = tags.get("health") != "healthy"

        if not (needs_start or needs_retag or needs_health):
            findings.append(f"instance {name} ({iid}): already at target state — left alone")
            continue

        if needs_health and not needs_start:
            # A running-but-unhealthy node is restarted before the health
            # gate: a reboot clears transient faults. Persistent faults
            # (fault=* tag) will still fail the gate — by design.
            steps.append(_step("stop_instance", iid, "instance",
                               f"Stop unhealthy instance {name} ({iid}) for restart"))
            steps.append(_step("start_instance", iid, "instance",
                               f"Start instance {name} ({iid})"))
        elif needs_start:
            steps.append(_step("start_instance", iid, "instance",
                               f"Start instance {name} ({iid})"))
        if needs_retag:
            steps.append(_step(
                "set_tags", iid, "instance",
                f"Set version tag on {name} ({iid}) to {TARGET_VERSION}",
                params={"tags": {"version": TARGET_VERSION}},
            ))
        if needs_health:
            steps.append(_step(
                "health_check", iid, "instance",
                f"Health gate for {name} ({iid}): clear health to 'healthy' "
                "only if no persistent fault is present",
            ))

    return {"steps": steps, "findings": findings}


def verify_failed_deployment(ec2) -> list[dict]:
    snap = snapshot(ec2)
    checks: list[dict] = []
    service_instances = {
        iid: inst for iid, inst in snap["instances"].items()
        if inst["tags"].get("service") == SERVICE
    }
    for iid, inst in sorted(service_instances.items(), key=lambda kv: kv[1]["tags"].get("Name", kv[0])):
        name = inst["tags"].get("Name", iid)
        checks.append({
            "check": f"{name} is running",
            "expected": "running",
            "actual": inst["state"],
            "passed": inst["state"] == "running",
        })
        checks.append({
            "check": f"{name} is on version {TARGET_VERSION}",
            "expected": TARGET_VERSION,
            "actual": inst["tags"].get("version", "<none>"),
            "passed": inst["tags"].get("version") == TARGET_VERSION,
        })
        checks.append({
            "check": f"{name} is healthy",
            "expected": "healthy",
            "actual": inst["tags"].get("health", "<none>"),
            "passed": inst["tags"].get("health") == "healthy",
        })
    return checks


PLANNERS = {
    "idle-remediation": plan_idle_remediation,
    "failed-deployment": plan_failed_deployment,
}

VERIFIERS = {
    "idle-remediation": verify_idle_remediation,
    "failed-deployment": verify_failed_deployment,
}
