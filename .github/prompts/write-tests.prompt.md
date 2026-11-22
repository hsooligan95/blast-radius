---
mode: agent
description: Write tests for a named module in blast-radius
---

Write tests for the module I name, following this repo's existing test
style, layout, and fixtures. Cover the behavior the module documents,
including these invariants where they apply:

- Nothing mutating runs without explicit human approval of that exact plan — never add a path that executes an unapproved plan.
- Every mutating step records its inverse from live pre-state at execution time; a step without a recorded inverse must not mutate.
- Verify post-conditions against live cloud state, never against the agent's own claims.

Then run the test suite:

    pytest

Show the result. Do not claim done unless the suite is green; if a
test cannot pass, say why instead of weakening the assertion.
