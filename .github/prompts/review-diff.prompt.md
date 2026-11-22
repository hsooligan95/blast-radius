---
mode: agent
description: Review the current diff against blast-radius's invariants
---

Review my current diff against this repo's invariants:

- Nothing mutating runs without explicit human approval of that exact plan — never add a path that executes an unapproved plan.
- Every mutating step records its inverse from live pre-state at execution time; a step without a recorded inverse must not mutate.
- Verify post-conditions against live cloud state, never against the agent's own claims.
- The planner is deterministic (no LLM, no network) and policy is default-deny, scoped per scenario.

Flag every violation with file and line. Also flag any change that
alters a documented behavior contract (output shapes, exit codes,
event schemas, recorded artifacts) without updating the docs and
tests that describe it. No style nitpicks — correctness and contract
violations only.
