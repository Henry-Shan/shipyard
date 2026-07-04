#!/usr/bin/env bash
#
# run-focused-test.sh — run ONE focused test (the feature-tester's key-behavior
# test) and print a compact, greppable PASS/FAIL with the failure tail. Keeps the
# builder's tight fix loop to a single tool round-trip instead of a verbose log.
#
# Usage:
#   run-focused-test.sh --root <dir> --cmd "<test command>" [--name <label>]
#   run-focused-test.sh --selftest        # validate the harness (no repo needed)
#
#   --root <dir>   repo / worktree root the command runs from (default: $PWD;
#                  should be "$WT")
#   --cmd "<...>"  the focused test command, exactly as the project runs one
#                  test (e.g. "npx vitest run tests/unit/x.test.tsx",
#                  "go test ./internal/foo -run TestBar -count=1",
#                  "pytest tests/test_x.py::test_key_behavior")
#   --name <l>     optional display label (default: the command)
#
# Output:
#   FOCUSED-TEST <name>: PASS (Ns)
#   FOCUSED-TEST <name>: FAIL (Ns, exit C)   + last 40 lines on failure
# Exit code mirrors the test (0 pass, non-zero fail) so the loop can branch on it.
set -uo pipefail

MODE=""          # cmd|selftest
CMD=""
NAME=""
ROOT="$PWD"

while [ $# -gt 0 ]; do
  case "$1" in
    --cmd)      MODE="cmd"; CMD="${2:-}"; shift 2 ;;
    --name)     NAME="${2:-}"; shift 2 ;;
    --root)     ROOT="${2:-}"; shift 2 ;;
    --selftest) MODE="selftest"; shift ;;
    -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "run-focused-test.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

run() {  # <name> <cwd> <command-string>
  local name="$1" cwd="$2" cmd="$3"
  local tmp rc start end dur
  tmp="$(mktemp)"
  start=$(date +%s)
  ( cd "$cwd" && bash -c "$cmd" ) >"$tmp" 2>&1
  rc=$?
  end=$(date +%s); dur=$((end - start))
  if [ "$rc" -eq 0 ]; then
    printf 'FOCUSED-TEST %s: PASS (%ds)\n' "$name" "$dur"
  else
    printf 'FOCUSED-TEST %s: FAIL (%ds, exit %d)\n' "$name" "$dur" "$rc"
    echo "── last 40 lines ─────────────────────────────"
    tail -n 40 "$tmp"
    echo "──────────────────────────────────────────────"
  fi
  rm -f "$tmp"
  return "$rc"
}

case "$MODE" in
  selftest)
    run "selftest-pass" "$(mktemp -d)" true
    echo "(expected: 1 PASS, exit 0)"
    exit 0 ;;
  cmd)
    [ -n "$CMD" ] || { echo "run-focused-test.sh: --cmd needs a command" >&2; exit 2; }
    run "${NAME:-$CMD}" "$ROOT" "$CMD"
    exit $? ;;
  *)
    echo "run-focused-test.sh: need --cmd \"<test command>\" | --selftest" >&2
    exit 2 ;;
esac
