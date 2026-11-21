"""Least-privilege enforcement: default deny, explicit deny, tag conditions."""

from pathlib import Path

from blastradius.cloud import instance_arn, volume_arn
from blastradius.policy import Policy

POLICIES = Path(__file__).resolve().parent.parent / "policies"


def test_terminate_is_never_allowed():
    policy = Policy.load(POLICIES / "idle-remediation-policy.json")
    allowed, reason = policy.allows(
        "ec2:TerminateInstances", instance_arn("i-abc"), {"idle": "true"})
    assert not allowed
    assert "default deny" in reason


def test_delete_volume_tag_condition():
    policy = Policy.load(POLICIES / "idle-remediation-policy.json")
    allowed, _ = policy.allows(
        "ec2:DeleteVolume", volume_arn("vol-1"), {"protected": "false"})
    assert allowed
    allowed, reason = policy.allows(
        "ec2:DeleteVolume", volume_arn("vol-2"), {"protected": "true"})
    assert not allowed


def test_stop_instance_requires_idle_tag():
    policy = Policy.load(POLICIES / "idle-remediation-policy.json")
    allowed, _ = policy.allows(
        "ec2:StopInstances", instance_arn("i-1"), {"idle": "true"})
    assert allowed
    allowed, _ = policy.allows(
        "ec2:StopInstances", instance_arn("i-2"), {"idle": "false"})
    assert not allowed


def test_deployment_policy_scoped_to_service():
    policy = Policy.load(POLICIES / "deployment-recovery-policy.json")
    allowed, _ = policy.allows(
        "ec2:StartInstances", instance_arn("i-1"), {"service": "checkout-api"})
    assert allowed
    # Same action, different service: denied. The agent's identity is
    # scoped to checkout-api and nothing else.
    allowed, _ = policy.allows(
        "ec2:StartInstances", instance_arn("i-2"), {"service": "billing"})
    assert not allowed
    # And it cannot delete volumes at all under this policy.
    allowed, _ = policy.allows(
        "ec2:DeleteVolume", volume_arn("vol-1"), {"service": "checkout-api"})
    assert not allowed
