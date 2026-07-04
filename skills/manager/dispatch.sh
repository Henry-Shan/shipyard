#!/usr/bin/env bash
#
# dispatch.sh — launch ONE headless /ship-task run for the manager.
#
# ⚠ SECURITY: this launches `claude` with --dangerously-skip-permissions by
# default (override via MANAGER_PERMS). The dispatched agent can edit files,
# run commands, push branches, and open PRs in the configured repo — read this
# file before first use. Runs stop at PR-ready and NEVER merge.
#
# The manager daemon calls this in a worker thread, one per task. ship-task
# itself owns the worktree/branch/PR/CI machinery (Step 1.5+); we only:
#   - run it non-interactively with a fixed session id (so logs/relay line up),
#   - inject a RELAY preamble so that anywhere ship-task would call AskUserQuestion
#     (e.g. a feature-tester exhaustion report), it instead posts to the manager and
#     polls for the human's answer. AskUserQuestion blocks forever in headless mode;
#     the relay is what makes "walk away" work.
#
# Args:  <task-description>  <session-id>  <log-path>  <manager-url>
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfg() { python3 "$HERE/skill_config.py" get "$1"; }

TASK="${1:?task description required}"
SESSION_ID="${2:?session id required}"
LOG_PATH="${3:?log path required}"
MANAGER_URL="${4:-http://127.0.0.1:$(cfg panel_port)}"
REPO="$(cfg repo)"
BASE_BRANCH="$(cfg base_branch)"
MEMORY_MCP="$(cfg memory_mcp)"

# Permission posture for an unattended run. ship-task is authorized to
# branch/push/PR under the manager; without skipping prompts a headless run
# stalls on the first tool permission. Override via MANAGER_PERMS if you want a
# tighter posture (e.g. "--permission-mode acceptEdits").
PERMS="${MANAGER_PERMS:---dangerously-skip-permissions}"
MODEL_FLAG=""
[ -n "${MANAGER_MODEL:-}" ] && MODEL_FLAG="--model ${MANAGER_MODEL}"

# Tell ship-task's setup-worktree.sh NOT to launch a per-worktree dev server.
# N parallel runs each spinning a full dev stack would fight over ports and any
# shared backing services. The human tests one branch on demand from the panel.
export MANAGER_MODE=1
# Expose the manager URL + this run's session id to the run's environment so
# in-run steps (e.g. the feature-tester's verdict report → POST /api/featuretest)
# can reach the panel and correlate to this run's row without re-deriving them.
# MANAGER_URL tracks the panel port (passed by managerd.py); MANAGER_SESSION_ID
# is the SAME id used for /api/runinfo and /api/ask.
export MANAGER_URL
export MANAGER_SESSION_ID="$SESSION_ID"

# How the run should ground unresolved questions: the project memory MCP when
# one is configured, otherwise the repo itself + its docs + git history.
if [ -n "$MEMORY_MCP" ]; then
  GROUNDING="read the repo, query the project's \`$MEMORY_MCP\` memory MCP, check git history and project docs"
else
  GROUNDING="read the repo, check git history (git log origin/$BASE_BRANCH), and the project docs (CLAUDE.md, README)"
fi

