# feature-tester — independent integration test of the key behavior

The empirical gate between ship-task's hermetic `verify.sh` (does it compile +
do the tests pass) and the human test gate (does it actually work). A **fresh,
black-box** agent writes ONE focused integration test of the feature's key
user-facing behavior; the **ship-task agent** (the builder, full impl context)
re-runs it and fixes the code until green — automatically. One independent
confirmation run seals it, and the test is committed as a permanent regression
guard.

## Why independence

Same principle as ship-task's Step-2 blind test agent: a tester primed by the
builder's reasoning "verifies" the builder's wrong assumptions. The tester runs
in a fresh session, never sees the implementation diff, and asserts what the
feature *should* do — not what the code happens to do.

## Where it runs

- **ship-task Step 5.5** (Tier 2-3): after the verify gate, before commit/PR.
- **Standalone**: `feature-test this branch for <key behavior>`.

## Pieces

| File | Role |
|------|------|
| `SKILL.md` | the loop: name the key behavior → dispatch a fresh tester → builder fixes (auto, capped) → final independent confirm → commit / escalate |
| `run-focused-test.sh` | runs ONE test command with a compact PASS/FAIL + failure tail, so the fix loop is one round-trip |
| `feature-test-gate.sh` | records the verdict and blocks ship-task's commit unless one exists and isn't `fail` |

## Loop at a glance

```
verify.sh green
   │  name the ONE key behavior
   ▼
TESTER (fresh, black-box) authors 1 focused integration test, runs it → verdict
   │ red
   ▼
BUILDER (= ship-task agent) fixes impl, re-runs   ← cap 5
   ·  cycle 3 → /investigate   ·  cycle 4 → /codex challenge   ·  cycle 5 → EXHAUST
   │ green
   ▼
FINAL TESTER (fresh) re-runs + confirms intent  ·  optional /code-review
   │ pass                                   │ exhausted
   ▼                                        ▼
commit the test (regression guard)     tell human + (under /manager) POST the verdict
```

## Composes other skills (each only when installed)

- **`/browse`** (fallback `/qa-only`) — when the key behavior is genuinely
  cross-process (real API/auth/500) and can't be proven hermetically.
- **`/investigate`** then **`/codex`** challenge — when the fix loop stalls.
- superpowers **test-driven-development · systematic-debugging ·
  verification-before-completion** — author/fix discipline (guides).
- **`/code-review`** — advisory bug scan before the human gate.

## Test conventions it follows

Whatever the target repo already uses. The tester is told to find 2-3
neighboring tests and mirror their runner, mocking, assertions, and file
location — it never introduces a new test framework.

## Manager panel display

Under `/manager`, the run POSTs its verdict to `$MANAGER_URL/api/featuretest` for
**every** outcome (`pass` / `skipped` / `blocked` / `fail`), with the per-cycle
`turns` and the final `output`. The panel shows a `🧪 <verdict>` chip on the task
card plus an expandable detail (the turns timeline + output) — so you can see what
the tester did, not just whether it stuck. `$MANAGER_URL` flows from
`managerd.py → dispatch.sh`, so it tracks the configured panel port.
(`/api/failtest` remains as a back-compat alias for `verdict:"fail"`.)
