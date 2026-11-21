"""Least-privilege policy enforcement.

Each scenario ships an IAM-style policy document (see policies/). Before the
executor issues any mutating AWS call it asks the policy: is this action
allowed, on this resource, whose tags are these? Evaluation mirrors IAM
semantics at a small scale:

  * an explicit Deny beats any Allow;
  * Action and Resource patterns are globs (``ec2:Describe*``,
    ``arn:aws:ec2:*:*:instance/*``);
  * a statement may carry tag conditions — every condition must match the
    target resource's live tags (e.g. DeleteVolume is only allowed when
    ``protected`` is not ``"true"``);
  * anything not explicitly allowed is denied (default deny).

A denial is not a suggestion: the executor raises PolicyDenied, the run is
marked failed, and the rollback path restores whatever already executed.
The agent never escalates its own privileges.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path


class PolicyDenied(Exception):
    def __init__(self, action: str, resource: str, reason: str):
        super().__init__(f"policy denied {action} on {resource}: {reason}")
        self.action = action
        self.resource = resource
        self.reason = reason


@dataclass
class Statement:
    effect: str
    actions: list[str]
    resources: list[str]
    tag_conditions: dict[str, str] = field(default_factory=dict)
    sid: str = ""


@dataclass
class Policy:
    name: str
    statements: list[Statement]

    @classmethod
    def load(cls, path: Path) -> "Policy":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        statements = []
        for raw in doc.get("Statement", []):
            cond = raw.get("Condition", {}).get("StringEquals", {})
            tag_conditions = {
                k.split("aws:ResourceTag/", 1)[-1]: v for k, v in cond.items()
            }
            statements.append(
                Statement(
                    effect=raw["Effect"],
                    actions=list(raw["Action"]),
                    resources=list(raw["Resource"]),
                    tag_conditions=tag_conditions,
                    sid=raw.get("Sid", ""),
                )
            )
        return cls(name=doc.get("PolicyName", Path(path).stem), statements=statements)

    def _statement_matches(self, st: Statement, action: str, resource_arn: str,
                           resource_tags: dict[str, str]) -> bool:
        if not any(fnmatch.fnmatchcase(action, pat) for pat in st.actions):
            return False
        if not any(fnmatch.fnmatchcase(resource_arn, pat) for pat in st.resources):
            return False
        for key, expected in st.tag_conditions.items():
            if resource_tags.get(key) != expected:
                return False
        return True

    def allows(self, action: str, resource_arn: str,
               resource_tags: dict[str, str]) -> tuple[bool, str]:
        """Return (allowed, human-readable reason)."""
        allow_sid = None
        for st in self.statements:
            if not self._statement_matches(st, action, resource_arn, resource_tags):
                continue
            if st.effect == "Deny":
                return False, f"explicit deny in statement '{st.sid}'"
            allow_sid = st.sid
        if allow_sid is not None:
            return True, f"allowed by statement '{allow_sid}'"
        return False, "no matching Allow statement (default deny)"
