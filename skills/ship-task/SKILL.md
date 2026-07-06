---
name: ship-task
version: 2.0.0
description: |
  Autonomous "send a task, walk away" pipeline. Classifies the task by
  complexity and routes it: trivial edits (docs/copy) execute directly; real features
  run the full plan → spec-only test agent (blind to impl) → separate implement agent
  → verify → independent feature-test pipeline. Each run gets its own git worktree
  (off the freshly-pulled base branch, env files symlinked, project setup command
  run). ALL tiers land the same safe way: branch → push → PR to the base branch, then
  HAND OFF at PR-ready — the run reports the PR + test guide and STOPS; it does not
  wait for CI and does not merge. The user watches CI, tests the branch, and merges.
  Use when asked to "ship-task", "autonomously build/fix X", "send this task and
  don't worry about it", or when running unattended (cloud / scheduled agent).
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Agent
  - AskUserQuestion
---

# /ship-task — routed autonomous task → tested → PR-ready hand-off

The invocation argument is the **task description**. Examples:
- `/ship-task "change 'Apps' to 'App' in the README"`  → routes to the fast path
- `/ship-task "add a CSV export button to the items table"` → routes to the full pipeline

Run the steps in order. Every gate is a hard stop: if a gate fails and cannot be
recovered within its capped retries, **STOP and report — never force code through.**

## Paths and settings used below

Project specifics come from the skill package's config (`config.json` in the
sibling manager skill; see its `config.example.json`). Resolve once:

```bash
SKILL_DIR="${SKILL_DIR:-$HOME/.claude/skills/ship-task}"   # where THIS skill is installed
[ -f "$SKILL_DIR/verify.sh" ] || SKILL_DIR="$(pwd)/.claude/skills/ship-task"  # project-level fallback
C(){ python3 "$SKILL_DIR/../manager/skill_config.py" get "$1"; }
REPO="$(C repo)"; BASE="$(C base_branch)"; MODE="$(C push_mode)"
```

`$BASE` (the configured `base_branch`) is the integration branch everywhere
below: the worktree base, the dedup truth, and the PR target.

`$MODE` (config `push_mode`) picks the finish line — resolve it once at start
and announce it with your routing decision:
- **`pr`** (default): commit → push → open a PR to `$BASE` → hand off. Needs
  `gh` — if it's missing, `bash "$SKILL_DIR/../manager/install-gh.sh"` installs
  it without sudo (official release binary into `~/.local/bin`); the user then
  runs `gh auth login` once.
- **`local`**: commit on the local feature branch and STOP. **No push, no PR, no
  `gh`, no network** — the user reviews and merges locally. Everything before
  the finish line (worktree, tests, verify gate, feature-tester) is identical.

## Invariants — true at EVERY tier, no exceptions

The router changes how much *authoring* work happens. It NEVER relaxes landing safety:

