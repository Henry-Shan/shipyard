#!/usr/bin/env bash
#
# setup-worktree.sh — provision an isolated dev worktree for a /ship-task run.
#
# The LLM calls this with ONLY two names; everything else comes from the skill
# package's config.json (repo, base_branch, worktree_dir, setup_command,
# dev_command, env_files) so the behavior is identical every run and cannot be
# improvised:
#
#   setup-worktree.sh <branch-name> <worktree-name>
#
# e.g.  setup-worktree.sh feature/csv-export-1716000000 csv-export-1716000000
#
# What it does, in order:
#   1. Refuse to run on a dirty main checkout.
#   2. Update the base branch in the main checkout via `git pull`. On a MERGE
#      CONFLICT (or any pull failure) it aborts the merge and exits 2 — caller
#      must STOP.
#   3. Create <worktree-name> off the freshly-pulled base branch, on a new
#      <branch-name>.
#   4. Symlink each config `env_files` entry from the main checkout (worktrees
#      share the main checkout's gitignored secrets).
#   5. Run the config `setup_command` inside the worktree (deps/build).
#   6. Launch the config `dev_command` HEADLESS (nohup, logs in <wt>/.ship-task/)
#      so it runs independent of any editor window. Skipped under MANAGER_MODE
#      (parallel runs must not each boot a dev stack). Non-fatal on failure.
#
# Exit codes:  0 ok · 2 base-branch pull conflict (STOP) · 3 dirty main checkout (STOP)
#              4 bad args · 5 worktree/branch already exists · 6 setup_command failed
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_HELPER="$SKILL_DIR/../manager/skill_config.py"   # sibling skill; single config
cfg() { python3 "$CONFIG_HELPER" get "$1"; }

MAIN_REPO="$(cfg repo)"
WORKTREE_BASE="$(cfg worktree_dir)"
BASE_BRANCH="$(cfg base_branch)"
SETUP_CMD="$(cfg setup_command)"
DEV_CMD="$(cfg dev_command)"
DEV_PORT="$(cfg dev_port)"
ENV_FILES_JSON="$(cfg env_files)"

if [ -z "$MAIN_REPO" ] || [ ! -d "$MAIN_REPO" ]; then
  echo "STOP(4): target repo not configured or missing ('$MAIN_REPO'). Set \"repo\" in the manager skill's config.json." >&2
  exit 4
fi

BRANCH="${1:-}"
WT_NAME="${2:-}"
if [ -z "$BRANCH" ] || [ -z "$WT_NAME" ]; then
  echo "usage: setup-worktree.sh <branch-name> <worktree-name>" >&2
  exit 4
fi
WT_PATH="$WORKTREE_BASE/$WT_NAME"

echo "==> ship-task worktree setup"
echo "    main repo : $MAIN_REPO"
echo "    base      : $BASE_BRANCH"
echo "    branch    : $BRANCH"
echo "    worktree  : $WT_PATH"

# --- 1. main checkout must be clean -----------------------------------------
if [ -n "$(git -C "$MAIN_REPO" status --porcelain)" ]; then
  echo "STOP(3): main checkout '$MAIN_REPO' has uncommitted changes. Resolve before an autonomous run." >&2
  exit 3
fi

# --- 2. pull the base branch; STOP on conflict -------------------------------
echo "==> updating $BASE_BRANCH"
git -C "$MAIN_REPO" fetch origin "$BASE_BRANCH"
git -C "$MAIN_REPO" checkout "$BASE_BRANCH"
set +e
PULL_OUT="$(git -C "$MAIN_REPO" pull origin "$BASE_BRANCH" 2>&1)"
PULL_RC=$?
set -e
echo "$PULL_OUT"
if [ "$PULL_RC" -ne 0 ] || [ -n "$(git -C "$MAIN_REPO" ls-files -u)" ]; then
  echo "STOP(2): merge conflict / pull failure on $BASE_BRANCH. Aborting merge; not creating a worktree." >&2
  git -C "$MAIN_REPO" merge --abort 2>/dev/null || true
  exit 2
fi

# --- 3. create the worktree + branch off the base branch ---------------------
if [ -e "$WT_PATH" ]; then
  echo "STOP(5): worktree path '$WT_PATH' already exists." >&2
  exit 5
fi
if git -C "$MAIN_REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "STOP(5): branch '$BRANCH' already exists." >&2
  exit 5
fi
mkdir -p "$WORKTREE_BASE"
echo "==> creating worktree"
git -C "$MAIN_REPO" worktree add -b "$BRANCH" "$WT_PATH" "$BASE_BRANCH"

