---
name: feature-tester
version: 0.2.0
description: |
  Independent post-build verifier for /ship-task. After a feature is built and the
  verify gate is green, a FRESH agent (no implementation context) authors ONE
  focused integration test of the feature's KEY user-facing behavior, then the
  ship-task agent (the builder, full impl context) re-runs it and fixes the code
  until it passes — automatically, no human in the inner loop. One final
  independent confirmation run seals it; the test is committed as a durable
  regression guard. On exhausting the fix budget it tells the human and, under
  /manager, shows the failing test on the panel. Composes other installed skills
  when present: /browse for genuinely cross-process behavior, /investigate then
  /codex challenge when the fix loop stalls. Runs as ship-task Step 5.5 (Tier 2-3),
  or standalone: "feature-test this branch", "write an integration test for <key
  behavior>".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Agent
  - Skill
  - AskUserQuestion
---

# /feature-tester — prove the key behavior with one focused integration test, in an automatic fix loop

`verify.sh` proves the code *compiles and the existing tests pass*. It is hermetic
and says nothing about whether the **feature actually behaves** the way it should.
The feature-tester closes that gap: an **independent** agent writes ONE focused
integration test of the feature's *key user-facing behavior*, and the builder
fixes until it's green — before a human ever looks.

The independence is the point — the same reason ship-task's Step-2 test agent is
blind to the implementation. A tester primed by the builder's reasoning "verifies"
the builder's wrong assumptions. So the **tester runs in a fresh session and never
sees the implementation diff**; it asserts what the feature *should* do, derived
from the key-behavior statement, not what the code happens to do.

This skill is installed as a sibling of ship-task; its scripts live at
`$FT_DIR` = the directory of this SKILL.md (typically
`$HOME/.claude/skills/feature-tester`).

## The triangle (who does what)

- **Tester** — a FRESH subagent (Agent tool, fresh context). Authors ONE focused
  integration test of the key behavior **inside the feature worktree `$WT`** and
  runs it once. Black-box: given the intended behavior + the repo's test
  conventions + `$WT`, **not** the diff. Author-once.
- **Builder** — the **ship-task agent itself** (the caller; full implementation
  context). Receives the tester's test + logs, re-runs the test, and fixes the
  implementation until it's green. Automatic — no human in the inner loop. Must
  **never weaken the test** to pass it.
- **Final confirmation** — ONE more fresh tester subagent re-runs the test
  independently and checks it still asserts the intended behavior (not a weakened
  version), then it's committed.

A genuinely-wrong test (asserts the wrong thing) is corrected by **re-dispatching
a fresh tester** to rewrite it — never by the builder quietly editing it. That
mid-loop re-dispatch is the only exception to "author-once + one final
confirmation"; everything runs in `$WT`, never the main checkout.

## When it runs

- **As ship-task Step 5.5** — after the verify gate (Step 5) is green and before
  the commit/PR steps. **Tier 2-3 only** (real features). Tier 0 has nothing to
  test; Tier 1's single focused test already ran at the verify gate.
- **Standalone** — `feature-test this branch for <key behavior>`; same loop.

## The loop

### A. Name the ONE key behavior + pick the test type

State the single most important user-facing behavior in one sentence — the thing
that, if broken, means the feature failed. Examples:
- *"In the team view the Chats item appears, and clicking it keeps you in
  team view."*
- *"Placing a tagged order deducts the order's cost from that budget."*
- *"A team lead is not listed in any project's Members list."*

Pick the cheapest test type that can prove it, **in the repo's own test stack**
(vitest/jest for a JS frontend, `go test`/`pytest`/etc. for a backend — mirror
what neighboring tests do; do not introduce a new test framework):
- **UI behavior** → the repo's component/unit test runner, with its established
  mocking conventions. Find 2-3 neighboring tests and mirror them. The new test
  goes **inside the worktree**, in the directory the repo keeps such tests
  (never the main checkout — else Step 6's `git -C "$WT" add -A` won't stage it).
- **Server behavior** → the repo's server test convention (in-memory fakes,
  table-driven, etc.). Same rule: mirror a neighboring test, write under `$WT`.
- **Genuinely cross-process** (real API round-trip, real 500, auth that can't be
  faked) → a hermetic test can't cover it. Delegate to a live-app check
  (**`/browse`** or **`/qa-only`** when installed) against the running app, and
  note it as such — do not fake it. Keep this the exception; default to a fast
  hermetic test.

### B. Dispatch the tester (fresh, black-box)