read -r -d '' RELAY <<EOF
─────────────────────────────────────────────────────────────────────
MANAGER-MODE: AUTONOMOUS, STOP AT PR-READY (overrides ship-task's gates)

You are running headless under the manager orchestrator. There is NO terminal,
so AskUserQuestion hangs forever — never call it.

STOP LINE = PR-READY. Run the full ship-task pipeline up to: branch -> commit ->
push -> open PR to ${BASE_BRANCH}. Then STOP and HAND OFF — do NOT wait for CI and
do NOT merge (ship-task already hands off at PR-ready; the human watches CI + merges
from GitHub). As soon as the PR is open, report it + a copyable MANUAL-TEST GUIDE:
  curl -s -X POST ${MANAGER_URL}/api/runinfo -H 'Content-Type: application/json' \\
    -d '{"session_id":"${SESSION_ID}","pr_url":"<PR url>","test_plan":"<guide>"}'
Then finish with a one-line summary. Reaching a green PR = success.

THE TEST GUIDE (test_plan) is for a non-engineer to verify your change by hand.
Make it CONCISE and CONCRETE — numbered steps, each naming: which ACCOUNT/ROLE to
log in as (if the app has auth — use the project's real seeded/fixture test
accounts; grep the repo, seed scripts, and docs to pick the right one for THIS
feature; NEVER invent an account), WHERE to go (page + nav), WHAT to click, WHAT
they should see, and a final "✅ Success =" line. If two roles are involved, show
both. Keep it under ~12 lines.

INVESTIGATE BEFORE ASKING. If something is unclear, resolve it yourself first:
${GROUNDING}. Only escalate a
genuine PREFERENCE you cannot determine from evidence (never a factual lookup),
phrased simply with your recommendation:
  1. POST {"session_id":"${SESSION_ID}","question":"<plain question>",
           "options":["<opt>", ...],"context":"<why + your recommendation>"}
       to ${MANAGER_URL}/api/ask
  2. Poll ${MANAGER_URL}/api/answer?session_id=${SESSION_ID} every 10s until
     "answer" is non-null, then continue with that decision.

Every other ship-task invariant (blind test agent, verify gate, green CI) stays.

DEV SERVERS: do NOT start this worktree's dev servers. Many runs go in parallel;
N live dev stacks would fight over ports and any shared backing services. The
verify gate is hermetic and needs no live server. The human starts ONE test
server on demand from the panel. Just report the PR url to
${MANAGER_URL}/api/runinfo once it's open.
─────────────────────────────────────────────────────────────────────
EOF

cd "$REPO" || { echo "repo not found: $REPO — set \"repo\" in the skill's config.json" >&2; exit 4; }

# MANAGER_SMOKE=1 validates the launcher + daemon dispatcher + relay against a
# REAL headless claude, without running the full ship-task PR cycle. The prompt
# exercises exactly the relay contract a parked ship-task run would use.
if [ "${MANAGER_SMOKE:-}" = "1" ]; then
  SMOKE="SMOKE TEST — do exactly this with the Bash tool, nothing else:
1. POST a question to the manager relay:
   curl -s -X POST ${MANAGER_URL}/api/ask -H 'Content-Type: application/json' -d '{\"session_id\":\"${SESSION_ID}\",\"question\":\"Smoke: pick a color\",\"options\":[\"red\",\"blue\"],\"context\":\"manager relay smoke test\"}'
2. Poll for the human answer up to 20 times, sleeping 3s between:
   curl -s \"${MANAGER_URL}/api/answer?session_id=${SESSION_ID}\"
   Stop as soon as the JSON \"answer\" field is non-null.
3. Print exactly:  SMOKE_RELAY_OK got=<the answer>   and finish."
  # shellcheck disable=SC2086
  exec claude -p "$SMOKE" \
    --session-id "$SESSION_ID" \
    --output-format stream-json --verbose \
    $PERMS $MODEL_FLAG
fi

# RESUME_SESSION (set by the manager when a prior attempt was interrupted, e.g. a
# rate limit) continues that exact session — its worktree, branch and partial
# work are still on disk — instead of starting the task over from scratch.
if [ -n "${RESUME_SESSION:-}" ]; then
  # shellcheck disable=SC2086
  exec claude --resume "$RESUME_SESSION" \
    -p "Resume this ship-task run where it left off — your worktree, branch and
any partial work are intact. Re-orient (run git status in your worktree, check
the last ship-task step you completed) and continue to a green PR.

${RELAY}" \
    --output-format stream-json --verbose \
    $PERMS $MODEL_FLAG
fi

# shellcheck disable=SC2086
exec claude -p "/ship-task '${TASK}'

${RELAY}" \
  --session-id "$SESSION_ID" \
  --output-format stream-json --verbose \
  $PERMS $MODEL_FLAG
