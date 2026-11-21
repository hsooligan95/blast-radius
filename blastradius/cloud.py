"""Cloud access layer: boto3 client factory, resource helpers, snapshots.

The agent talks to AWS exclusively through boto3. Two backends are used:

  * a moto *server* process (BLASTRADIUS_AWS_ENDPOINT=http://127.0.0.1:PORT)
    for demos and end-to-end tests — the cloud outlives the agent process,
    which is what makes kill -9 / resume an honest crash test;
  * in-process moto (mock_aws) for unit tests, where no endpoint is set.

Snapshots are plain dicts of the resources BlastRadius manages, so two
snapshots can be compared with == to *prove* a rollback restored the exact
original state (same IDs, states, and tags).
"""

from __future__ import annotations

import os

import boto3

ACCOUNT_ID = "123456789012"  # moto's default account id


def region() -> str:
    return os.environ.get("BLASTRADIUS_AWS_REGION", "us-east-1")


def ec2_client():
    kwargs: dict = {"region_name": region()}
    endpoint = os.environ.get("BLASTRADIUS_AWS_ENDPOINT")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("ec2", **kwargs)


def tags_of(resource: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


def instance_arn(instance_id: str) -> str:
    return f"arn:aws:ec2:{region()}:{ACCOUNT_ID}:instance/{instance_id}"


def volume_arn(volume_id: str) -> str:
    return f"arn:aws:ec2:{region()}:{ACCOUNT_ID}:volume/{volume_id}"


def get_instance(ec2, instance_id: str) -> dict | None:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    for reservation in resp["Reservations"]:
        for inst in reservation["Instances"]:
            return inst
    return None


def get_volume(ec2, volume_id: str) -> dict | None:
    try:
        resp = ec2.describe_volumes(VolumeIds=[volume_id])
    except ec2.exceptions.ClientError:
        return None
    volumes = resp["Volumes"]
    return volumes[0] if volumes else None


def snapshot(ec2) -> dict:
    """Full managed-state snapshot: instances and volumes by ID."""
    instances: dict[str, dict] = {}
    resp = ec2.describe_instances()
    for reservation in resp["Reservations"]:
        for inst in reservation["Instances"]:
            instances[inst["InstanceId"]] = {
                "state": inst["State"]["Name"],
                "type": inst["InstanceType"],
                "tags": tags_of(inst),
            }
    volumes: dict[str, dict] = {}
    resp = ec2.describe_volumes()
    for vol in resp["Volumes"]:
        volumes[vol["VolumeId"]] = {
            "state": vol["State"],
            "size": vol["Size"],
            "type": vol["VolumeType"],
            "az": vol["AvailabilityZone"],
            "tags": tags_of(vol),
            "attachments": sorted(a["InstanceId"] for a in vol.get("Attachments", [])),
        }
    return {"instances": instances, "volumes": volumes}


def diff_snapshots(before: dict, after: dict) -> list[str]:
    """Human-readable list of differences; empty list means identical."""
    diffs: list[str] = []
    for section in ("instances", "volumes"):
        b, a = before.get(section, {}), after.get(section, {})
        for rid in sorted(set(b) | set(a)):
            if rid not in b:
                diffs.append(f"{section} {rid}: only in AFTER")
            elif rid not in a:
                diffs.append(f"{section} {rid}: only in BEFORE")
            elif b[rid] != a[rid]:
                for key in sorted(set(b[rid]) | set(a[rid])):
                    if b[rid].get(key) != a[rid].get(key):
                        diffs.append(
                            f"{section} {rid}.{key}: {b[rid].get(key)!r} != {a[rid].get(key)!r}"
                        )
    return diffs
