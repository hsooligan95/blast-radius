"""Crash/resume end-to-end: kill -9 the agent mid-run, resume, roll back.

Unlike the in-process tests, this runs the real CLI in subprocesses
against a moto *server* subprocess — so the cloud genuinely outlives the
agent process, exactly like a real deployment. The run is killed while a
step delay is in progress, i.e. between two checkpointed steps.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "run-crash"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/moto-api/", timeout=2)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("moto server did not come up")


def _cli(env: dict, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "blastradius.cli", *argv],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)


def _audit_events(runs_dir: Path) -> list[dict]:
    path = runs_dir / RUN_ID / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_kill_resume_rollback(tmp_path):
    port = _free_port()
    runs_dir = tmp_path / "runs"
    env = {
        **os.environ,
        "BLASTRADIUS_AWS_ENDPOINT": f"http://127.0.0.1:{port}",
        "BLASTRADIUS_RUNS_DIR": str(runs_dir),
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for_server(port)
        seed = _cli(env, "seed", "--scenario", "failed-deployment")
        assert seed.returncode == 0, seed.stderr
        initial = tmp_path / "initial.json"
        final = tmp_path / "final.json"
        assert _cli(env, "snapshot", "--out", str(initial)).returncode == 0

        # Start the run in the background: 3s delay before each mutation.
        proc = subprocess.Popen(
            [sys.executable, "-m", "blastradius.cli", "run",
             "--scenario", "failed-deployment", "--run-id", RUN_ID,
             "--auto-approve", "--step-delay", "3"],
            cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            # Wait until exactly two steps are durably applied, then kill
            # the process mid-run (during the third step's delay).
            deadline = time.time() + 90
            while time.time() < deadline:
                applied = [e for e in _audit_events(runs_dir)
                           if e["event"] == "step_applied"]
                if len(applied) >= 2:
                    break
                if proc.poll() is not None:
                    raise AssertionError("agent exited before the kill point")
                time.sleep(0.25)
            else:
                raise AssertionError("timed out waiting for 2 applied steps")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

        # Mid-crash state: node-2 is started + retagged, node-3 untouched.
        mid = json.loads(_cli(env, "snapshot").stdout)
        by_name = {v["tags"].get("Name"): v for v in mid["instances"].values()}
        assert by_name["checkout-api-2"]["state"] == "running"
        assert by_name["checkout-api-2"]["tags"]["version"] == "2.0"
        assert by_name["checkout-api-3"]["tags"]["health"] == "failed"

        # A fresh process resumes from the SQLite checkpoint.
        resumed = _cli(env, "resume", "--run-id", RUN_ID)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert "rolled_back" in resumed.stdout

        assert _cli(env, "snapshot", "--out", str(final)).returncode == 0
        compared = _cli(env, "compare", "--a", str(initial), "--b", str(final))
        assert compared.returncode == 0, compared.stdout

        events = _audit_events(runs_dir)
        kinds = [e["event"] for e in events]
        assert "verify_failed" in kinds and "rollback_step" in kinds
        assert kinds[-1] == "run_finished"
    finally:
        server.terminate()
        server.wait(timeout=10)