- **NEVER** push to, commit to, or merge into any protected branch.
- **NEVER** push directly to `$BASE`. The only path to `$BASE` is a merged PR
  (or, in local mode, the user's own local merge).
- **In local mode, NEVER push or open a PR at all** — the boundary moves
  earlier, never later. A mode can only make the run touch LESS, not more.
- **ship-task STOPS at PR-ready and HANDS OFF.** It pushes the branch, opens the PR,
  and reports immediately — it does **not** block waiting for CI, and it does **not**
  merge. The user watches CI, tests the branch, and merges when green + satisfied.
  (This matches how the run already behaves headless under `/manager`.) By not
  merging at all, the agent can never merge on red or without a human — both are the
  user's call.
- **Always** branch → PR to `$BASE` → CI, even for a one-character doc fix. CI is
  cheap; it is the net that catches a misclassification. Skipping it is exactly how a
  "trivial" change breaks things.
- Authorization for this skill to commit/push/open PRs is scoped to `/ship-task`
  runs only; it does not extend to other sessions.

## Step 0 — Preconditions

```bash
git -C "$REPO" rev-parse --abbrev-ref HEAD
git -C "$REPO" status --porcelain
git -C "$REPO" fetch origin
```

- Non-empty `git status --porcelain` → **STOP**: "Working tree dirty; resolve before
  an autonomous run." (Never fold unrelated changes into the shipped PR.)
- Base all work on the latest `origin/$BASE`.

## Step 1 — Classify & route (the router)

Read the task. Decide a tier from these signals: **what files/types it touches**,
**whether observable behavior changes** (i.e. is there anything a test could
meaningfully assert?), **scope** (files/packages), **risk surface**, and **how
precisely it's specified**.

**Risk override (check FIRST).** If the task plausibly touches any of these, the
minimum tier is **2**, regardless of how small it looks textually:

> auth / JWT / `middleware/` · roles, permissions, access control · DB
> migrations · multi-tenancy / data isolation · `.env`, secrets · CORS ·
> `.github/workflows/**`, deploy config · payment/billing · deletion or any
> destructive/irreversible operation · any term in config `risk_keywords_extra`.

Run the deterministic floor check as a backstop and fold its result into your
decision — if it returns `floor: 2`, you are at **minimum Tier 2** even if the
task read as trivial:

```bash
python3 "$SKILL_DIR/risk_floor.py" "<task description>"
# -> {"floor":2,"matched":[...]} and exit 2 when a risk category is hit; floor 0 / exit 0 otherwise
```

It only RAISES a floor — you still judge the real tier (and may go higher); the
script just guarantees the safety-critical categories never slip onto the fast
path. A one-line config change can be catastrophic — size is not safety.

| Tier | What it is | How it runs |
|---|---|---|
| **0 — Trivial** | No behavior change: docs/markdown, comments, copy, whitespace, a string rename in non-executed text. Nothing meaningful to test. | Main agent edits directly. **No test agent, no impl agent.** → land (Step 6+). |
| **1 — Small** | Localized, fully-specified logic: one pure helper, a small obvious bugfix; single file/package; no risk-override hit. | Main agent writes a focused test, then implements until it passes (TDD, but **no separate/parallel subagents**). → verify → land. |
| **2 — Standard** | Real feature/bugfix, multiple files or genuine logic, OR any risk-override hit. | **Full pipeline:** spec-only blind test agent → separate impl agent → verify → land. |
| **3 — Large / ambiguous** | Cross-cutting, schema/migration, security-sensitive, or under-specified. | Tier-2 pipeline **plus**: if acceptance criteria can't be made testable → STOP and ask; add an adversarial review pass before merge; split into smaller PRs if huge. |

**Rules for the router:**
- **Fail up.** Any doubt between two tiers → pick the higher one.
- **Be explicit.** Emit the decision before proceeding, e.g.:
  `Routed: Tier 0 — README only, no behavior change, no risk paths. Skipping
  test/impl agents; PR+CI+merge invariants retained.` This lets a human catch a
  misroute.
- If Tier 0/1 but you discover during work that it actually changes behavior or
  touches a risk path → **re-classify upward and restart at the right tier.**

## Step 1.5 — Provision an isolated worktree FIRST (all tiers, before any edits)

Every run gets its **own git worktree** off the freshly-pulled base branch. All
authoring — direct edits, test-agent writes, impl-agent writes — happens inside
that worktree, never in the main checkout and never on `$BASE`. Separate worktrees
(not just separate branches in one checkout) are what let multiple `/ship-task`
sessions run truly concurrently without sharing a working tree.

**Do NOT improvise the git/worktree/env/install commands.** Call the static script;
the only things you choose are the two names:

```bash
# Derive <slug> from the task (short, kebab-case). The timestamp suffix keeps parallel
# runs from colliding on a branch/worktree name.
slug="<short-kebab-slug>-$(date +%s)"
"$SKILL_DIR/setup-worktree.sh" "feature/$slug" "$slug"
```

The script (`setup-worktree.sh`) does, deterministically, every run:
1. Refuses to run if the **main checkout is dirty** (exit 3 → STOP).
2. `git pull`s `$BASE`. On a **merge conflict / pull failure** it aborts the merge
   and exits **2** → you MUST **STOP and tell the user**; do not create a worktree or
   continue.
3. Creates `<worktree_dir>/<slug>` on new branch `feature/<slug>` off `$BASE`.
4. Symlinks each config `env_files` entry from the main checkout (gitignored
   secrets shared into the worktree).
5. Runs the config `setup_command` inside the worktree (deps/build). A failure
   is exit **6** → STOP and report.
6. Launches the config `dev_command` **headless** (`nohup`, log in
   `<wt>/.ship-task/dev.log`) so the app runs independent of any editor window,
   serving on the config `dev_port`. Skipped under `/manager` (`MANAGER_MODE`).
   Non-fatal if it can't start — the verify gate is hermetic.

**Handle the exit code before doing anything else:**
- `0` → capture the printed `WORKTREE_PATH=…`. Call it `$WT`. **Every** subsequent
  git / build / test / commit / push command in Steps 2–9 runs inside `$WT`
  (`cd "$WT" && …` or `git -C "$WT" …`), NOT in the main checkout.
- `2` → **STOP**: "the base branch has a merge conflict on pull — resolve it before
  an autonomous run." Report and end the run.
- `3`/`4`/`5`/`6` → **STOP** and report the precondition that failed.

Keep `feature/$slug`, `$slug`, and `$WT` for Steps 6–9 and the Step 11 cleanup.

## Fast path — Tier 0 and Tier 1

Skip Steps 2–4. Instead:

- **Tier 0:** make the edit directly. No tests (nothing to assert).
- **Tier 1:** write one focused, hermetic test for the specified behavior, confirm it
  fails, implement until it passes. Do not over-engineer; do not add parallel agents.

Then go to **Step 5 (verify) → Step 6+ (land)**.

## Step 2 — Test agent (Tier 2–3 only; spec-only, blind to implementation)

First produce, held in context (do not commit): a short plan and **acceptance
criteria** — concrete, individually testable bullets, frozen for the rest of the run.
If the task is too vague to write testable criteria → **STOP** and report exactly
what's ambiguous (do not guess in an unattended run).

**Validate each criterion before dispatch** (a vague criterion makes the test agent
AND impl agent flail at a moving target — the most common source of wasted cycles).
For each bullet check: (1) it names **one observable behavior**, not a goal like
"improve performance"; (2) a test could **hermetically assert** it (else defer that
part to Step 5.5's live-app check); (3) it's **independent** — testable in isolation.
Rewrite any bullet that fails the check, or **STOP** if it can't be made concrete.

**Learn the repo's test conventions first** (once per run): find how existing
tests run (`package.json` scripts, `Makefile`, CI workflow files, the config
`verify_command`) and where they live — then hand those conventions to the agents
instead of letting each rediscover them.

Dispatch a subagent (`subagent_type: general-purpose`) with **only**: the task
description, the frozen acceptance criteria, and the repo test conventions you
just gathered (runner, file locations, how to run one file). Instruct it:

> Write a **failing** test suite encoding each acceptance criterion. The feature does
> not exist yet — do NOT implement it, do NOT search for or read feature
> implementation, only read test infra/config. Tests must be **hermetic** (no live
> network/DB/external services — mock/stub). Return exact test paths + run command.

This agent is structurally blind to the implementation (it does not exist yet). That
is what makes the gate trustworthy.

## Step 3 — Confirm red (Tier 2–3)

Run the returned command. Expect **failure**. If it already **passes** with no
implementation → tautological tests or behavior already exists → **STOP** and report.

## Step 4 — Implement agent (Tier 2–3; separate context)

**First, locate (don't make the impl agent search from scratch).** Read the failing
test(s), pull the symbols / import paths / mocked functions / routes they reference,
and grep the repo for them → a short **target list**: the exact files + functions to
touch across the vertical slice (e.g. server handler + route registration · shared
types/functions · client wiring — whatever layers this repo has). This alone
prevents wrong-place edits (e.g. client instead of server) and saves the impl
agent a full re-exploration each run.

Dispatch a *separate* subagent (`subagent_type: general-purpose`) with: the task, the
frozen acceptance criteria, **the target list from above**, **the full source of the
test files** (pre-read and paste it — the contract; don't make it re-read), and a
pointer to **`$SKILL_DIR/BUILDER_PLAYBOOK.md`** (the repo orientation + footguns —
customize that file per project; see its header). **Substitute the absolute worktree
path for `$WT` in everything you pass** (the subagent's shell does NOT inherit
`$WT`) and tell it: "your worktree root is `<abs path>` — run every command there."
Instruct it:

> Read `$SKILL_DIR/BUILDER_PLAYBOOK.md` first, then implement following
> its patterns. Make the tests at the given paths pass (their source is above — the
> contract). You may NOT modify, delete, weaken, or skip any test, nor weaken the
> acceptance criteria. No stubs/TODOs/disabled code — do the FULL vertical slice
> across every layer the change touches, per the playbook.
>
> **Fast inner loop:** iterate on ONLY the focused acceptance test (the repo's
> single-file test command, run inside "$WT"). Do **not** run the full
> `verify.sh` each cycle. When the focused test is green, run
> `"$SKILL_DIR/verify.sh" --root "$WT"` **once** to confirm no
> regressions, fix any, then return only when both are green.

## Step 5 — Verify gate (ALL tiers; hard gate)

Run the whole gate as **one command**. `verify.sh` runs the project's configured
`verify_command` (lint + typecheck + tests — whatever the project defines) rooted
at the worktree, and prints a compact `PASS`/`FAIL` summary, so you read one short
result instead of a verbose log.

```bash
"$SKILL_DIR/verify.sh" --root "$WT"
# Tier 0 (nothing new to test — the configured verify_command still runs):
"$SKILL_DIR/verify.sh" --root "$WT" --no-tests
```

The gate passes **iff** the final line is `VERIFY RESULT: PASS` (exit 0). On any
`FAIL`, that check's output is printed inline — use it as the failure summary.

On failure:
- **Test-drift check first** — `git -C "$WT" diff --name-only` against the frozen
  test paths from Step 2. If the impl agent touched a test file, **reject that change,
  `git -C "$WT" checkout -- <test>` to restore it, and retry** (a "passing" run that
  edited the test is invalid).
- **Root-cause escalation ladder (Tier 2–3, max 5 attempts)** — don't blind-retry:
  - Attempts 1–2: re-dispatch the impl agent with the failure output.
  - Attempt 3, still red: run **`superpowers:systematic-debugging`** / invoke
    **`/investigate`** for the root cause, then re-dispatch with the finding.
  - Attempt 4, still red: invoke **`/codex`** challenge on the diff + failure to
    surface a wrong structural assumption, then re-dispatch. (Skip if the skill
    isn't installed — go straight to attempt 5's stop.)
  - Attempt 5, still red → **STOP**.
- Tier 0–1: fix directly, **max 3 attempts**.
- After the cap → **STOP**, do not push, report the failure summary.

## Step 5.5 — Feature-tester loop (Tier 2–3 only)

`verify.sh` proved the code builds and the suite passes — but hermetically. It
says nothing about whether the **feature actually behaves** as intended. Before
committing, prove the ONE key user-facing behavior with an independent
integration test, via the **`/feature-tester`** skill
(`$SKILL_DIR/../feature-tester/SKILL.md`).

**Applies to Tier 2–3 only.** Tier 0 has nothing to test; Tier 1's single focused
test already ran at the verify gate. For those, **record a skipped verdict so the
commit gate is satisfied** and move on:
`"$SKILL_DIR/../feature-tester/feature-test-gate.sh" --root "$WT" --record skipped --reason "tier 0/1"`.
Under /manager (`$MANAGER_URL` set), also surface it so the card shows `🧪 skipped`:
`curl -s -X POST "$MANAGER_URL/api/featuretest" -H 'Content-Type: application/json' -d '{"session_id":"'"$MANAGER_SESSION_ID"'","verdict":"skipped","cycles":0}'`.

This step is **code-enforced**, not optional: Step 6 runs the gate and refuses to
commit unless a verdict (`pass`/`blocked`/`skipped`) is recorded — a skipped or
red feature test cannot reach a committed branch. The loop below records `pass`,
`blocked`, or (on exhaustion) `fail` itself.

Run the feature-tester loop (read its SKILL.md for the full contract). **All test
authoring + runs happen inside the worktree `$WT`**, like every other step:
1. **Name the ONE key behavior** the feature must exhibit (e.g. "in the team view
   the Chats item appears and clicking it stays in team view").
2. **Dispatch a FRESH `general-purpose` subagent** (the *tester*) — give it the
   key behavior, **the worktree root as an absolute path** (substitute the real
   value for `$WT`; the subagent's shell doesn't inherit it) — it must write the
   test under that root and run it from there — the repo's test conventions (point
   it at a neighboring test to mirror), and the worktree-rooted run command.
   **Withhold the implementation diff and your reasoning** — its independence is
   the point. It authors ONE focused integration test in the repo's test stack
   under `$WT`, runs it once, and returns the verdict object
   (`pass` | `fail` | `blocked`).
3. **You are the builder.** On `blocked`, don't fix-loop — delegate to a live-app
   check (`/browse` if installed) or defer to the human gate. On `fail`, fix the
   **implementation** (never weaken the test) and re-run it with
   `"$SKILL_DIR/../feature-tester/run-focused-test.sh" --root "$WT" --cmd "<the tester's run command>"`
   — automatic loop, **cap 5**. Keep `verify.sh --root "$WT"` green. At cycle 3
   invoke `/investigate`; at cycle 4 `/codex` challenge (each only if installed).
4. **Final confirmation:** once green, dispatch ONE more fresh tester subagent to
   independently re-run it and confirm it still asserts intent (not a weakened
   version). The test (written under `$WT`) is committed with the feature at Step 6.

On **exhaustion** (still red after the cap) → **STOP** (do not push). Report the
failing test path + last logs + the builder's cycle count. The two modes are
**mutually exclusive**:
- **Under /manager** (`$MANAGER_URL` set): do **NOT** call `AskUserQuestion` (it
  hangs headless). POST the `fail` verdict (same `/api/featuretest` as the other
  outcomes, so the card gets a `🧪 fail` chip + the turns timeline) and finish:
  `curl -s -X POST "$MANAGER_URL/api/featuretest" -H 'Content-Type: application/json'
  -d '{"session_id":"'"$MANAGER_SESSION_ID"'","verdict":"fail","test":"<path>","cycles":<N>,"output":"<tail>","turns":[...]}'`
  (`$MANAGER_URL` + `$MANAGER_SESSION_ID` are exported by dispatch.sh — the same id
  as `/api/runinfo`; `turns` notes plain ASCII, no quotes). Do not block on an answer.
- **Standalone** (no `$MANAGER_URL`): surface via `AskUserQuestion`.

## Step 6 — Commit & push the feature branch

Only on a fully green verify gate **and** a passing (or skipped/blocked) Step 5.5
feature-tester. The worktree `$WT` is **already** on `feature/$slug`
(created in Step 1.5); do NOT re-create or reset the branch — that would discard your
edits. All commands run inside `$WT`.

```bash
git -C "$WT" rev-parse --abbrev-ref HEAD   # sanity check: must be feature/$slug
# HARD GATE: a feature-test verdict must be recorded and not `fail` (Step 5.5).
# Exit 1 here = STOP: do not commit (a skipped or red feature test never lands).
"$SKILL_DIR/../feature-tester/feature-test-gate.sh" --root "$WT" || { echo "feature-test gate failed — STOP"; exit 1; }
git -C "$WT" add -A
git -C "$WT" commit -m "<type>: <short imperative summary>"
# PR mode only — in local mode ($MODE = local) do NOT run this line:
git -C "$WT" push -u origin "feature/$slug"
```

**Local mode short-circuit:** after the commit, skip Steps 7–9 entirely and go
straight to the hand-off report (Step 8's local variant). Do not push, do not
touch `gh`.

## Step 7 — Open the PR to the base branch (PR mode only)

```bash
cd "$WT"
gh pr create --base "$BASE" --head "feature/$slug" \
  --title "<short title>" \
  --body "$(cat <<'EOF'
Autonomous /ship-task run · Tier <N> (<one-line routing reason>)

## Task
<the task description>

## Acceptance criteria / change summary
<frozen bullets for Tier 2–3, or a one-line summary for Tier 0–1>

## Tests
<test files + red→green note, or "Tier 0: docs-only, no tests">

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Step 8 — Hand off (ALL tiers)

**Local mode variant** (`$MODE` = local): the finish line is the committed
branch. Report immediately:

- **What shipped** — one line + tier + `Mode: local (no push)`.
- **The branch** — `feature/$slug`, living in worktree `$WT`. Review it with:
  `git -C "$REPO" diff "$BASE"..."feature/$slug"`.
- **How to test** — same as below (the worktree's dev server / commands).
- **Merge when ready** — the user's action, locally:
  `git -C "$REPO" merge --squash "feature/$slug" && git -C "$REPO" commit`
  (then the teardown command from Step 11 removes the worktree + branch).
- Nothing left the machine. If they later want a PR instead, one command from
  the worktree does it: `git -C "$WT" push -u origin "feature/$slug" && cd "$WT" && gh pr create --base "$BASE"`.

Then the run is **done**. The rest of this step is the **PR-mode variant**:

The branch is pushed and the PR is open. **Do NOT block on `gh pr checks --watch`.**
CI runs on GitHub and takes a few minutes; you don't sit and wait for it, and you
do **not** merge. Instead, **report immediately as a finished hand-off** — the user
watches CI, tests the branch, and merges. Capture the PR URL and CI link once, no
polling:

```bash
cd "$WT"
gh pr view --json url,number -q '.url'                 # PR link for the report
gh pr checks 2>/dev/null | head -1 || true             # one-shot status snapshot (no --watch)
```

Then give the user the **hand-off report** (this IS the final Step 10 report —
write it as if the run is complete; the user does the waiting):

- **What shipped** — one line + tier.
- **PR** — the link, with: "CI is running — it goes green in a few minutes; watch it
  with `gh pr checks <#> --watch` or on the PR page."
- **How to test the branch** — the exact, minimal steps against this worktree's
  dev server (launched in Step 1.5, serving on the config `dev_port`) — or, if it
  wasn't launched, the one command to start it (`cd "$WT" && <dev_command>`).
  Cover: page/URL + action, or for pure logic / Tier 0–1 the command/file to
  eyeball. State the expected result. Logs: `tail -f "$WT/.ship-task/dev.log"`.
- **Merge when ready** — "once CI is green and you're satisfied:
  `gh pr merge <#> --squash --delete-branch`." The agent does **not** run this.

Then the run is **done**. Do not poll CI, do not call `AskUserQuestion`, do not merge.

> Under `/manager`, this is exactly the relay's stop-at-PR-ready behavior (it POSTs
> the PR + test guide to the panel via `/api/runinfo` and stops). Standalone now
> behaves the same: submit, report, hand off.

## Step 9 — (the user merges)

Merging is the **user's** action, from the report's instructions — after CI is
green and they've tested (PR mode: `gh pr merge <#> --squash --delete-branch`;
local mode: the local `merge --squash` from the report). ship-task never runs
either. (CI being red, or the branch needing changes, is also the user's call — a
follow-up `/ship-task` run can address requested changes on the same branch.)

## Step 10 — Report

Step 8 already IS the success report (the hand-off). Use this shape (+ a push
notification if available):

- ✅ Submitted (PR-ready): tier + routing reason, PR # + link, what changed, the
  feature-test/verify result, the test guide for the branch, the CI-watch hint, and
  the merge command for the user to run when green. This is a *success* — reaching a
  pushed PR is the finish line; CI + merge are the user's to complete.
- 🛑 Stopped at Step N: exact reason, PR/branch link if any, what a human must
  decide. Leave branch/PR intact for inspection.

A pushed PR with the hand-off report = success. Do not wait for CI or merge to
declare it; never push on a failed verify or feature-test gate.

## Step 11 — Worktree cleanup (deferred; the worktree STAYS for testing)

Because the run hands off at PR-ready (Step 8), **leave `$WT` and its dev server
running** — the user needs them to test the branch. Do **not** ask about cleanup at
submit (that would block the hand-off) and never auto-remove it. Just include the
worktree path + teardown command in the report so the user can remove it **after
they've merged**:

```bash
# run by the user once the PR is merged:
"$SKILL_DIR/teardown-worktree.sh" "$slug" --delete-branch "feature/$slug"
```

The teardown script stops the worktree's headless dev server (by the PID recorded
in `<wt>/.ship-task/`, with a `pkill -f "$WT"` fallback), runs
`git worktree remove --force`, prunes, and deletes the branch only if it's merged.
(Under `/manager`, cleanup is the panel's per-worktree button — same deferred model.)

## Experience — debugging lessons (learned the slow way)

Distilled from real multi-hour investigations that should have been short. Apply
these when a task is "fix bug X" rather than "build feature Y".

### Get ground-truth data BEFORE theorizing
- The biggest time sink in one real case was hypothesizing a cause (a missing
  linking row in one table) and writing prevention + self-heal code for it — then
  the actual DB showed that row was present and the real gap was a *different*
  table with zero rows. **One data query up front would have killed the wrong
  hypothesis immediately.**
- Rule: for a data/permission bug, read the actual rows before writing any fix. If
  you "can't reach the DB," that's the first blocker to solve — find the
  credentials in the project's env/docs, or ask for them — not a license to guess.

### Diff broken-vs-working
- When one account/tenant works and another doesn't, **diff them across every table
  in the chain**, don't read one in isolation. A `6 rows vs 0 rows` difference
  jumps out the instant they're side by side.

### Read the handler log at failure time; reproduce at the API, not the UI
- The failing server handler usually names the failing path in its log line. Grab
  that first.
- Hit the endpoint directly with the user's token/session to see the real HTTP
  status + log line. A "can see it but can't open it" symptom = the **read/list
  path and the enter/write path use different authorization sources** — that
  divergence is the bug. Fix toward one shared `canAccess(user, resource)`;
  invariant: *listable ⇒ enterable*.

### Creation paths diverge — check every one
- Self-created vs invited/imported/migrated records often write **different rows**
  (one creation path provisions a linking record; another never did). When a bug
  only hits one class of entity, compare what each creation path writes.

### "Patched" ≠ "root-caused" — say which
- Inserting the missing rows fixes the one stale account but does **not** explain
  why the provisioning never fired. Always state plainly whether you've repaired an
  instance or fixed the cause, and flag the still-open root cause rather than
  implying it's solved.

### Infra/config failures masquerade as code bugs
- A `404` can be a known not-yet-implemented endpoint (caught + ignored
  client-side), not the bug. A `500` can be a missing DB column. Emails silently
  not sending can be invalid provider credentials, not code. Before editing code,
  check: does the route exist? is the column there? are the creds valid? Decode
  the error code — it points to infra vs. code.
