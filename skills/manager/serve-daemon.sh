#!/usr/bin/env bash
#
# serve-daemon.sh — start the manager daemon so it KEEPS RUNNING after you close
# the terminal and DOESN'T STOP when the Mac goes idle / the lid is closed.
#
# Two independent things have to be true for "runs while the laptop is closed":
#   1. The process must survive the terminal/SSH session ending  -> nohup + setsid-style detach.
#   2. The OS must not sleep while the lid is shut               -> caffeinate (this script),
#      AND the machine must stay on power. On battery, macOS still sleeps on
#      lid-close; either keep it on the charger, or run:
#         sudo pmset -b disablesleep 1      # allow clamshell (lid-closed) running on battery
#         sudo pmset -b disablesleep 0      # revert
#
# Usage:
#   ./serve-daemon.sh            # start detached, caffeinated (cap from CAP file / env / default 2)
#   MANAGER_CAP=2 ./serve-daemon.sh
#   ./serve-daemon.sh stop       # stop the daemon (running ship-task children keep going)
#   ./serve-daemon.sh status
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${MANAGER_LOGS:-$HERE/runs}"
PIDFILE="$LOG_DIR/daemon.pid"
OUTLOG="$LOG_DIR/daemon.out"
PORT="$(python3 "$HERE/skill_config.py" get panel_port)"   # env MANAGER_PORT overrides via the loader
mkdir -p "$LOG_DIR"

# probe "/" not /api/state: state polls mark "a panel tab is alive", which
# would suppress the daemon's own browser auto-open (see managerd.open_panel)
is_up() { curl -s -m 3 -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; }

case "${1:-start}" in
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && echo "stopped daemon (pid $(cat "$PIDFILE"))"
    else
      pkill -f "managerd.py serve" && echo "stopped daemon" || echo "no daemon running"
    fi
    rm -f "$PIDFILE"
    exit 0 ;;
  status)
    if is_up; then echo "daemon UP on :$PORT"; else echo "daemon DOWN"; fi
    exit 0 ;;
esac

if is_up; then
  echo "daemon already running on :$PORT — leaving it. (use './serve-daemon.sh stop' first to restart)"
  exit 0
fi

# caffeinate -dimsu (macOS): prevent display(-d), idle(-i), disk(-m) and
# system(-s) sleep and assert(-u) user activity, for as long as the wrapped
# daemon lives. On Linux there is no caffeinate; run the daemon plain.
# nohup + & + disown detaches it from this terminal so closing the shell is safe.
KEEPAWAKE=""
command -v caffeinate >/dev/null 2>&1 && KEEPAWAKE="caffeinate -dimsu"
# shellcheck disable=SC2086
nohup $KEEPAWAKE python3 -u "$HERE/managerd.py" serve >"$OUTLOG" 2>&1 &
echo $! > "$PIDFILE"
disown || true

for _ in $(seq 1 20); do
  if is_up; then
    echo "manager daemon up (detached${KEEPAWAKE:+, caffeinated}) on http://127.0.0.1:${PORT}"
    echo "pid $(cat "$PIDFILE") · log: $OUTLOG"
    [ -n "$KEEPAWAKE" ] && echo "NOTE: on battery, lid-closed still sleeps unless 'sudo pmset -b disablesleep 1' or on charger."
    exit 0
  fi
  sleep 0.5
done
echo "daemon did not come up; see $OUTLOG" >&2
tail -5 "$OUTLOG" >&2 || true
exit 1