# --- 4. symlink env files (worktree shares the main checkout's secrets) ------
# config `env_files` is a JSON list of repo-relative paths (e.g. [".env",
# "apps/web/.env.local"]). Missing sources are skipped with a note.
echo "==> linking env files"
python3 - "$MAIN_REPO" "$WT_PATH" "$ENV_FILES_JSON" <<'PY'
import json, os, sys
main_repo, wt_path, raw = sys.argv[1:4]
try:
    files = json.loads(raw) if raw else []
except ValueError:
    files = []
if not files:
    print("    (none configured — set \"env_files\" in config.json if your dev setup needs gitignored files)")
for rel in files:
    src = os.path.join(main_repo, rel)
    dst = os.path.join(wt_path, rel)
    if not os.path.exists(src):
        print(f"    SKIP {rel} (not present in the main checkout)")
        continue
    os.makedirs(os.path.dirname(dst) or dst, exist_ok=True)
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(src, dst)
    print(f"    linked {rel}")
PY

# --- 5. project setup inside the worktree ------------------------------------
echo "==> running setup_command (this can take a few minutes): $SETUP_CMD"
set +e
( cd "$WT_PATH" && bash -c "$SETUP_CMD" )
SETUP_RC=$?
set -e
if [ "$SETUP_RC" -ne 0 ]; then
  echo "STOP(6): setup_command failed (exit $SETUP_RC) — fix the project setup or the \"setup_command\" in config.json." >&2
  exit 6
fi

# --- 5b. register the worktree in a multi-root VS Code workspace -------------
# Adds this worktree as a folder in <repo>.code-workspace so it shows up in the
# same window (each as its own Source Control repo). Creates the workspace file
# if it doesn't exist yet. Idempotent. Non-fatal on failure.
WORKSPACE_FILE="$MAIN_REPO.code-workspace"
echo "==> adding worktree to $WORKSPACE_FILE"
add_to_workspace() {
  python3 - "$WORKSPACE_FILE" "$MAIN_REPO" "$WT_PATH" "$WT_NAME" "$BASE_BRANCH" <<'PY'
import json, os, re, sys
ws_file, main_repo, wt_path, wt_name, base_branch = sys.argv[1:6]

data = {"folders": [], "settings": {}}
if os.path.exists(ws_file):
    with open(ws_file) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            pass
data.setdefault("folders", [])

# Ensure the main repo is always present as the first folder.
if not any(os.path.abspath(f.get("path", "")) == os.path.abspath(main_repo) for f in data["folders"]):
    name = f"{os.path.basename(main_repo)} ({base_branch})"
    data["folders"].insert(0, {"name": name, "path": main_repo})

# Add this worktree if not already present. Display name strips a trailing
# -<timestamp> for readability (matches existing entries).
if not any(os.path.abspath(f.get("path", "")) == os.path.abspath(wt_path) for f in data["folders"]):
    display = re.sub(r"-\d{6,}$", "", wt_name)
    data["folders"].append({"name": display, "path": wt_path})

data.setdefault("settings", {})
with open(ws_file, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
}
if add_to_workspace; then
  echo "    registered in workspace."
else
  echo "    NOTE: could not update workspace file (non-fatal)."
fi

# --- 6. launch the dev server HEADLESS (skipped under the manager) -----------
# Headless (nohup) so the server runs independently of any editor/terminal.
# The project's dev_command owns its own port(s); dev_port (config) is where
# the app is expected to serve. NOTE: two worktrees running dev_command at once
# will typically fight over that port — standalone runs are usually one at a
# time, and under /manager this launch is skipped entirely.
if [ -n "${MANAGER_MODE:-}" ]; then
  # Under the /manager orchestrator: do NOT launch this worktree's dev server.
  # Many runs go in parallel; the verify gate is hermetic, so no live server is
  # needed. The human starts a SINGLE test server on demand from the panel.
  echo "==> MANAGER_MODE: skipping dev-server launch (test on demand from the panel)."
else
  LOG_DIR="$WT_PATH/.ship-task"
  mkdir -p "$LOG_DIR"
  echo "==> launching dev server (headless): $DEV_CMD"
  # `</dev/null` + `disown` fully detach the persistent dev server: without the
  # stdin redirect, the server keeps the inherited pipe open and this script
  # (and the /ship-task session that called it) blocks here forever.
  nohup bash -c "cd '$WT_PATH' && exec $DEV_CMD" \
      </dev/null > "$LOG_DIR/dev.log" 2>&1 &
  echo $! > "$LOG_DIR/dev.pid"
  disown 2>/dev/null || true
  echo "    dev : http://localhost:$DEV_PORT   (log: $LOG_DIR/dev.log)"
  echo "    Tail : tail -f \"$LOG_DIR/dev.log\""
fi

echo "==> DONE. Worktree ready at: $WT_PATH"
echo "WORKTREE_PATH=$WT_PATH"
if [ -z "${MANAGER_MODE:-}" ]; then
  echo "WEB_URL=http://localhost:$DEV_PORT"
fi
