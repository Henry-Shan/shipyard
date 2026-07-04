#!/usr/bin/env bash
#
# feature-test-gate.sh — RECORD and ENFORCE the Step 5.5 feature-test verdict, so
# the loop can't be silently skipped. The loop must drive to a SHOWN result
# (pass | blocked | fail), record it here, and the commit step CHECKS it: a
# missing result or a `fail` verdict blocks the commit (exit 1).
#
#   record:  feature-test-gate.sh --root "$WT" --record <pass|fail|blocked|skipped> \
#              [--test <path>] [--cycles <N>] [--reason "<text>"]
#   check :  feature-test-gate.sh --root "$WT"
#   selftest: feature-test-gate.sh --selftest
#
# State file: <root>/.feature-test-result.json  {verdict,test,cycles,reason,at}
# CHECK exit 0 iff a result exists AND verdict in {pass, blocked, skipped};
#       exit 1 if the file is missing (loop skipped) or verdict == fail.
set -uo pipefail

ROOT="$PWD"; MODE="check"; VERDICT=""; TEST=""; CYCLES="0"; REASON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root)     ROOT="${2:-}"; shift 2 ;;
    --record)   MODE="record"; VERDICT="${2:-}"; shift 2 ;;
    --test)     TEST="${2:-}"; shift 2 ;;
    --cycles)   CYCLES="${2:-0}"; shift 2 ;;
    --reason)   REASON="${2:-}"; shift 2 ;;
    --selftest) MODE="selftest"; shift ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "feature-test-gate: unknown arg: $1" >&2; exit 2 ;;
  esac
done

valid_verdict() { case "$1" in pass|fail|blocked|skipped) return 0;; *) return 1;; esac; }

do_record() {  # $1=root $2=verdict $3=test $4=cycles $5=reason
  valid_verdict "$2" || { echo "feature-test-gate: verdict must be pass|fail|blocked|skipped, got '$2'" >&2; exit 2; }
  V="$2" T="$3" C="$4" R="$5" python3 - "$1/.feature-test-result.json" <<'PY'
import json, os, sys, time
path = sys.argv[1]
try:
    cycles = int(os.environ.get("C") or 0)
except ValueError:
    cycles = 0
json.dump({
    "verdict": os.environ["V"],
    "test": os.environ.get("T", ""),
    "cycles": cycles,
    "reason": os.environ.get("R", ""),
    "at": time.time(),
}, open(path, "w"), indent=2)
print(f"feature-test-gate: recorded verdict={os.environ['V']} -> {path}")
PY
}

do_check() {  # $1=root
  local file="$1/.feature-test-result.json"
  if [ ! -f "$file" ]; then
    echo "FEATURE-TEST GATE: FAIL — no result recorded ($file missing)."
    echo "  Step 5.5 did not run to a shown result. Record one before committing:"
    echo "  feature-test-gate.sh --root \"\$WT\" --record <pass|blocked|skipped>"
    return 1
  fi
  local verdict
  verdict="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict",""))' "$file" 2>/dev/null)"
  case "$verdict" in
    pass|blocked|skipped)
      echo "FEATURE-TEST GATE: PASS (verdict=$verdict)"; return 0 ;;
    fail)
      echo "FEATURE-TEST GATE: FAIL — feature test is still red (verdict=fail). Do not commit."; return 1 ;;
    *)
      echo "FEATURE-TEST GATE: FAIL — unreadable/empty verdict in $file."; return 1 ;;
  esac
}

case "$MODE" in
  record)
    do_record "$ROOT" "$VERDICT" "$TEST" "$CYCLES" "$REASON" ;;
  selftest)
    tmp="$(mktemp -d)"
    do_record "$tmp" pass "x.test.tsx" 2 "" >/dev/null
    do_check "$tmp" >/dev/null && echo "selftest: pass-verdict -> gate PASS OK"
    do_record "$tmp" fail "x.test.tsx" 5 "" >/dev/null
    do_check "$tmp" >/dev/null && echo "selftest: FAIL (fail-verdict should block)" || echo "selftest: fail-verdict -> gate BLOCK OK"
    rm -f "$tmp/.feature-test-result.json"
    do_check "$tmp" >/dev/null 2>&1 && echo "selftest: FAIL (missing should block)" || echo "selftest: missing-result -> gate BLOCK OK"
    rm -rf "$tmp"
    echo "(expected: PASS OK, BLOCK OK, BLOCK OK)" ;;
  check)
    do_check "$ROOT" ;;
esac