Dispatch a `general-purpose` subagent. Hand it ONLY:
- the **one key behavior** to prove (intent — what it *should* do);
- the **feature worktree root `$WT`** — it MUST author and run the test there
  (the feature's code lives only in `$WT`; the main checkout is on the plain base
  branch);
- **how tests are built here** — point it at a canonical neighboring test and tell
  it to read 2-3 more to match conventions (imports, mocking, assertions, file
  location);
- the focused **run command, rooted at the worktree** (the repo's way of running
  ONE test file, e.g. `cd "$WT" && npx vitest run <path>` or
  `cd "$WT" && go test ./<pkg> -run <Test>`);
- read access to the repo to find *where* to test (component/route/handler names).

**Withhold:** the implementation diff and the builder's reasoning. Instruct it:

> Write ONE focused integration test that asserts the intended behavior above.
> Write it UNDER `$WT` and run it from `$WT` —
> the feature's code is only in that worktree. Derive the assertion from what the
> feature SHOULD do — do NOT reverse-engineer it from the implementation so it
> trivially passes. Follow `superpowers:test-driven-development` conventions and
> mirror a neighboring test. Run it once and return the verdict object (below)
> — its `verdict`, the `test_path` (under `$WT`), the `run_command`, and the
> failing `logs` if red. If the behavior genuinely can't be proven by a hermetic
> test (real API/auth/500), set `verdict:"blocked"` + `blocker` and stop — do NOT
> fake a pass. Do not edit any implementation code.

Return value: the **verdict contract** (see below).

### C. Builder fix loop (automatic; the ship-task agent owns this)

