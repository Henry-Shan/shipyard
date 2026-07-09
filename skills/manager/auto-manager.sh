#!/usr/bin/env bash
#
# auto-manager.sh — launch ONE headless /manager ingest run, triggered
# automatically by the daemon when the panel's own intake question is
# answered directly on the website (no human needs to open a terminal and
# type /manager for that answer to actually get processed).
#
# ⚠ SECURITY: runs `claude` with --dangerously-skip-permissions by default
# (override via MANAGER_PERMS) — the SAME posture dispatch.sh already uses
# for every headless /ship-task run this package launches. Read dispatch.sh's
# header for the full explanation; it applies here unchanged. This session
# can ingest the source, ground/dedup/triage it, and dispatch further headless
# ship-task runs on its own — exactly what an interactive /manager run does,
# just started by the daemon instead of a human typing the command.
#
# Args: <source>  <log-path>
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfg() { python3 "$HERE/skill_config.py" get "$1"; }

SOURCE="${1:?source (task doc / typed tasks) required}"
LOG_PATH="${2:?log path required}"
REPO="$(cfg repo)"

PERMS="${MANAGER_PERMS:---dangerously-skip-permissions}"
MODEL_FLAG=""
[ -n "${MANAGER_MODEL:-}" ] && MODEL_FLAG="--model ${MANAGER_MODEL}"

cd "$REPO" || { echo "repo not found: $REPO" >&2; exit 4; }

# shellcheck disable=SC2086
exec claude -p "/manager '${SOURCE}'" \
  --output-format stream-json --verbose \
  $PERMS $MODEL_FLAG \
  >"$LOG_PATH" 2>&1
