"""Shared fixtures: in-process moto cloud, seeded scenarios, run helper.

Unit/integration tests use moto's mock_aws (in-process) — no endpoint —
while the crash/resume test uses a real moto *server* subprocess so the
cloud survives the agent process being killed.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.pop("BLASTRADIUS_AWS_ENDPOINT", None)

import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from blastradius.cloud import ec2_client  # noqa: E402
from blastradius.graph import build_graph, thread_config  # noqa: E402
from blastradius.scenarios import seed_failed_deployment, seed_idle_remediation  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLICIES_DIR = PROJECT_ROOT / "policies"


@pytest.fixture()
def aws():
    with mock_aws():
        yield


@pytest.fixture()
def ec2(aws):
    return ec2_client()


@pytest.fixture()
def idle_seed(ec2):
    return seed_idle_remediation(ec2)


@pytest.fixture()
def deployment_seed(ec2):
    return seed_failed_deployment(ec2)


def run_graph(ec2, tmp_path, run_id: str, scenario: str, approve: bool = True):
    """Drive a full run in-process: invoke, then resume the approval."""
    from langgraph.types import Command

    graph = build_graph(ec2, Path(tmp_path), run_id, scenario, POLICIES_DIR)
    config = thread_config(run_id)
    result = graph.invoke(
        {"run_id": run_id, "scenario": scenario, "actor": "pytest"}, config)
    assert "__interrupt__" in result, "run should pause for approval"
    result = graph.invoke(
        Command(resume={"approved": approve, "approved_by": "pytest"}), config)
    return graph, result
