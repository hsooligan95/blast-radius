"""BlastRadius command line.

Commands:
  seed      create a scenario's broken/wasteful starting state
  run       plan a remediation and pause for approval (or --auto-approve)
  approve   grant approval to a paused run and let it execute
  resume    re-attach to a run whose process died mid-execution
  status    show a run's checkpointed state
  audit     print a run's audit log (compliance evidence)
  snapshot  dump the current cloud state as JSON
  compare   compare two snapshot files (exit 0 iff identical)

Exit codes for `run`/`approve`/`resume`: 0 = completed or rolled back
(the safety shell did its job), 1 = failed, 2 = aborted (approval denied),
3 = paused awaiting approval.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# boto3 needs *some* credentials even against moto; these are moto's
# documented dummy test credentials, not secrets. Real credentials from the
# environment always win.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

from langgraph.types import Command  # noqa: E402

from .audit import format_events, read_events  # noqa: E402
from .cloud import diff_snapshots, ec2_client, snapshot  # noqa: E402
from .graph import build_graph, thread_config  # noqa: E402
from .scenarios import SEEDERS  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICIES_DIR = PROJECT_ROOT / "policies"


def runs_dir() -> Path:
    return Path(os.environ.get("BLASTRADIUS_RUNS_DIR", PROJECT_ROOT / "runs"))


def policies_dir() -> Path:
    return Path(os.environ.get("BLASTRADIUS_POLICIES_DIR", DEFAULT_POLICIES_DIR))


def _write_run_meta(run_id: str, scenario: str) -> None:
    d = runs_dir() / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(
        json.dumps({"run_id": run_id, "scenario": scenario}), encoding="utf-8")


def _read_run_meta(run_id: str) -> dict:
    path = runs_dir() / run_id / "run.json"
    if not path.exists():
        raise SystemExit(f"no such run: {run_id} (missing {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def _graph_for(run_id: str, step_delay: float = 0.0):
    meta = _read_run_meta(run_id)
    return build_graph(ec2_client(), runs_dir(), run_id, meta["scenario"],
                       policies_dir(), step_delay=step_delay)


def _print_plan(interrupt_payload: dict) -> None:
    print(f"\n=== APPROVAL REQUEST — run {interrupt_payload['run_id']} "
          f"({interrupt_payload['scenario']}) ===")
    print("Plan (dry-run — nothing has mutated yet):")
    for i, step in enumerate(interrupt_payload["plan"], 1):
        print(f"  {i}. [{step['action']}] {step['description']}")
    if interrupt_payload.get("findings"):
        print("Findings (inspected, deliberately untouched):")
        for finding in interrupt_payload["findings"]:
            print(f"  - {finding}")


def _report_result(result: dict) -> int:
    status = result.get("status", "unknown")
    print(f"\nrun status: {status}")
    if status == "completed":
        return 0
    if status == "rolled_back":
        print("verification or a step failed; all executed steps were rolled back")
        return 0
    if status == "aborted":
        return 2
    return 1


def _drive(graph, run_id: str, first_input, args) -> int:
    """Invoke the graph; handle the approval interrupt per CLI flags."""
    config = thread_config(run_id)
    result = graph.invoke(first_input, config)
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if interrupts:
        payload = interrupts[0].value
        _print_plan(payload)
        if getattr(args, "auto_approve", False):
            print("\n--auto-approve given (scripted demo only): approving plan")
            decision = {"approved": True,
                        "approved_by": f"{args.actor} via --auto-approve (DEMO ONLY)"}
        elif getattr(args, "interactive", False):
            answer = input("\nApprove this plan? [y/N] ").strip().lower()
            decision = {"approved": answer == "y",
                        "approved_by": f"{args.actor} via interactive CLI"}
        else:
            print(f"\nRun paused. Approve with:  blastradius approve --run-id {run_id}")
            print(f"             or resume after a crash with:  blastradius resume --run-id {run_id}")
            return 3
        result = graph.invoke(Command(resume=decision), config)
    return _report_result(result)


# -- subcommands -------------------------------------------------------------


def cmd_seed(args) -> int:
    result = SEEDERS[args.scenario](ec2_client())
    print(json.dumps(result, indent=2))
    return 0


def cmd_run(args) -> int:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    _write_run_meta(run_id, args.scenario)
    graph = build_graph(ec2_client(), runs_dir(), run_id, args.scenario,
                        policies_dir(), step_delay=args.step_delay)
    print(f"run {run_id}: planning {args.scenario} ...")
    initial = {"run_id": run_id, "scenario": args.scenario, "actor": args.actor}
    return _drive(graph, run_id, initial, args)


def cmd_approve(args) -> int:
    graph = _graph_for(args.run_id)
    decision = {"approved": not args.deny,
                "approved_by": f"{args.actor} via approve command"}
    result = graph.invoke(Command(resume=decision), thread_config(args.run_id))
    return _report_result(result)


def cmd_resume(args) -> int:
    graph = _graph_for(args.run_id)
    state = graph.get_state(thread_config(args.run_id))
    if state.next and state.values.get("status") == "planned":
        print(f"run {args.run_id} is paused awaiting approval; "
              f"use: blastradius approve --run-id {args.run_id}")
        return 3
    print(f"run {args.run_id}: resuming from checkpoint "
          f"(step_index={state.values.get('step_index')}) ...")
    result = graph.invoke(None, thread_config(args.run_id))
    if isinstance(result, dict) and result.get("__interrupt__"):
        print("run is paused awaiting approval")
        return 3
    return _report_result(result)


def cmd_status(args) -> int:
    graph = _graph_for(args.run_id)
    state = graph.get_state(thread_config(args.run_id))
    values = state.values
    summary = {
        "run_id": args.run_id,
        "scenario": values.get("scenario"),
        "status": values.get("status"),
        "step_index": values.get("step_index"),
        "plan_steps": len(values.get("plan", [])),
        "approved": values.get("approved"),
        "approved_by": values.get("approved_by"),
        "verify_passed": values.get("verify_passed"),
        "next_nodes": list(state.next),
    }
    print(json.dumps(summary, indent=2))
    return 0


def cmd_audit(args) -> int:
    path = runs_dir() / args.run_id / "audit.jsonl"
    events = read_events(path)
    if not events:
        print(f"no audit events for run {args.run_id}")
        return 1
    print(format_events(events))
    return 0


def cmd_snapshot(args) -> int:
    snap = snapshot(ec2_client())
    text = json.dumps(snap, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"snapshot written to {args.out}")
    else:
        print(text)
    return 0


def cmd_compare(args) -> int:
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    diffs = diff_snapshots(a, b)
    if diffs:
        print("snapshots DIFFER:")
        for d in diffs:
            print(f"  {d}")
        return 1
    print("snapshots are IDENTICAL")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blastradius", description=__doc__)
    parser.add_argument("--actor", default=os.environ.get("USER", "operator"),
                        help="name recorded in the audit log")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed")
    p.add_argument("--scenario", required=True, choices=sorted(SEEDERS))
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("run")
    p.add_argument("--scenario", required=True, choices=sorted(SEEDERS))
    p.add_argument("--run-id", default=None)
    p.add_argument("--auto-approve", action="store_true",
                   help="approve the plan without asking (scripted demos ONLY)")
    p.add_argument("--interactive", action="store_true",
                   help="prompt for approval at the terminal")
    p.add_argument("--step-delay", type=float,
                   default=float(os.environ.get("BLASTRADIUS_STEP_DELAY", "0")),
                   help="seconds to sleep before each mutation (crash demos)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("approve")
    p.add_argument("--run-id", required=True)
    p.add_argument("--deny", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("resume")
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("status")
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("audit")
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("snapshot")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("compare")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.set_defaults(fn=cmd_compare)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
