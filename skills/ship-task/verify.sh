#!/usr/bin/env bash
#
# verify.sh — ship-task's verify gate as ONE command with a compact, greppable
# PASS/FAIL summary. It runs the project's configured `verify_command`
# (config.json — e.g. "npm run lint && npm test", or a script the project
# provides) rooted at the worktree, and prints a short result — so the agent
# reads one summary instead of a verbose log (fewer tokens, one round-trip).
#
# Usage:
#   verify.sh [--root <dir>] [--no-tests]
#   verify.sh --selftest        # validate the harness itself (no repo needed)
#
#   --root <d>  worktree root to verify (default: $PWD; should be "$WT").
#   --no-tests  accepted for Tier-0 compatibility. The full verify_command still
#               runs (a docs-only change should pass it quickly); the flag only
#               annotates the output. If your suite is too slow for that, point
#               verify_command at a script that branches on $VERIFY_NO_TESTS=1.
#
# Output: one line per check —
#   VERIFY <check>:   PASS (Ns)
#   VERIFY <check>:   FAIL (Ns, exit C)   + the last 40 lines of that check
# Final line + exit code:
#   VERIFY RESULT: PASS (N checks)   -> exit 0
#   VERIFY RESULT: FAIL — <names>    -> exit 1
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_HELPER="$SKILL_DIR/../manager/skill_config.py"   # sibling skill; single config
cfg() { python3 "$CONFIG_HELPER" get "$1"; }

NO_TESTS=false
ROOT="$PWD"
MODE="verify"     # verify|selftest

while [ $# -gt 0 ]; do
  case "$1" in
    --no-tests) NO_TESTS=true; shift ;;
    --root)     ROOT="${2:-}"; shift 2 ;;
    --selftest) MODE="selftest"; shift ;;
    --scope)    shift 2 ;;   # legacy flag; scope is the project verify_command's business now
    -h|--help)  sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "verify.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

PASSED=0
FAILED=()

run_check() {  # <name> <bash-script-string>  — runs in a subshell rooted at $ROOT
  local name="$1" script="$2" tmp rc start end dur
  tmp="$(mktemp)"
  start=$(date +%s)
  ( cd "$ROOT" && bash -c "$script" ) >"$tmp" 2>&1
  rc=$?
  end=$(date +%s); dur=$((end - start))
  if [ "$rc" -eq 0 ]; then
    printf 'VERIFY %-16s PASS (%ds)\n' "$name:" "$dur"
    PASSED=$((PASSED + 1))
  else
    printf 'VERIFY %-16s FAIL (%ds, exit %d)\n' "$name:" "$dur" "$rc"
    echo "  ── last 40 lines ─────────────────────────────"
    tail -n 40 "$tmp" | sed 's/^/  /'
    echo "  ──────────────────────────────────────────────"
    FAILED+=("$name")
  fi
  rm -f "$tmp"
}

finalize() {
  echo "──────────────────────────────────────────────"
  if [ "${#FAILED[@]}" -eq 0 ]; then
    echo "VERIFY RESULT: PASS ($PASSED checks)"
    exit 0
  fi
  echo "VERIFY RESULT: FAIL — ${FAILED[*]}"
  exit 1
}

# --- harness self-test: one passing + one failing check, no repo needed --------
if [ "$MODE" = "selftest" ]; then
  ROOT="$(mktemp -d)"
  run_check "selftest-pass" 'true'
  run_check "selftest-fail" 'echo "simulated failure output"; exit 7'
  echo "(expected: 1 PASS, 1 FAIL, RESULT FAIL, exit 1)"
  finalize
fi

VERIFY_CMD="$(cfg verify_command)"
if [ -z "$VERIFY_CMD" ]; then
  echo "verify.sh: no verify_command configured — set it in the manager skill's config.json" >&2
  exit 2
fi

if $NO_TESTS; then
  echo "VERIFY note:      --no-tests (Tier 0) — running the configured verify_command anyway; VERIFY_NO_TESTS=1 is exported for project scripts that want to branch on it"
  export VERIFY_NO_TESTS=1
fi
echo "VERIFY command:   $VERIFY_CMD"
run_check "verify" "$VERIFY_CMD"

finalize
