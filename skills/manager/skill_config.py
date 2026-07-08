#!/usr/bin/env python3
"""
skill_config.py — the ONE source of project-specific truth for the manager /
ship-task / feature-tester skill package.

Every script (Python and shell) resolves its settings through this module, in
this precedence order (highest wins):

  1. Environment variables (MANAGER_REPO, MANAGER_PORT, …) — the env layer is
     untouched; it overrides the config file exactly as it always overrode the
     old hardcoded defaults.
  2. config.json — the ACTIVE PROJECT's values (see "Multi-project" below), or
     wherever $MANAGER_CONFIG points. It is gitignored; config.example.json is
     the committed template.
  3. Built-in defaults below.

Multi-project: one daemon, many projects, switchable from the panel's home
page — no /manager invocation needed to add or switch between them. Each
project gets its own state under `projects/<slug>/` (config.json + its own
manager.db); `gh_available` and `panel_port` stay in the top-level
`config.json` since they're machine-level facts, not per-project ones.
`projects.json` is the registry (`{"projects":[...], "active": "<slug>"}`).
Switching the active project just rewrites its "active" field and re-execs
the daemon — the same restart-first pattern every other config change uses.

CLI (used by the shell scripts, one value per call):
  python3 skill_config.py get <key>     # prints the resolved value ('' if unset;
                                        # lists/objects print as JSON)
  python3 skill_config.py show          # the full resolved config, as JSON
  python3 skill_config.py check         # validates repo/base_branch; exit != 0 with
                                        # a human-readable reason when misconfigured
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MACHINE_CONFIG_PATH = os.path.join(HERE, "config.json")   # gh_available, panel_port — always here
PROJECTS_FILE = os.path.join(HERE, "projects.json")


def _read_json(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def slugify(path):
    """A filesystem- and URL-safe slug for a project path, e.g. for
    /Users/x/my-app -> "my-app". Deduped by the caller if it collides."""
    base = os.path.basename(os.path.normpath(str(path))) or "project"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower()
    return slug or "project"


def load_projects():
    """{"projects": [{"slug","path","name"}...], "active": "<slug>"|None}"""
    d = _read_json(PROJECTS_FILE, {})
    d.setdefault("projects", [])
    d.setdefault("active", None)
    return d


def save_projects(data):
    _write_json(PROJECTS_FILE, data)


def project_dir(slug):
    return os.path.join(HERE, "projects", slug)


def project_config_path(slug):
    return os.path.join(project_dir(slug), "config.json")


def project_db_path(slug):
    return os.path.join(project_dir(slug), "manager.db")


def active_project():
    """The active project's registry entry ({"slug","path","name"}), or None
    if no project has been added/activated yet (legacy single-config state)."""
    d = load_projects()
    slug = d.get("active")
    if not slug:
        return None
    for p in d["projects"]:
        if p.get("slug") == slug:
            return p
    return None


def project_push_mode(slug):
    """A specific project's push_mode, read directly from its own
    projects/<slug>/config.json — works for ANY registered project, not just
    the currently active one, so the panel's project list can show/toggle
    every project's mode without switching into each one first."""
    cfg = _read_json(project_config_path(slug), {})
    return "local" if str(cfg.get("push_mode", "")).lower() == "local" else "pr"


def set_project_push_mode(slug, mode):
    """Set push_mode for a specific project directly — a pure panel/website
    action. Never involves /manager or any Claude session: this is per-
    project preference, not something the agent decides or is asked about."""
    if not any(p["slug"] == slug for p in load_projects()["projects"]):
        raise ValueError(f"no such project: {slug}")
    cur = _read_json(project_config_path(slug), {})
    cur["push_mode"] = "local" if str(mode).lower() == "local" else "pr"
    _write_json(project_config_path(slug), cur)


def add_project(path, name=None):
    """Register a new project (does NOT activate it — that's a separate,
    explicit step, same as the panel's Add vs. Open). Returns the registry
    entry. Raises ValueError on a bad path."""
    path = os.path.expanduser(str(path).strip())
    if not path:
        raise ValueError("path required")
    d = load_projects()
    for p in d["projects"]:
        if os.path.normpath(p["path"]) == os.path.normpath(path):
            return p   # already registered — idempotent
    existing = {p["slug"] for p in d["projects"]}
    slug = slugify(path)
    if slug in existing:
        i = 2
        while f"{slug}-{i}" in existing:
            i += 1
        slug = f"{slug}-{i}"
    entry = {"slug": slug, "path": path, "name": name or os.path.basename(path.rstrip("/")) or slug}
    d["projects"].append(entry)
    save_projects(d)
    _write_json(project_config_path(slug), {"repo": path})   # else it'd default to "" once active
    return entry


