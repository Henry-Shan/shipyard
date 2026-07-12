#!/usr/bin/env bash
#
# view.sh — ONE command to SEE your manager board (no orchestration, no tokens).
#
#     bash ~/.claude/skills/manager/view.sh
#
# Brings the local control panel up in your browser, and — if this machine has
# saved cloud relay config (relay.json) — keeps the outbound bridge running so
# the same board shows on the hosted dashboard too. Nothing to type: the relay
# URL + machine token are read from relay.json (saved once via
# `python3 relay_client.py --save`). Idempotent and safe to re-run.
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# panel-up.sh does the work: (re)starts the daemon, opens the browser tab, and
# starts the relay bridge when relay.json is present. We just summarize.
out="$(bash "$HERE/panel-up.sh" 2>/dev/null)"
url="$(printf '%s\n' "$out"   | sed -n 's/^PANEL_URL=//p')"
relay="$(printf '%s\n' "$out" | sed -n 's/^RELAY_BRIDGE=//p')"

echo
echo "  Local board:  ${url:-http://127.0.0.1:4317}"
if [ -f "$HERE/relay.json" ]; then
  cloud="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("relay_url",""))' "$HERE/relay.json" 2>/dev/null)"
  echo "  Cloud board:  ${cloud:-(configured)}   (bridge: ${relay:-off})"
else
  echo "  Cloud board:  not set up"
  echo "                enable once with:"
  echo "                MANAGER_RELAY_URL=<url> MANAGER_MACHINE_TOKEN=<token> \\"
  echo "                python3 \"$HERE/relay_client.py\" --save"
fi
echo
