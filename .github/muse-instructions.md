# Copilot instructions — blast-radius

You are working in **BlastRadius**, an infrastructure remediation agent
with a blast radius of zero: plan -> approve -> checkpointed execute ->
verify -> rollback, running against moto (mock AWS) as a separate server
process so the cloud genuinely outlives the agent.

## Stack

- Python 3; package `blastradius/` with CLI entry `blastradius.cli:main`.
- LangGraph for the execution graph (one step per super-step),
  `interrupt()` for durable human approval, SQLite checkpoints, moto for
  the mock cloud, IAM-style policies under `policies/`.

## Tests

```bash
pytest
bash scripts/demo_kill_resume.sh   # end-to-end: kill -9 mid-run, resume, verify
```

## Architecture notes

- The planner is deterministic — no LLM, no API keys, no network in
  planning. The engineering on display is the safety shell, not a model.
- Nothing mutating runs without explicit human approval of that exact
  plan. Never add a path that executes an unapproved plan.
- Every mutating step records its inverse from live pre-state at
  execution time; rollback replays inverses in reverse order. A step
  without a recorded inverse must not mutate.
- Verify post-conditions against live cloud state, never against the
  agent's own claims.
- Policy is default-deny, scoped per scenario with tag conditions.
  Audit events are formatted as compliance evidence (timestamp, actor,
  action, resource, approved-by, result) — keep every field populated.

## Shared rules

- Use Unix LF line endings in every file; never CRLF.
- Never commit secrets, tokens, or credentials — real or synthetic-looking.
- Tests must pass before you describe a change as complete; run the test
  command above and report honestly if you cannot.
- Prefer the smallest complete implementation: reuse what already exists in
  this repo before adding new code, files, or dependencies.
