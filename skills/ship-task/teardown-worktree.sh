#!/usr/bin/env bash
#
# teardown-worktree.sh — tear down a /ship-task dev worktree.
#
#   teardown-worktree.sh <worktree-name> [--delete-branch <branch-name>]
#
# What it does:
#   1. Stop the worktree's headless dev server — by the PID recorded in
#      <wt>/.ship-task/ at setup, with a pkill fallback on the worktree path.
#   2. `git worktree remove --force` (node_modules/untracked files are expected).
#   3. Optionally delete the feature branch (only if --delete-branch is passed
#      AND it is already merged into the base branch; an unmerged branch is kept
#      and reported).
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_HELPER="$SKILL_DIR/../manager/skill_config.py"   # sibling skill; single config
cfg() { python3 "$CONFIG_HELPER" get "$1"; }

MAIN_REPO="$(cfg repo)"
WORKTREE_BASE="$(cfg worktree_dir)"
BASE_BRANCH="$(cfg base_branch)"

WT_NAME="${1:-}"
if [ -z "$WT_NAME" ]; then
  echo "usage: teardown-worktree.sh <worktree-name> [--delete-branch <branch>]" >&2
  exit 4
fi
WT_PATH="$WORKTREE_BASE/$WT_NAME"

DELETE_BRANCH=""
if [ "${2:-}" = "--delete-branch" ]; then
  DELETE_BRANCH="${3:-}"
fi

echo "==> tearing down worktree: $WT_PATH"

# --- 1. stop the worktree's headless dev server ------------------------------
echo "==> stopping dev server for this worktree"
LOG_DIR="$WT_PATH/.ship-task"

# Kill the recorded launcher PID and its direct children (dev servers often
# spawn workers / a compiled child process).
for pf in "$LOG_DIR"/*.pid; do
  [ -f "$pf" ] || continue
  pid="$(cat "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] || continue
  pkill -P "$pid" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
done

# Fallback: anything else whose command line references the worktree path.
pkill -f "$WT_PATH" 2>/dev/null || true

# --- 2. remove the worktree -------------------------------------------------
if [ -e "$WT_PATH" ]; then
  git -C "$MAIN_REPO" worktree remove --force "$WT_PATH"
  echo "    removed worktree."
else
  echo "    worktree path already gone; pruning registrations."
fi
git -C "$MAIN_REPO" worktree prune

# --- 2b. de-register the worktree from the multi-root VS Code workspace ------
# Removes this worktree's folder entry from <repo>.code-workspace (leaving
# the main repo and any other worktrees intact). Idempotent, non-fatal.
WORKSPACE_FILE="$MAIN_REPO.code-workspace"
if [ -f "$WORKSPACE_FILE" ]; then
  echo "==> removing worktree from $WORKSPACE_FILE"
  python3 - "$WORKSPACE_FILE" "$WT_PATH" <<'PY' && echo "    de-registered from workspace." || echo "    NOTE: could not update workspace file (non-fatal)."
import json, os, sys
ws_file, wt_path = sys.argv[1:3]
with open(ws_file) as f:
    data = json.load(f)
before = len(data.get("folders", []))
data["folders"] = [f for f in data.get("folders", [])
                   if os.path.abspath(f.get("path", "")) != os.path.abspath(wt_path)]
if len(data["folders"]) != before:
    with open(ws_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
PY
fi

# --- 3. optionally delete the branch (merged only) --------------------------
if [ -n "$DELETE_BRANCH" ]; then
  if git -C "$MAIN_REPO" branch --merged "$BASE_BRANCH" | grep -qw "$DELETE_BRANCH"; then
    git -C "$MAIN_REPO" branch -d "$DELETE_BRANCH"
    echo "    deleted merged branch '$DELETE_BRANCH'."
  else
    echo "    KEPT branch '$DELETE_BRANCH' (not merged into $BASE_BRANCH)."
  fi
fi

echo "==> teardown complete."
