#!/usr/bin/env bash
#
# panel-up.sh — the WHOLE of /manager Phase 0 as one deterministic command, so
# the panel is in the user's browser within ~2 seconds of typing /manager
# (instead of the agent hand-rolling init/serve/beacon/register as separate
# tool calls).
#
# Does, in order:
#   1. managerd init          (create/upgrade the queue DB)
#   2. managerd serve         (detached; restart-first, so it always runs the
#                              latest code; auto-opens the browser tab unless
#                              one is already connected)
#   3. waits until /api/state answers (up to ~10s)
#   4. clears + starts the live progress feed
#   5. best-effort: registers the calling Claude session id for the panel
#   6. prints everything the agent needs, one KEY=VALUE per line:
#        PANEL_URL= CONFIG_OK= REPO= BASE_BRANCH= PUSH_MODE=
#
# Exit 0 = panel is up. Exit 1 = daemon failed to boot (tail of its log shown).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfg() { python3 "$HERE/skill_config.py" get "$1"; }

PORT="$(cfg panel_port)"
URL="http://127.0.0.1:${PORT}"
mkdir -p "$HERE/runs"

python3 "$HERE/managerd.py" init >/dev/null

# Detached, restart-first serve. Its own open_panel() handles the browser tab
# (and skips opening a duplicate when a tab is already polling).
nohup python3 -u "$HERE/managerd.py" serve > "$HERE/runs/daemon.out" 2>&1 &
NEW_PID=$!
disown 2>/dev/null || true

# Wait until the NEW daemon owns the port AND answers. Checking the API alone
# races: during the restart-first handover the OLD daemon can still answer for
# a moment, and we'd report "up" while the fresh one is still binding.
# IMPORTANT: probe "/" (the HTML), NOT /api/state — state polls stamp the
# "a panel tab is alive" marker, which would make the daemon skip its own
# browser auto-open. Only real tabs may touch /api/state.
up=""
for _ in $(seq 1 60); do
  holder="$(lsof -ti "tcp:${PORT}" 2>/dev/null | head -1)"
  if [ "$holder" = "$NEW_PID" ] && curl -s -m 2 -o /dev/null "$URL/"; then
    up=1; break
  fi
  sleep 0.25
done
if [ -z "$up" ]; then
  if curl -s -m 2 -o /dev/null "$URL/"; then
    # something else (a prior daemon) is serving — usable, but possibly stale code
    echo "PANEL_UP=stale-daemon (the fresh start did not take over — check $HERE/runs/daemon.out)" >&2
  else
    echo "PANEL_UP=no"
    echo "daemon did not come up — tail of $HERE/runs/daemon.out:" >&2
    tail -5 "$HERE/runs/daemon.out" >&2 || true
    exit 1
  fi
fi

# Reset the progress feed ONLY when reconnecting to an empty board. Every
# /manager run restarts the daemon (restart-first), and the daemon's own
# progress state is otherwise untouched by a restart — but blindly resetting
# it here would flash "Setting up your board..." over an existing populated
# board every single time the daemon restarts, even long after ingest finished.
TASK_COUNT="$(curl -s -m 2 "$URL/api/state" 2>/dev/null | python3 -c '
import json,sys
try: print(len([t for t in json.load(sys.stdin).get("tasks",[]) if t.get("status")!="rejected"]))
except Exception: print(0)' 2>/dev/null || echo 0)"
if [ "${TASK_COUNT:-0}" = "0" ]; then
  curl -s -X POST "$URL/api/progress" -H 'Content-Type: application/json' -d '{"clear":true}' >/dev/null
  curl -s -X POST "$URL/api/progress" -H 'Content-Type: application/json' -d '{"label":"Started the control panel"}' >/dev/null
fi

# Best-effort: register the calling Claude session for the panel (resume info).
# The transcript dir is derived from this shell's cwd (Claude Code mangles both
# '/' and '.' to '-'); only a well-formed UUID is registered.
PROJ="$HOME/.claude/projects/$(pwd | sed 's#[/.]#-#g')"
SID="$(ls -t "$PROJ"/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl 2>/dev/null || true)"
if printf '%s' "${SID:-}" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
  curl -s -X POST "$URL/api/manager-session" -H 'Content-Type: application/json' \
    -d '{"session_id":"'"$SID"'"}' >/dev/null || true
fi

CONFIG_OK=no
python3 "$HERE/skill_config.py" check >/dev/null 2>&1 && CONFIG_OK=yes

echo "PANEL_URL=$URL"
echo "CONFIG_OK=$CONFIG_OK"
echo "REPO=$(cfg repo)"
echo "BASE_BRANCH=$(cfg base_branch)"
echo "PUSH_MODE=$(cfg push_mode)"
echo "HAS_WEB_UI=$(cfg has_web_ui)"   # empty = not asked yet; True/False once decided