def activate_project(slug):
    """Switch the active project. Raises ValueError if slug isn't registered."""
    d = load_projects()
    if not any(p["slug"] == slug for p in d["projects"]):
        raise ValueError(f"no such project: {slug}")
    d["active"] = slug
    save_projects(d)


def migrate_legacy_single_project():
    """One-time upgrade for an existing single-project install (a config.json
    with "repo" set, from before multi-project support existed): register it
    as a project and make it active, so nothing already configured is lost or
    silently ignored. COPIES config.json's per-project fields and manager.db
    into the new projects/<slug>/ location — never moves or deletes the
    originals, so a legacy config.json / manager.db is always left intact as
    a fallback. No-op (and safe to call every boot) once projects.json exists.
    Returns True iff it just performed a real migration (the caller should
    re-exec so CONFIG_PATH/DB_PATH — both fixed at import time — recompute
    against the new project-scoped layout instead of the stale one)."""
    if os.path.exists(PROJECTS_FILE):
        return False
    legacy = _read_json(MACHINE_CONFIG_PATH, {})
    repo = str(legacy.get("repo") or "").strip()
    if not repo:
        _write_json(PROJECTS_FILE, {"projects": [], "active": None})
        return False
    slug = slugify(repo)
    pdir = project_dir(slug)
    os.makedirs(pdir, exist_ok=True)
    per_project = {k: v for k, v in legacy.items() if k not in ("gh_available", "panel_port")}
    _write_json(project_config_path(slug), per_project)
    legacy_db = os.path.join(HERE, "manager.db")
    if os.path.isfile(legacy_db):
        # NOT shutil.copy2 — manager.db runs in WAL mode, so recent commits can
        # still be sitting in a separate manager.db-wal sidecar file that a raw
        # byte copy of just "manager.db" would silently miss. sqlite3's own
        # backup API reads the full logical database (main file + WAL) and
        # writes a single consolidated, consistent file — the only safe way to
        # copy a live WAL-mode database.
        import sqlite3
        try:
            src = sqlite3.connect(legacy_db)
            dst = sqlite3.connect(project_db_path(slug))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        except sqlite3.Error:
            pass
    save_projects({"projects": [{"slug": slug, "path": repo, "name": os.path.basename(repo.rstrip("/")) or slug}],
                   "active": slug})
    return True


CONFIG_PATH = os.environ.get("MANAGER_CONFIG")
if not CONFIG_PATH:
    _ap = active_project()
    CONFIG_PATH = project_config_path(_ap["slug"]) if _ap else MACHINE_CONFIG_PATH

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
    # Does this project have a browsable web frontend at dev_command/dev_port?
    # null = not yet asked (the intake flow asks once and persists the answer).
    # true  -> the panel offers "Test this branch" / "Open app".
    # false -> no live-app button is ever shown (many projects — APIs, CLIs,
    #          libraries — have nothing to open in a browser); the panel shows
    #          a plain-language before/after of the fix instead.
    "has_web_ui": None,
    # Optional memory MCP server name (e.g. "my-project-memory"). null = ground
    # via git history + repo docs instead.
    "memory_mcp": None,
    # Was `gh` (the GitHub CLI) confirmed working at install time? Decided ONCE,
    # during install.md's prerequisite check — never re-asked by /manager or
    # /ship-task. null = install.md hasn't run its gh check yet (rare: skill
    # copied by hand). true/false lets a fresh "create a new repo for me" flow
    # skip the GitHub-linked-vs-local question entirely and just act on it.
    "gh_available": None,
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
    """The fully-resolved config dict (defaults <- machine config
    (gh_available/panel_port only) <- active project's config.json (or
    $MANAGER_CONFIG) <- env)."""
    cfg = dict(DEFAULTS)
    machine = _read_json(MACHINE_CONFIG_PATH, {})
    cfg.update({k: v for k, v in machine.items()
                if v is not None and k in ("gh_available", "panel_port")})
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
