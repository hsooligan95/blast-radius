"""Step executor: policy-checked mutations with recorded inverses.

For every mutating step the executor, in order:

  1. resolves the target resource's live tags and ARN;
  2. asks the scenario policy whether the AWS action is allowed on this
     resource with these tags — and audits the decision either way;
  3. captures the resource's pre-state and computes the *inverse action*
     that would undo this step;
  4. issues the boto3 call;
  5. returns the inverse, which the graph stores in its durable checkpoint
     *before* the next step runs.

Rollback is then mechanical: replay the recorded inverses in reverse
order. Inverses are data, computed from live state at execution time —
never guessed by the planner.
"""

from __future__ import annotations

import time

from .audit import AuditLog
from .cloud import get_instance, get_volume, instance_arn, tags_of, volume_arn
from .policy import Policy, PolicyDenied

# BlastRadius action -> AWS API actions it needs policy permission for.
ACTION_PERMISSIONS = {
    "start_instance": ["ec2:StartInstances"],
    "stop_instance": ["ec2:StopInstances"],
    "set_tags": ["ec2:CreateTags"],
    "restore_tags": ["ec2:CreateTags", "ec2:DeleteTags"],
    "delete_volume": ["ec2:DeleteVolume"],
    "create_volume": ["ec2:CreateVolume", "ec2:CreateTags"],
    "health_check": ["ec2:CreateTags"],
}