Branch on the tester's `verdict`:
- **`blocked`** (blocker non-null — the behavior can't be proven hermetically) →
  do **NOT** enter the fix loop and do **NOT** count cycles. Delegate to a
  live-app check (**`/browse`** / **`/qa-only`** when installed) against the
  running app, or defer to the human's post-hand-off testing. Then go to E with
  that outcome. Never fake a pass.
- **`pass`** (green on first run) → skip to D.
- **`fail`** (red) → run the automatic fix loop below.

Fix loop (red only). YOU (the ship-task/builder agent, full impl context) fix the
implementation and re-run — a tight automatic loop. Each cycle:

1. Read the failing `logs`. Fix the **implementation** (never the test — do not
   weaken, skip, or delete it; if the test itself is wrong, re-dispatch the tester
   to correct it, don't quietly edit it yourself).
2. Re-run the focused test — rooted at the worktree (one round-trip):
   `"$FT_DIR/run-focused-test.sh" --root "$WT" --cmd "<the tester's run_command>"`.
3. Keep `verify.sh --root "$WT"` green too (a fix must not break the suite).

**Escalation, by cycle (cap = 5):**
- **Cycle 3, still red** → invoke **`/investigate`** (root-cause, 4 phases) on the
  failure before guessing further. Follow `superpowers:systematic-debugging`.
- **Cycle 4, still red** → invoke **`/codex`** in challenge mode on the diff +
  failure to surface a wrong structural assumption. (Skip either escalation if
  that skill isn't installed.)
- **Cycle 5, still red → EXHAUSTION** (see E). Stop; do not loop forever.

### D. Final independent confirmation

Once green, dispatch ONE more fresh `general-purpose` subagent to: re-run the test
independently and confirm it (i) passes and (ii) still asserts the intended
behavior (skim it for a weakened/tautological assertion). Apply
`superpowers:verification-before-completion` — evidence (the green run output)
before the claim. Optionally run **`/code-review`** on the builder's diff here for
a last bug scan (advisory — a finding goes back to the builder, it doesn't fail
the gate).

### E. Settle — the loop MUST end with a recorded, shown result

Throughout the loop, keep a short **turns log** — one entry per cycle:
`{n, note (what the tester/builder did that round), result ("red"|"green")}`. The
panel shows it so the human can see how the test progressed.

The loop never ends silently. Every terminal state **records a verdict** to
`$WT/.feature-test-result.json` via the gate, and **shows** it. The commit step
(ship-task Step 6) then runs `feature-test-gate.sh --root "$WT"`, which **blocks
the commit** unless a result exists and isn't `fail` — so a skipped or red
feature test cannot reach a committed branch. (The gate checks the *recorded
verdict*, not a re-run — so record it as the **last action before commit** and do
not edit the implementation after recording `pass`.) Record exactly one of:

- **Pass** (green, confirmed) →
  `feature-test-gate.sh --root "$WT" --record pass --test <path> --cycles <N>`.
  The focused test is committed with the feature (durable regression guard).
  Report green + the test path; ship-task continues to commit.
- **Blocked** (cross-process — can't be proven hermetically) →
  `feature-test-gate.sh --root "$WT" --record blocked --test <path> --reason "<why>"`.
  Delegate the live check (or defer to the human gate), report it, and continue.
  Never fake a pass to get past the gate.
- **Exhaustion** (still red after the cap) →
  `feature-test-gate.sh --root "$WT" --record fail --test <path> --cycles <N>`,
  then **STOP** (do not push — the gate will block commit anyway). Tell the human
  the failing test path, the last logs, and the builder's cycle count `N`. The
  branch/worktree stays for inspection.

**Show the verdict on the manager panel — for ALL outcomes.** If `$MANAGER_URL`
is set (running under /manager), after recording the gate verdict, POST it so it
appears as a `🧪 <verdict>` chip + an expandable turns/output detail on the task
card — `pass` / `skipped` / `blocked` / `fail` alike. Do **NOT** call
`AskUserQuestion` headless (it hangs forever). **Substitute the verdict you just
recorded** (the `"<verdict>"` below — do not leave it as `pass`):
```bash
curl -s -X POST "$MANAGER_URL/api/featuretest" -H 'Content-Type: application/json' \
  -d '{"session_id":"'"$MANAGER_SESSION_ID"'","verdict":"<pass|skipped|blocked|fail — the one you recorded>","test":"<test_path>","cycles":<N>,
       "output":"<last ~600 chars of the final test run>",
       "turns":[{"n":1,"note":"authored test; ran","result":"red"},
                {"n":2,"note":"implemented X","result":"green"}]}'
```
- **`turns` notes must be plain ASCII** — no quotes/apostrophes/newlines (they break
  the shell-quoted JSON; a malformed body is silently dropped → no chip). Write
  "implemented X fix", not "user's fix". If unsure, **omit `turns`** — the chip still
  shows from the verdict alone.
- **Best-effort:** if the daemon is old/unreachable the POST just 404s silently —
  that does **not** fail the run (the local gate verdict already recorded).

`$MANAGER_URL` / `$MANAGER_SESSION_ID` are exported by `dispatch.sh` (the SAME id
as `/api/runinfo`/`/api/ask`, which correlates this to the run's row). For an
**exhaustion**, this same POST (`verdict:"fail"`, with the `turns`) surfaces it on
the Stopped card; do not block on an answer.

**Standalone** (no `$MANAGER_URL`): no panel — report the verdict in the terminal;
for an exhaustion, surface it via `AskUserQuestion`.

## The verdict contract (what the tester returns)

```json
{
  "verdict": "pass" | "fail" | "blocked",
  "key_behavior": "one line — what was proven",
  "test_path": "$WT/<repo's test dir>/<file>",
  "test_type": "ui-unit" | "server" | "browse-e2e",
  "run_command": "cd \"$WT\" && <the repo's single-file test command>",
  "logs": "green | <last ~40 lines of the failing run>",
  "blocker": null
}
```
The tester runs author-once, so it reports no cycle count — the **builder** keeps
its own fix-cycle counter `N`, and that N is what feeds the human report and the
manager `attempts` field on exhaustion (not anything from this contract).
`blocker` is non-null only when the behavior cannot be proven hermetically (e.g.
needs a live backend) — say so instead of faking a pass.

## Composition map (reuse, don't reinvent — each only when installed)

| Situation | Skill | How |
|---|---|---|
| Key behavior is genuinely cross-process (real API/auth/500) | **`/browse`** (fallback **`/qa-only`**) | drive the running app; needs a live dev server |
| Fix loop still red at cycle 3 | **`/investigate`** | root-cause the failure before more guessing |
| Still red at cycle 4 (structure may be wrong) | **`/codex`** challenge | adversarial review of diff + failure |
| Authoring / fixing discipline | `superpowers:test-driven-development`, `systematic-debugging`, `verification-before-completion` | guides the tester + builder; not invoked |
| Last bug scan before the human gate | **`/code-review`** | advisory on the builder's diff |

## What it must never do

- Never let the builder **weaken, skip, or delete** the test to make it pass — fix
  the implementation, or re-dispatch the tester to correct a genuinely-wrong test.
- Never give the **tester** the implementation diff or the builder's reasoning —
  independence is what makes its verdict trustworthy.
- Never loop past the cap — on exhaustion, stop and report (human + manager panel).
- Never **fake** a pass for a cross-process behavior a hermetic test can't reach —
  set `blocker` and delegate to a live-app check, or defer to the human gate.
- Never merge — the feature-tester gates *before* the PR/human; landing safety
  stays with ship-task.
