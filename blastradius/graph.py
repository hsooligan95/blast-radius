"""The LangGraph execution shell: plan -> approve -> execute -> verify
-> (rollback) -> finalize.

Durability model
----------------
The graph is compiled with a SqliteSaver checkpointer, one thread per
run_id. Execution is structured so that **each plan step is its own
super-step**: the execute node applies exactly one step, returns, and the
framework persists the state — including the step's recorded inverse —
before the next step begins. Kill the process at any point and a fresh
process can re-attach to the same thread and continue exactly where the
checkpoint left off. Rollback steps get the same treatment, one inverse
per super-step.

The approve node uses LangGraph's interrupt(): the run pauses durably
until a human decision is resumed into the thread. Nothing mutates before
that decision exists.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .actions import StepExecutor
from .audit import AuditLog
from .planner import PLANNERS, VERIFIERS
from .policy import Policy, PolicyDenied

POLICY_FILES = {
    "idle-remediation": "idle-remediation-policy.json",
    "failed-deployment": "deployment-recovery-policy.json",
}


class RunState(TypedDict, total=False):
    run_id: str
    scenario: str
    actor: str
    plan: list
    findings: list
    step_index: int
    inverses: list          # one entry per executed step (dict or None)
    approved: bool
    approved_by: str
    verify_results: list
    verify_passed: bool
    failed: bool
    failure_detail: str
    rollback_index: int     # next inverse to replay is rollback_index - 1
    status: str


def build_graph(ec2, runs_dir: Path, run_id: str, scenario: str,
                policies_dir: Path, step_delay: float = 0.0):
    """Compile the graph for one run, bound to its audit log and policy."""
    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(run_dir / "audit.jsonl", run_id=run_id, actor="blastradius-agent")
    policy = Policy.load(Path(policies_dir) / POLICY_FILES[scenario])

    def executor(state: RunState) -> StepExecutor:
        return StepExecutor(ec2, policy, audit,
                            approved_by=state.get("approved_by"),
                            step_delay=step_delay)

    # -- nodes ---------------------------------------------------------------

    def plan_node(state: RunState) -> dict:
        result = PLANNERS[state["scenario"]](ec2)
        steps = [dict(s, step_id=f"s{i + 1}") for i, s in enumerate(result["steps"])]
        audit.log("plan_created", result="ok",
                  detail=f"{len(steps)} step(s), {len(result['findings'])} finding(s) "
                         f"under policy {policy.name}")
        return {"plan": steps, "findings": result["findings"],
                "step_index": 0, "inverses": [], "status": "planned"}

    def approve_node(state: RunState) -> dict:
        decision = interrupt({
            "type": "approval_request",
            "run_id": state["run_id"],
            "scenario": state["scenario"],
            "plan": state["plan"],
            "findings": state["findings"],
        })
        approved = bool(decision.get("approved"))
        approved_by = decision.get("approved_by", "unknown")
        audit.log("approval_granted" if approved else "approval_denied",
                  approved_by=approved_by, result="ok" if approved else "denied",
                  detail=f"plan of {len(state['plan'])} step(s)")
        return {"approved": approved, "approved_by": approved_by,
                "status": "approved" if approved else "aborted"}

    def execute_node(state: RunState) -> dict:
        idx = state["step_index"]
        step = state["plan"][idx]
        audit.log("step_started", action=step["action"], resource=step["resource_id"],
                  approved_by=state.get("approved_by"),
                  detail=f"step {step['step_id']}/{len(state['plan'])}: {step['description']}")
        try:
            outcome = executor(state).apply(step)
        except PolicyDenied as exc:
            audit.log("step_failed", action=exc.action, resource=exc.resource,
                      approved_by=state.get("approved_by"), result="policy_denied",
                      detail=str(exc))
            return {"failed": True, "failure_detail": str(exc),
                    "rollback_index": len(state["inverses"]), "status": "failed"}
        except Exception as exc:  # cloud API error mid-step: fail the run, roll back
            audit.log("step_failed", action=step["action"], resource=step["resource_id"],
                      approved_by=state.get("approved_by"), result="error",
                      detail=f"{type(exc).__name__}: {exc}")
            return {"failed": True, "failure_detail": f"{type(exc).__name__}: {exc}",
                    "rollback_index": len(state["inverses"]), "status": "failed"}
        audit.log("step_applied", action=step["action"], resource=step["resource_id"],
                  approved_by=state.get("approved_by"), result=outcome["result"],
                  detail=outcome["detail"])
        return {"inverses": state["inverses"] + [outcome["inverse"]],
                "step_index": idx + 1, "status": "executing"}

    def verify_node(state: RunState) -> dict:
        checks = VERIFIERS[state["scenario"]](ec2)
        passed = all(c["passed"] for c in checks)
        for c in checks:
            audit.log("verify_check", result="pass" if c["passed"] else "FAIL",
                      detail=f"{c['check']}: expected {c['expected']}, actual {c['actual']}")
        audit.log("verify_passed" if passed else "verify_failed",
                  result="ok" if passed else "failed",
                  detail=f"{sum(c['passed'] for c in checks)}/{len(checks)} checks passed")
        updates = {"verify_results": checks, "verify_passed": passed}
        if not passed:
            updates["rollback_index"] = len(state["inverses"])
        return updates

    def rollback_node(state: RunState) -> dict:
        idx = state["rollback_index"] - 1
        inverse = state["inverses"][idx]
        if inverse is None:
            audit.log("rollback_step_skipped", result="ok",
                      detail=f"inverse #{idx + 1}: step had no mutation to undo")
        else:
            outcome = executor(state).apply_inverse(inverse)
            audit.log("rollback_step", action=inverse["action"],
                      resource=inverse["resource_id"],
                      approved_by=state.get("approved_by"), result=outcome["result"],
                      detail=outcome["detail"])
        return {"rollback_index": idx, "status": "rolling_back"}

    def finalize_node(state: RunState) -> dict:
        if not state.get("approved", False):
            status = "aborted"
        elif state.get("verify_passed"):
            status = "completed"
        elif state.get("failed") and not any(
                inv is not None for inv in state.get("inverses", [])):
            status = "failed"
        elif state.get("failed") or state.get("verify_results"):
            status = "rolled_back"
        else:
            status = "finished"
        audit.log("run_finished", result=status,
                  approved_by=state.get("approved_by"),
                  detail=state.get("failure_detail") or
                         f"{state.get('step_index', 0)} step(s) executed")
        return {"status": status}

    # -- routing ---------------------------------------------------------------

    def route_after_approve(state: RunState) -> str:
        if not state.get("approved"):
            return "finalize"
        return "execute" if state["plan"] else "verify"

    def route_after_execute(state: RunState) -> str:
        if state.get("failed"):
            return "rollback" if state["rollback_index"] > 0 else "finalize"
        if state["step_index"] < len(state["plan"]):
            return "execute"
        return "verify"

    def route_after_verify(state: RunState) -> str:
        if state.get("verify_passed"):
            return "finalize"
        return "rollback" if state["rollback_index"] > 0 else "finalize"

    def route_after_rollback(state: RunState) -> str:
        return "rollback" if state["rollback_index"] > 0 else "finalize"

    # -- assembly ----------------------------------------------------------------

    builder = StateGraph(RunState)
    builder.add_node("plan", plan_node)
    builder.add_node("approve", approve_node)
    builder.add_node("execute", execute_node)
    builder.add_node("verify", verify_node)
    builder.add_node("rollback", rollback_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "approve")
    builder.add_conditional_edges("approve", route_after_approve,
                                  {"execute": "execute", "verify": "verify",
                                   "finalize": "finalize"})
    builder.add_conditional_edges("execute", route_after_execute,
                                  {"execute": "execute", "verify": "verify",
                                   "rollback": "rollback", "finalize": "finalize"})
    builder.add_conditional_edges("verify", route_after_verify,
                                  {"rollback": "rollback", "finalize": "finalize"})
    builder.add_conditional_edges("rollback", route_after_rollback,
                                  {"rollback": "rollback", "finalize": "finalize"})
    builder.add_edge("finalize", END)

    conn = sqlite3.connect(str(run_dir / "checkpoints.sqlite"), check_same_thread=False)
    return builder.compile(checkpointer=SqliteSaver(conn))


def thread_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}