class StepExecutor:
    def __init__(self, ec2, policy: Policy, audit: AuditLog,
                 approved_by: str | None, step_delay: float = 0.0):
        self.ec2 = ec2
        self.policy = policy
        self.audit = audit
        self.approved_by = approved_by
        self.step_delay = step_delay

    # -- internal helpers ---------------------------------------------------

    def _resource_context(self, step: dict) -> tuple[str, dict[str, str]]:
        rid = step["resource_id"]
        if step["resource_type"] == "instance":
            inst = get_instance(self.ec2, rid)
            if inst is None:
                raise ValueError(f"instance {rid} not found")
            return instance_arn(rid), tags_of(inst)
        vol = get_volume(self.ec2, rid)
        if vol is None:
            raise ValueError(f"volume {rid} not found")
        return volume_arn(rid), tags_of(vol)

    def _check_policy(self, action: str, step: dict,
                      arn: str | None = None, tags: dict | None = None) -> None:
        if arn is None:
            arn, tags = self._resource_context(step)
        for aws_action in ACTION_PERMISSIONS[action]:
            allowed, reason = self.policy.allows(aws_action, arn, tags)
            self.audit.log(
                "policy_decision", action=aws_action, resource=step["resource_id"],
                approved_by=self.approved_by,
                result="allow" if allowed else "deny", detail=reason,
            )
            if not allowed:
                raise PolicyDenied(aws_action, step["resource_id"], reason)

    def _delay(self) -> None:
        # Used by the crash demo: a slow step widens the window in which
        # kill -9 lands *between* two checkpointed steps.
        if self.step_delay:
            time.sleep(self.step_delay)

    # -- forward actions ------------------------------------------------------

    def apply(self, step: dict) -> dict:
        """Execute one plan step. Returns {inverse, result, detail}."""
        action = step["action"]
        handler = getattr(self, f"_do_{action}")
        self._check_policy(action, step)
        self._delay()
        return handler(step)

    def _do_start_instance(self, step: dict) -> dict:
        rid = step["resource_id"]
        inst = get_instance(self.ec2, rid)
        pre_state = inst["State"]["Name"]
        inverse = None
        if pre_state != "running":
            inverse = {"action": "stop_instance", "resource_id": rid,
                       "resource_type": "instance", "params": {},
                       "description": f"Rollback: stop instance {rid}"}
            self.ec2.start_instances(InstanceIds=[rid])
        return {"inverse": inverse, "result": "ok",
                "detail": f"instance {rid}: {pre_state} -> running"}

    def _do_stop_instance(self, step: dict) -> dict:
        rid = step["resource_id"]
        inst = get_instance(self.ec2, rid)
        pre_state = inst["State"]["Name"]
        inverse = None
        if pre_state != "stopped":
            inverse = {"action": "start_instance", "resource_id": rid,
                       "resource_type": "instance", "params": {},
                       "description": f"Rollback: start instance {rid}"}
            self.ec2.stop_instances(InstanceIds=[rid])
        return {"inverse": inverse, "result": "ok",
                "detail": f"instance {rid}: {pre_state} -> stopped"}

    def _do_set_tags(self, step: dict) -> dict:
        rid = step["resource_id"]
        new_tags: dict[str, str] = step["params"]["tags"]
        resource = (get_instance(self.ec2, rid) if step["resource_type"] == "instance"
                    else get_volume(self.ec2, rid))
        current = tags_of(resource)
        old_values = {k: current[k] for k in new_tags if k in current}
        absent_keys = [k for k in new_tags if k not in current]
        self.ec2.create_tags(
            Resources=[rid],
            Tags=[{"Key": k, "Value": v} for k, v in new_tags.items()],
        )
        inverse = {"action": "restore_tags", "resource_id": rid,
                   "resource_type": step["resource_type"],
                   "params": {"tags": old_values, "delete_keys": absent_keys},
                   "description": f"Rollback: restore tags on {rid}"}
        return {"inverse": inverse, "result": "ok",
                "detail": f"tags on {rid}: set {new_tags} (was {old_values or '<unset>'})"}

    def _do_delete_volume(self, step: dict) -> dict:
        rid = step["resource_id"]
        vol = get_volume(self.ec2, rid)
        spec = {
            "size": vol["Size"], "type": vol["VolumeType"],
            "az": vol["AvailabilityZone"], "tags": tags_of(vol),
        }
        inverse = {"action": "create_volume", "resource_id": rid,
                   "resource_type": "volume", "params": spec,
                   "description": f"Rollback: recreate volume (was {rid}) from recorded spec"}
        self.ec2.delete_volume(VolumeId=rid)
        return {"inverse": inverse, "result": "ok",
                "detail": f"volume {rid} deleted ({spec['size']} GiB {spec['type']})"}

    def _do_health_check(self, step: dict) -> dict:
        rid = step["resource_id"]
        inst = get_instance(self.ec2, rid)
        tags = tags_of(inst)
        fault = tags.get("fault")
        if fault:
            # The health gate refuses to clear a persistent fault. The step
            # itself succeeds (it did its job); verification will fail.
            return {"inverse": None, "result": "health_not_cleared",
                    "detail": f"instance {rid}: persistent fault '{fault}' present — "
                              "health left as 'failed'"}
        old_health = tags.get("health")
        self.ec2.create_tags(Resources=[rid], Tags=[{"Key": "health", "Value": "healthy"}])
        inverse = None
        if old_health is not None:
            inverse = {"action": "restore_tags", "resource_id": rid,
                       "resource_type": "instance",
                       "params": {"tags": {"health": old_health}, "delete_keys": []},
                       "description": f"Rollback: restore health tag on {rid}"}
        return {"inverse": inverse, "result": "ok",
                "detail": f"instance {rid}: health {old_health!r} -> 'healthy'"}

    # -- inverse actions (rollback) ------------------------------------------

    def apply_inverse(self, inverse: dict) -> dict:
        # Rollback is policy-checked too: least privilege applies on the
        # way back. A recreated volume's original no longer exists, so its
        # check runs against the recorded spec's tags.
        action = inverse["action"]
        if action == "create_volume":
            self._check_policy(action, inverse,
                               arn=volume_arn(inverse["resource_id"]),
                               tags=inverse["params"].get("tags", {}))
            return self._do_create_volume(inverse)
        self._check_policy(action, inverse)
        if action in ("start_instance", "stop_instance"):
            return getattr(self, f"_do_{action}")(inverse)
        if action == "restore_tags":
            return self._do_restore_tags(inverse)
        if action == "create_volume":
            return self._do_create_volume(inverse)
        raise ValueError(f"unknown inverse action {action!r}")

    def _do_restore_tags(self, inverse: dict) -> dict:
        rid = inverse["resource_id"]
        old_tags: dict[str, str] = inverse["params"].get("tags", {})
        delete_keys: list[str] = inverse["params"].get("delete_keys", [])
        if old_tags:
            self.ec2.create_tags(
                Resources=[rid],
                Tags=[{"Key": k, "Value": v} for k, v in old_tags.items()],
            )
        if delete_keys:
            self.ec2.delete_tags(
                Resources=[rid],
                Tags=[{"Key": k} for k in delete_keys],
            )
        return {"inverse": None, "result": "ok",
                "detail": f"tags on {rid} restored to {old_tags}, removed {delete_keys}"}

    def _do_create_volume(self, inverse: dict) -> dict:
        spec = inverse["params"]
        resp = self.ec2.create_volume(
            AvailabilityZone=spec["az"], Size=spec["size"], VolumeType=spec["type"]
        )
        new_id = resp["VolumeId"]
        if spec.get("tags"):
            self.ec2.create_tags(
                Resources=[new_id],
                Tags=[{"Key": k, "Value": v} for k, v in spec["tags"].items()],
            )
        return {"inverse": None, "result": "ok",
                "detail": f"volume recreated as {new_id} "
                          f"({spec['size']} GiB {spec['type']}, tags restored; "
                          "a recreated volume always gets a fresh ID)"}
