#!/usr/bin/env python3
"""
skill_config.py — the ONE source of project-specific truth for the manager /
ship-task / feature-tester skill package.

Every script (Python and shell) resolves its settings through this module, in
this precedence order (highest wins):

  1. Environment variables (MANAGER_REPO, MANAGER_PORT, …) — the env layer is
     untouched; it overrides the config file exactly as it always overrode the
     old hardcoded defaults.
  2. config.json — per-project values, written at install time. Lives next to
     this file (<manager skill dir>/config.json) or wherever $MANAGER_CONFIG
     points. It is gitignored; config.example.json is the committed template.
  3. Built-in defaults below.

CLI (used by the shell scripts, one value per call):
  python3 skill_config.py get <key>     # prints the resolved value ('' if unset;
                                        # lists/objects print as JSON)
  python3 skill_config.py show          # the full resolved config, as JSON
  python3 skill_config.py check         # validates repo/base_branch; exit != 0 with
                                        # a human-readable reason when misconfigured
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("MANAGER_CONFIG", os.path.join(HERE, "config.json"))

DEFAULTS = {
    # REQUIRED — absolute path to the target repo the manager works on.
    "repo": "",
    # The branch that is the truth for "already shipped": dedup source, worktree
    # base, and PR target.
    "base_branch": "main",
    # Where ship-task creates per-run git worktrees.
    "worktree_dir": "~/.manager-worktrees",
    # Where finished runs are archived (log + diff + summary per task).
    "archive_dir": "~/.manager-archive",
    # The localhost control panel port.
    "panel_port": 4317,
    # Max concurrent ship-task runs.
    "dispatch_cap": 2,
    # Run once inside each fresh worktree (deps, codegen, build). Point it at a
    # script if your project needs more than one line.
    "setup_command": "npm install && npm run build",
    # Starts the project's dev server(s), run from the worktree root. Used by
    # standalone ship-task runs and by the panel's "Test this branch" button.
    "dev_command": "npm run dev",
    # The localhost port dev_command serves on — the panel health-checks it.
    "dev_port": 3000,
    # The full quality gate ship-task must pass before committing.
    "verify_command": "npm run lint && npm test",
    # Files (paths relative to the repo root) to symlink from the main checkout
    # into each worktree — typically gitignored .env files.
    "env_files": [],
    # Extra project-specific risk keywords appended to risk_floor.py's universal
    # set (any task matching one is minimum Tier 2).
    "risk_keywords_extra": [],
    # Where finished work lands: "pr" = push the branch + open a GitHub PR
    # (needs gh); "local" = stop at a committed local branch — no push, no PR,
    # no gh needed. Toggleable live from the panel.
    "push_mode": "pr",
    # Optional memory MCP server name (e.g. "my-project-memory"). null = ground
    # via git history + repo docs instead.
    "memory_mcp": None,
}

# Env overrides — same names the scripts have always honored.
_ENV = {
    "repo": "MANAGER_REPO",
    "base_branch": "MANAGER_BASE_BRANCH",
    "worktree_dir": "MANAGER_WORKTREES",
    "archive_dir": "MANAGER_ARCHIVE",
    "panel_port": "MANAGER_PORT",
    "dispatch_cap": "MANAGER_CAP",
    "setup_command": "MANAGER_SETUP_CMD",
    "dev_command": "MANAGER_DEV_CMD",
    "dev_port": "MANAGER_DEV_PORT",
    "verify_command": "MANAGER_VERIFY_CMD",
    "push_mode": "MANAGER_PUSH_MODE",
}

_INT_KEYS = ("panel_port", "dispatch_cap", "dev_port")
_PATH_KEYS = ("repo", "worktree_dir", "archive_dir")


def load():
    """The fully-resolved config dict (defaults <- config.json <- env)."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if v is not None})
    except (OSError, ValueError):
        pass  # no config yet — defaults + env still work
    for key, env in _ENV.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    for k in _INT_KEYS:
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in _PATH_KEYS:
        if cfg.get(k):
            cfg[k] = os.path.expanduser(str(cfg[k]))
    # normalize: anything that isn't explicitly "local" means the default PR flow
    cfg["push_mode"] = "local" if str(cfg.get("push_mode", "")).lower() == "local" else "pr"
    return cfg


def get(key, default=None):
    return load().get(key, default)


def require_repo():
    """The configured target repo, validated. Exits with a clear message when
    it's missing — every entry point that needs the repo calls this first so a
    fresh install fails loudly instead of defaulting to a path that isn't there."""
    repo = get("repo")
    if not repo:
        sys.exit(
            "manager: no target repo configured.\n"
            f"  Copy config.example.json to {CONFIG_PATH} and set \"repo\" to your\n"
            "  project's absolute path (or export MANAGER_REPO=/path/to/repo)."
        )
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        sys.exit(f"manager: configured repo is not a git checkout: {repo}")
    return repo


def main():
    args = sys.argv[1:]
    cfg = load()
    if args[:1] == ["show"]:
        print(json.dumps(cfg, indent=2))
        return 0
    if args[:1] == ["check"]:
        require_repo()
        print("config OK:", CONFIG_PATH if os.path.isfile(CONFIG_PATH) else "(defaults + env)")
        return 0
    if len(args) == 2 and args[0] == "get":
        v = cfg.get(args[1])
        if v is None:
            print("")
        elif isinstance(v, (list, dict)):
            print(json.dumps(v))
        else:
            print(v)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
