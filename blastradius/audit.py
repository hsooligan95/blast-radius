"""Append-only audit log, formatted as compliance evidence.

Every action the agent takes — planning, policy decisions, approval,
execution, verification, rollback — is appended as one JSON object per line.
Each record carries the fields an auditor asks for: when (UTC timestamp),
who (actor), what (event + AWS action + resource), under whose approval
(approved_by), and with what result.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    def __init__(self, path: Path, run_id: str, actor: str):
        self.path = Path(path)
        self.run_id = run_id
        self.actor = actor
        # Instance identity for fleet rollups (AgentTrace): which
        # process of this agent type wrote the record. From the
        # shared AGENT_INSTANCE env var; "default" when unset. A
        # resumed run is a new process under the same env, so it
        # keeps the same instance.
        self.instance = os.environ.get("AGENT_INSTANCE") or "default"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event: str,
        action: str | None = None,
        resource: str | None = None,
        approved_by: str | None = None,
        result: str = "ok",
        detail: str | None = None,
    ) -> dict:
        record = {
            "ts": _now(),
            "run_id": self.run_id,
            "actor": self.actor,
            "instance": self.instance,
            "event": event,
            "action": action,
            "resource": resource,
            "approved_by": approved_by,
            "result": result,
            "detail": detail,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return record


def read_events(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def format_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        parts = [e["ts"], f"run={e['run_id']}", f"actor={e['actor']}", f"event={e['event']}"]
        if e.get("action"):
            parts.append(f"action={e['action']}")
        if e.get("resource"):
            parts.append(f"resource={e['resource']}")
        if e.get("approved_by"):
            parts.append(f"approved_by={e['approved_by']}")
        parts.append(f"result={e['result']}")
        if e.get("detail"):
            parts.append(f"detail={e['detail']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)
