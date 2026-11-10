#!/usr/bin/env bash
#
# view.sh — ONE command to SEE your manager board (no orchestration, no tokens).
#
#     bash ~/.claude/skills/manager/view.sh
#
# Brings the local control panel up in your browser. Idempotent and safe to
# re-run: panel-up.sh (re)starts the daemon and opens the tab; we just summarize.
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

out="$(bash "$HERE/panel-up.sh" 2>/dev/null)"
url="$(printf '%s\n' "$out" | sed -n 's/^PANEL_URL=//p')"

echo
echo "  Board:  ${url:-http://127.0.0.1:4317}"
echo
