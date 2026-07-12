#!/usr/bin/env python3
"""
managerd — the /manager orchestrator daemon.

One process does three jobs:
  1. Serves the localhost control panel (ui/index.html) + a small JSON API.
  2. Runs a dispatcher loop: pulls the top-ranked queued task and launches a
     headless `/ship-task` run for it (up to MANAGER_CAP at once).
  3. Hosts the human-in-the-loop relay: /api/ask (a running session relays a
     question) and /api/answer (the session polls for the human's reply). The
     human answers from the web UI via /api/questions/<id>/answer.

Stdlib only — no install step. Run:  python3 managerd.py serve
The agent (SKILL.md) talks to this same module via the CLI subcommands below.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import manager_core as core
import skill_config

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
DISPATCH = os.path.join(HERE, "dispatch.sh")
AUTO_MANAGER = os.path.join(HERE, "auto-manager.sh")
# The ship-task skill is installed as a sibling of this one; resolve its scripts
# relative to THIS file, never via an absolute repo path.
TEARDOWN = os.path.normpath(os.path.join(HERE, "..", "ship-task", "teardown-worktree.sh"))
# the orchestrator (/manager) Claude session id, registered at startup — persists
# across daemon restarts so the panel's "Reconnect" button can resume it.
MANAGER_SESSION_FILE = os.path.join(HERE, ".manager_session")
# single-flight guard for auto-launched /manager ingest runs (see
# launch_auto_manager below) — a flat file, not per-project, since only one
# project is ever "active" for the whole daemon at a time.
AUTO_MANAGER_LOCK = os.path.join(HERE, ".auto_manager.pid")


def read_manager_session():
    try:
        return open(MANAGER_SESSION_FILE).read().strip() or None
    except OSError:
        return None


def _active_project_slug():
    ap = skill_config.active_project()
    return ap["slug"] if ap else None


def read_source_pdf():
    """The active project's active task doc, IF it's a PDF (registered by
    /manager at ingest via /api/source-pdf) — shown on the review board,
    served via /api/image. Per-project docs history lives in docs.json; see
    skill_config.py's docs registry."""
    slug = _active_project_slug()
    if not slug:
        return None
    doc = skill_config.active_doc(slug)
    return doc["path"] if doc and doc["kind"] == "pdf" else None


# The packaged example task doc, shown read-only on the intake card so a new
# user can SEE what the manager eats, before they've pasted their own source.
# realpath: the skill dir is usually a symlink into the warehouse clone;
# examples/ lives at the clone root.
DEMO_DOC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "..", "..", "examples", "demo-qa-findings.md"))
_EXAMPLES = None


def demo_examples():
    """The packaged demo doc, parsed into the SAME block shape the Notion doc
    view renders (so the intake card can show it as a continuous, scrollable
    document rather than a card grid). Cached; {} shape on any failure."""
    global _EXAMPLES
    if _EXAMPLES is not None:
        return _EXAMPLES
    out = {"path": None, "title": "", "findings": [], "blocks": []}
    try:
        import notion_ingest
        raw = open(DEMO_DOC, encoding="utf-8", errors="replace").read()
        m = notion_ingest.parse(raw, "", "demo", tempfile.gettempdir(), do_images=False)
        if m.get("findings"):
            out = {"path": DEMO_DOC, "title": "Example task doc",
                   "findings": m["findings"][:20], "blocks": m.get("blocks", [])}
    except Exception:
        pass
    _EXAMPLES = out
    return out


# Matches a Notion page URL in either its notion.so or app.notion.com form —
# used to gate auto-launch on a deterministic connector check before ever
# spending an LLM invocation on a source that's doomed to fail without it.
NOTION_URL_RE = re.compile(r"https?://(www\.)?(notion\.so|app\.notion\.com)/", re.I)

_NOTION_STATUS_CACHE = {"ts": 0, "connected": None}
NOTION_STATUS_TTL = 30   # seconds — `claude mcp list` does a live health check per call


def notion_connected():
    """Deterministic (no LLM involved) check of whether the Notion MCP
    connector is actually authorized right now, via `claude mcp list`'s own
    health check — the same source of truth Claude Code itself uses. This is
    the ONLY reliable way to know: connector OAuth state lives in Claude
    Code's own account-level config, not something this daemon can inspect
    directly. Cached briefly since `claude mcp list` re-checks every
    configured server's health on each call (not free or instant).
    Returns True/False, or None if the check itself failed (missing `claude`
    CLI, timeout, etc.) — callers should NOT treat None as "connected"."""
    now = time.time()
    if now - _NOTION_STATUS_CACHE["ts"] < NOTION_STATUS_TTL and _NOTION_STATUS_CACHE["connected"] is not None:
        return _NOTION_STATUS_CACHE["connected"]
    try:
        out = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    line = next((l for l in out.splitlines() if "notion" in l.lower()), None)
    if line is None:
        result = None   # Notion connector isn't configured at all here
    else:
        result = "✔" in line   # the exact marker `claude mcp list` prints for a healthy connection
    _NOTION_STATUS_CACHE["ts"] = now
    _NOTION_STATUS_CACHE["connected"] = result
    return result


NOTION_BLOCKED_NOTICE_FILE = os.path.join(HERE, ".notion_blocked_notice")
NOTION_BLOCKED_NOTICE_TTL = 30   # don't spam the same notice on every poll retry


def _notify_notion_not_connected():
    """A launch attempt is retried automatically on every /api/state poll
    while the question stays 'answered' and undelivered (see
    ensure_intake_question) — without throttling, that would append a fresh
    identical progress-feed entry every 1.5-4s forever until the human
    connects Notion. One notice per NOTION_BLOCKED_NOTICE_TTL is enough; the
    retries themselves stay silent and cheap (notion_connected() is cached)."""
    try:
        last = float(open(NOTION_BLOCKED_NOTICE_FILE).read().strip())
    except (OSError, ValueError):
        last = 0
    if time.time() - last < NOTION_BLOCKED_NOTICE_TTL:
        return
    progress_add(
        "⚠ Notion isn't connected",
        "Open a Claude Code session in this project and run /mcp → select "
        "\"claude.ai Notion\" → sign in. This task doc starts automatically "
        "once it's connected — no need to resubmit.",
        "error",
    )
    with open(NOTION_BLOCKED_NOTICE_FILE, "w") as f:
        f.write(str(time.time()))


def notion_map_path():
    """The active project's active task doc, IF it's a Notion/markdown
    ingest (registered by /manager at ingest via /api/source-notion)."""
    slug = _active_project_slug()
    if not slug:
        return None
    doc = skill_config.active_doc(slug)
    return doc["path"] if doc and doc["kind"] == "notion" else None


INTAKE_QUESTION = "Task docs (Notion URL, PDF, .md) — or just type the tasks"


# How long an answered-but-undelivered intake question gets left alone before
# it's considered stale enough to reset. Must comfortably outlast the polling
# gap between answering here and a /manager session's next poll_answer() —
# too short and this races with a completely normal answer that just hasn't
# been picked up yet a few seconds later (this exact bug: /api/state's own
# empty-board auto-trigger re-ran on the very next poll, ~1.5-4s after the
# human answered, and reset a genuinely fresh answer back to pending before
# anything had a chance to consume it).
STALE_ANSWER_GRACE_SECS = 120


def ensure_intake_question():
    """Make sure the standard task-doc intake question is actually
    answerable right now — used both by the auto-trigger (empty board) and
    the manual "+ New task doc" action (populated board). Identical text to
    what /manager's own Step 3 posts, so a real /manager run finds the same
    row instead of a genuinely duplicate one.

    While an auto-launched ingest run is already alive (see
    launch_auto_manager), there is nothing new to ask for — the board should
    show that run's own real progress instead of a redundant question card.

    An 'answered' row with nothing consuming it yet is retried here too (not
    just from the direct answer handler) — covers the case where a SECOND
    doc was answered while a first auto-manager run was still busy, so the
    launch was skipped at the time; once that run finishes, this poll notices
    the still-undelivered answer and launches it. Only past that point does a
    STALE grace period apply: genuinely nothing ever launched for a good
    while, so reset it back to pending rather than leaving it a permanent
    dead end. A 'delivered' row (already consumed) is done for good — start
    a fresh one."""
    if _auto_manager_alive():
        return True
    existing = core.find_question("manager-intake", INTAKE_QUESTION)
    if existing and existing["status"] == "pending":
        return True
    if existing and existing["status"] == "answered":
        if launch_auto_manager(existing["answer"]):
            core.mark_delivered(existing["id"])
            return True
        age = time.time() - (existing.get("answered_at") or 0)
        if age < STALE_ANSWER_GRACE_SECS:
            return True   # still fresh — leave it for whatever's about to consume it
        core.reset_question(existing["id"])
        return True
    core.add_question("manager-intake", INTAKE_QUESTION, [], "")
    return True


def _auto_manager_alive():
    """Is a previously-launched auto-manager ingest run still alive? Checked
    lazily at launch time (no long-lived watcher thread) — robust to the
    daemon itself restarting mid-run (every /manager invocation, including
    this auto-launched one, restarts the daemon via panel-up.sh; the launched
    `claude` child is detached — see launch_auto_manager — and outlives that
    restart, so a stale lock file must still be checked against the real PID,
    not assumed dead just because a new daemon process is answering now)."""
    try:
        pid = int(open(AUTO_MANAGER_LOCK).read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        try: os.remove(AUTO_MANAGER_LOCK)
        except OSError: pass
        return False
    except PermissionError:
        return True   # exists, just owned by someone else — treat as alive


def launch_auto_manager(source):
    """Automatically start a real, unattended /manager ingest run the moment
    the panel's own intake question is answered directly on the website —
    the human doesn't have to separately open a terminal and type /manager
    for that answer to actually get processed. Mirrors dispatch.sh's existing
    headless-launch pattern for /ship-task (same --dangerously-skip-permissions
    posture; see auto-manager.sh's header for the full security note).

    Single-flight: skips launching a second one while one is already running
    (checked via a PID lock file, not a long-lived watcher — see
    _auto_manager_alive). Detached (start_new_session) so it survives the
    daemon restart its own panel-up.sh call will normally trigger almost
    immediately. That fast restart is what USUALLY reaps this child (once
    reparented to init after this process dies) — but if the launch fails
    before ever reaching that restart (bad path, missing `claude`, etc.), the
    child exits while THIS process is still alive, and nothing else would
    ever call wait() on it: it'd sit as a zombie whose PID still answers
    os.kill(pid, 0), permanently wedging _auto_manager_alive() into thinking
    a run is forever in progress. A background reaper thread closes that gap
    unconditionally, independent of whether the daemon restarts or not.

    A Notion URL source gets ONE extra, deterministic pre-check: whether the
    Notion MCP connector is actually authorized (notion_connected()) — never
    just attempted and left to fail deep inside a headless run with a cryptic
    "not authorized" error the human has no easy way to see. If it's not
    connected, this refuses to launch (returns False, same as any other
    launch failure — the caller leaves the question 'answered' so it's
    retried automatically once Notion connects) and posts ONE throttled
    progress notice with the exact fix (open a session, run /mcp, sign in)."""
    if not (source or "").strip():
        return False
    if _auto_manager_alive():
        return False
    if NOTION_URL_RE.search(source):
        if notion_connected() is not True:
            _notify_notion_not_connected()
            return False
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"auto-manager-{int(time.time())}.log")
    try:
        proc = subprocess.Popen(["bash", AUTO_MANAGER, source, log_path],
                                start_new_session=True)
    except Exception as e:
        progress_add("", f"couldn't start the ingest session: {e}", "error")
        return False
    with open(AUTO_MANAGER_LOCK, "w") as f:
        f.write(str(proc.pid))

    def _reap():
        proc.wait()
        try:
            if open(AUTO_MANAGER_LOCK).read().strip() == str(proc.pid):
                os.remove(AUTO_MANAGER_LOCK)
        except OSError:
            pass
    threading.Thread(target=_reap, daemon=True).start()

    progress_add("Starting a Claude session to process your task doc…",
                (source or "")[:100], "active")
    return True


def read_notion_map():
    p = notion_map_path()
    if not p:
        return None
    try:
        m = json.load(open(p))
    except Exception:
        return None
    base = os.path.dirname(p)                      # resolve image src -> absolute, for /api/image
    for b in m.get("blocks", []):
        if b.get("t") == "image" and b.get("src"):
            ap = os.path.join(base, b["src"])
            b["img"] = ap if os.path.isfile(ap) else None
        # Evidence images attached to a finding resolve the same way.
        for ev in (b.get("evidence") or []):
            if ev.get("src"):
                ap = os.path.join(base, ev["src"])
                ev["img"] = ap if os.path.isfile(ap) else None
    return m


# ---------------------------------------------------------------------------
# Setup progress — a live feed of the /manager ingest (fetch → parse → download
# → register → triage → dispatch), shown on the board while the batch loads.
# In-memory (cleared on restart, which is exactly the start of a fresh run).
# ---------------------------------------------------------------------------
_PROGRESS = []
_PROGRESS_LOCK = threading.Lock()


def progress_snapshot():
    with _PROGRESS_LOCK:
        return [dict(s) for s in _PROGRESS]


def progress_add(label, detail="", status="active"):
    """Append a step. A new 'active' step marks the previous active one done, so
    the caller only has to announce each step as it starts."""
    now = time.time()
    with _PROGRESS_LOCK:
        if status in ("active", "done"):
            for s in _PROGRESS:
                if s["status"] == "active":
                    s["status"] = "done"
        if status == "error" and _PROGRESS and _PROGRESS[-1]["status"] in ("active",):
            _PROGRESS[-1]["status"] = "error"
            if detail:
                _PROGRESS[-1]["detail"] = detail
        elif label:
            _PROGRESS.append({"label": label, "detail": detail, "status": status, "ts": now})
        if len(_PROGRESS) > 40:
            del _PROGRESS[:len(_PROGRESS) - 40]


def progress_clear():
    with _PROGRESS_LOCK:
        _PROGRESS.clear()

# All project-specific settings come from skill_config (config.json, with env
# overrides applied inside the loader).
CFG = skill_config.load()
PORT = CFG["panel_port"]
DEFAULT_CAP = CFG["dispatch_cap"]
BASE_BRANCH = CFG["base_branch"]
# How long to hold dispatch after a run dies on a rate/usage limit (seconds).
RATELIMIT_COOLDOWN = int(os.environ.get("MANAGER_RATELIMIT_COOLDOWN", "3600"))
# Concurrency is file-backed so it survives a daemon restart AND a *live* daemon
# picks up changes (panel button or `managerd.py cap N`) on its next loop. The
# old in-memory cap meant a stale daemon ignored both env and restarts — which
# is exactly how a cap of 2 silently became 12 and over-dispatched.
CAP_FILE = None  # set once LOG_DIR is known, just below
REPO = CFG["repo"]
LOG_DIR = os.environ.get("MANAGER_LOGS", os.path.join(HERE, "runs"))
URL = os.environ.get("MANAGER_URL", f"http://127.0.0.1:{PORT}")


def repo_ok():
    """True when a usable target repo is configured. The daemon runs either way
    (setup mode when False): the panel shows a setup screen and the dispatcher
    holds until a repo is saved."""
    return bool(REPO) and os.path.exists(os.path.join(REPO, ".git"))

os.makedirs(LOG_DIR, exist_ok=True)

_dispatch_lock = threading.Lock()
# Touched on every /api/state poll. open_panel() checks it so a daemon restart
# (every /manager run, every /api/config save) never opens a SECOND tab while
# one is already connected — the live tab auto-reloads itself instead.
LAST_POLL_FILE = os.path.join(LOG_DIR, "last_poll")
PAUSE_FILE = os.path.join(LOG_DIR, "PAUSED")        # presence halts auto-dispatch
PAUSE_UNTIL_FILE = os.path.join(LOG_DIR, "PAUSE_UNTIL")  # timed auto-resume (unix ts)
CAP_FILE = os.path.join(LOG_DIR, "CAP")


def get_cap():
    """Read the live cap from disk every time, so a running daemon honors changes
    from the panel or the `cap` CLI without a restart."""
    try:
        return max(1, min(int(open(CAP_FILE).read().strip()), 24))
    except (OSError, ValueError):
        return DEFAULT_CAP


def set_cap(n):
    n = max(1, min(int(n), 24))
    with open(CAP_FILE, "w") as f:
        f.write(str(n))
    return n


def pause_dispatch_until(ts):
    """Hold auto-dispatch until a unix timestamp; the loop auto-resumes after."""
    with open(PAUSE_UNTIL_FILE, "w") as f:
        f.write(str(float(ts)))


def is_paused():
    if os.path.exists(PAUSE_FILE):
        return True
    try:
        until = float(open(PAUSE_UNTIL_FILE).read().strip())
    except (OSError, ValueError):
        return False
    if time.time() < until:
        return True
    try:                       # cooldown elapsed -> clear and resume
        os.remove(PAUSE_UNTIL_FILE)
    except OSError:
        pass
    return False


# ----------------------------------------------------------------- dispatch

def _run_task(task):
    """Worker thread: launch dispatch.sh for one task and settle its status when
    the headless ship-task process exits. While it runs, /api/ask + /api/answer
    carry any human-in-the-loop questions independently."""
    tid = task["id"]
    # Resume an existing session (reusing its worktree) when a prior attempt was
    # interrupted (e.g. rate limit) rather than throwing the partial work away.
    resume_sid = task.get("session_id") if task.get("resume") else None
    sid = resume_sid or core.new_session_id()
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"task-{tid}-{sid}.log")
    core.update_task(tid, status="running", session_id=sid, log_path=log_path, resume=0,
                     failtest_info=None, featuretest=None)  # fresh run: no stale feature-test verdict
    with core.connect() as c:
        verb = "resumed" if resume_sid else "dispatched"
        core.log_event(c, f"task.{verb}", f"session {sid}", tid)

    # No per-run ports: MANAGER_MODE tells the run NOT to launch dev servers, so
    # nothing serves on a per-task port. Testing happens on demand on a single
    # shared backend (:3000) via the panel's "Test this branch".
    env = dict(os.environ, MANAGER_URL=URL, MANAGER_REPO=REPO)
    if resume_sid:
        env["RESUME_SESSION"] = resume_sid
    # Feed the finding's screenshots to the ship-task agent — the description
    # plus the exact image paths to Read, so it SEES the reported behavior
    # before building the fix.
    desc = task["description"] or task["title"]
    ev = [p for p in (task.get("evidence") or []) if p]
    if ev:
        desc += ("\n\nEVIDENCE — screenshots of the reported behavior. Use the Read "
                 "tool on each of these image files to see the bug before fixing:\n"
                 + "\n".join(f"- {p}" for p in ev))
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.run(
                ["bash", DISPATCH, desc, sid, log_path, URL],
                env=env, stdout=logf, stderr=subprocess.STDOUT,
            )
        rc = proc.returncode
    except Exception as e:  # dispatch.sh itself failed to launch
        core.update_task(tid, status="failed", triage_note=f"dispatch error: {e}")
        return

    _settle(tid, log_path, rc)


def start_rework(tid, feedback):
    """Bounce a PR-ready/failed task back to its own ship-task session with the
    human's test feedback. Resumes the same session (same worktree + PR) to fix it."""
    t = core.get_task(tid)
    if not t or not t.get("session_id"):
        return False
    threading.Thread(target=_run_rework, args=(tid, t["session_id"], feedback),
                     daemon=True).start()
    return True


def _run_rework(tid, sid, feedback):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"task-{tid}-{sid}-rework-{int(time.time())}.log")
    core.update_task(tid, status="running", log_path=log_path, failtest_info=None, featuretest=None)
    with core.connect() as c:
        core.log_event(c, "task.rework", feedback[:120], tid)
    prompt = (
        "The human tested your PR and it did NOT work. Their feedback:\n\n"
        f"{feedback}\n\n"
        "Fix it in the SAME worktree you already created, push to the SAME branch/PR, "
        "keep CI green, and stop at PR-ready (do NOT merge). Investigate the real cause "
        "first — reproduce the failure before changing code. When the PR is updated, POST "
        f'{{"session_id":"{sid}","pr_url":"<pr>"}} to {URL}/api/runinfo, then report.'
    )
    env = dict(os.environ, MANAGER_URL=URL, MANAGER_REPO=REPO)
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--resume", sid,
                 "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions"],
                cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT,
            )
        _settle(tid, log_path, proc.returncode)
    except Exception as e:
        core.update_task(tid, status="failed", triage_note=f"rework error: {e}")


def parse_worktree(log_path):
    """Recover the git worktree a run created, from its log. Always returns a clean
    absolute path: <worktree_dir>/<slug>, where slug excludes any trailing
    punctuation (slug chars only). Avoids the old bug where a non-greedy prefix or a
    trailing '.' produced a path that didn't exist."""
    import re
    from collections import Counter
    try:
        text = open(log_path, errors="ignore").read()
    except OSError:
        return None
    # slug = letters/digits/_/- only (no '.'), so a sentence-ending dot is excluded.
    slugs = re.findall(
        re.escape(os.path.basename(WORKTREE_BASE)) + r"/([A-Za-z0-9][A-Za-z0-9_-]*)", text)
    if not slugs:
        return None
    # Pick the MOST-mentioned worktree (the one the run actually worked in), not the
    # first stray reference — a run sometimes names a sibling task's worktree in
    # passing, and grabbing the first match mis-attributed the task.
    return os.path.join(WORKTREE_BASE, Counter(slugs).most_common(1)[0][0])


def _has_result(log_path):
    """True once the run's stream-json log contains a terminal result line."""
    try:
        with open(log_path, errors="ignore") as f:
            return '"type":"result"' in f.read()
    except OSError:
        return False


def _settle(tid, log_path, rc=0):
    pr_url, ok, stopped, limited, transient = _parse_log(log_path)
    now = core.get_task(tid) or {}
    pr_url = pr_url or now.get("pr_url")
    clean = ok and not stopped   # reached a clean terminal result line

    # Interrupted BEFORE a clean finish (usage/session limit, or a network drop) —
    # this is NOT a clean PR-ready, even if a partial PR was already pushed. Don't
    # silently call it done; surface it so the human can resume.
    if not clean and (limited or transient):
        # A transient network blip (wifi) is already over → auto-resume right away.
        if transient and (now.get("attempts") or 0) < 5:
            core.update_task(tid, status="queued", resume=1, stopped_reason="",
                             attempts=(now.get("attempts") or 0) + 1)
            with core.connect() as c:
                core.log_event(c, "task.transient", "network drop — auto-resuming", tid)
            return
        reason = ("session/usage limit reached — resume to finish" if limited
                  else "connection/network dropped — resume to finish")
        core.update_task(tid, status="failed", stopped_reason=reason, pr_url=pr_url)
        if limited:
            pause_dispatch_until(time.time() + RATELIMIT_COOLDOWN)
        with core.connect() as c:
            core.log_event(c, "task.stopped", reason, tid)
        _archive_async(tid)
        return

    if clean:
        core.update_task(tid, status="done", pr_url=pr_url, stopped_reason="")
    else:
        core.update_task(tid, status="failed", pr_url=pr_url,
                         stopped_reason="stopped before finishing — resume to retry")
    with core.connect() as c:
        core.log_event(c, "task.finished", f"ok={ok} pr={'y' if pr_url else 'n'}", tid)
    _archive_async(tid)  # persist log + diff + summary into the archive dir


def _archive_async(tid):
    """Save a finished run's full record without blocking the dispatcher."""
    def _go():
        try:
            import archive
            folder = archive.archive_task(tid)
            with core.connect() as c:
                core.log_event(c, "task.archived",
                               os.path.basename(folder) if folder else "skipped", tid)
        except Exception as e:
            with core.connect() as c:
                core.log_event(c, "task.archive_error", str(e)[:120], tid)
    threading.Thread(target=_go, daemon=True).start()


def reconcile_orphans():
    """On daemon startup, re-attach a watcher to any run that was in flight under
    a previous daemon, so a restart never loses track of a running ship-task."""
    for t in core.list_tasks():
        if t["status"] in ("running", "parked") and t.get("log_path"):
            threading.Thread(target=_watch_orphan,
                             args=(t["id"], t["log_path"]), daemon=True).start()
            with core.connect() as c:
                core.log_event(c, "task.reattached", "watching after daemon restart", t["id"])


def rescan_done_for_interruptions():
    """One-time on startup: re-check 'done' tasks whose run was actually cut off by
    a usage/session limit or network drop before finishing — flip them to a
    resumable 'failed' so they leave PR-ready and show a Resume button."""
    for t in core.list_tasks(status="done"):
        if not t.get("log_path"):
            continue
        pr_url, ok, stopped, limited, transient = _parse_log(t["log_path"])
        if not (ok and not stopped) and (limited or transient):
            reason = ("session/usage limit reached — resume to finish" if limited
                      else "connection/network dropped — resume to finish")
            core.update_task(t["id"], status="failed", stopped_reason=reason)
            with core.connect() as c:
                core.log_event(c, "task.stopped", "re-detected interruption: " + reason, t["id"])


def _watch_orphan(tid, log_path):
    """Tail an orphaned run's log until it shows a terminal result, then settle."""
    waited = 0
    while waited < 6 * 60 * 60:
        if _has_result(log_path):
            _settle(tid, log_path, rc=0)
            return
        time.sleep(5)
        waited += 5


def _parse_log(log_path):
    """Inspect the stream-json run log. Returns (pr_url, ok, stopped, limited):
      ok      — the headless run completed cleanly (final result subtype=success,
                not is_error). This is the structured signal, NOT a substring grep.
      stopped — ship-task hit one of its own STOP gates (its result text uses the
                "Stopped at Step" / 🛑 sentinel). A clean run that merely *mentions*
                the word stop is not 'stopped'.
      limited — the run died on a usage/rate limit (HTTP 429 / "session limit" /
                error:"rate_limit"); the task should be requeued+resumed, not failed.
      transient — the run died on a transient API/network error (socket closed,
                connection reset/timeout — e.g. wifi changed mid-run); also retryable.
      pr_url  — first GitHub PR url seen anywhere in the run."""
    import re
    pr_url, ok, stopped, limited, transient = None, False, False, False, False
    try:
        text = open(log_path, errors="ignore").read()
    except FileNotFoundError:
        return pr_url, ok, stopped, limited, transient
    if ('"api_error_status":429' in text or '"error":"rate_limit"' in text
            or "hit your session limit" in text or "usage limit" in text.lower()):
        limited = True
    if ("socket connection was closed" in text or "ECONNRESET" in text
            or "Connection error" in text or "fetch failed" in text
            or ("API Error" in text and "closed unexpectedly" in text)):
        transient = True
    m = re.search(r"https://github\.com/\S+/pull/\d+", text)
    if m:
        pr_url = m.group(0)
    # Find the final {"type":"result", ...} line.
    result_text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "result":
            ok = (o.get("subtype") == "success") and not o.get("is_error")
            result_text = o.get("result") or ""
    if "Stopped at Step" in result_text or "🛑" in result_text:
        stopped = True
    if "hit your session limit" in result_text or "usage limit" in result_text.lower():
        limited = True
    if "closed unexpectedly" in result_text or "API Error" in result_text:
        transient = True
    return pr_url, ok, stopped, limited, transient


def tail_progress(log_path, lines=160):
    """The latest human-readable action from a run's stream-json log — what the
    ship-task agent is doing *right now* (tool call or assistant text)."""
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        with open(log_path, errors="ignore") as f:
            tail = f.readlines()[-lines:]
    except OSError:
        return None
    # Track Claude's narration (assistant text) and its current action (tool call)
    # SEPARATELY — otherwise the tool_use that ends every turn overwrites the text
    # and the panel only ever shows the latest Bash/Read line. We surface both.
    last_text = None    # what Claude is saying
    last_action = None  # what Claude is doing right now
    for line in tail:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "assistant":
            for blk in (o.get("message", {}) or {}).get("content", []) or []:
                if blk.get("type") == "tool_use":
                    inp = blk.get("input", {}) or {}
                    arg = (inp.get("file_path") or inp.get("command")
                           or inp.get("description") or inp.get("path")
                           or inp.get("pattern") or "")
                    last_action = f"🔧 {blk.get('name','')} · {str(arg)[:80]}".strip(" ·")
                elif blk.get("type") == "text":
                    txt = " ".join((blk.get("text") or "").split())
                    if txt:
                        last_text = f"💬 {txt[:240]}"
        elif o.get("type") == "result":
            last_action = "✓ wrapping up…"
    # Two-line progress: Claude's words on top, current action below.
    parts = [p for p in (last_text, last_action) if p]
    return "\n".join(parts) if parts else None


def list_worktrees():
    """All ship-task dev worktrees (everything under the main repo, minus it)."""
    out = subprocess.run(["git", "-C", REPO, "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout
    wts, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                wts.append(cur)
            cur = {"path": line[9:]}
        elif line.startswith("HEAD "):
            cur["head"] = line[5:][:8]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
    if cur:
        wts.append(cur)
    res = []
    for w in wts:
        if os.path.abspath(w.get("path", "")) == os.path.abspath(REPO):
            continue
        if not os.path.isdir(w.get("path", "")):
            continue  # removed but not yet pruned — never show a dead worktree
        w["slug"] = os.path.basename(w["path"])
        # is this worktree's branch tied to a manager task?
        with core.connect() as c:
            row = c.execute("SELECT id,title FROM tasks WHERE branch=?",
                            (w.get("branch"),)).fetchone()
            if row:
                w["task_id"], w["task_title"] = row["id"], row["title"]
        res.append(w)
    return res


# ---- on-demand single test dev server (only ONE at a time → no port fights) --
TEST_FILE = os.path.join(LOG_DIR, "testserver.json")
# The project's dev server is expected on dev_port (config; default 3000) — most
# stacks have OAuth redirects / CORS / etc. wired to one canonical local origin,
# so the panel boots the branch under test on that same port.
DEV_PORT = CFG["dev_port"]
DEV_COMMAND = CFG["dev_command"]
BACKEND_PORT = CFG.get("backend_port")
BACKEND_COMMAND = CFG.get("backend_command")
WORKTREE_BASE = CFG["worktree_dir"]


def open_pr(branch, worktree):
    """Push a finished task's branch and open (or find) its GitHub PR.

    Used by the panel's "Open PR to merge" button in PR mode when a done task
    has a branch but no PR yet — typically because the network blocked github
    at the moment the run finished. Idempotent: an existing PR is reused, never
    duplicated, so it's also safe to click for several tasks that share one
    branch (a batched fix). Runs inside the worktree so gh resolves the right
    branch + remote. Returns (ok, pr_url, log)."""
    cwd = worktree if worktree and os.path.isdir(worktree) else REPO
    log = []

    def run(args):
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        log.append("$ " + " ".join(args) + "\n" + (r.stdout + r.stderr).strip())
        return r

    if run(["git", "push", "-u", "origin", branch]).returncode != 0:
        return False, None, "\n".join(log)
    # Reuse an existing PR for this branch if there is one; else open a new one.
    view = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"])
    url = view.stdout.strip() if view.returncode == 0 else ""
    if not url:
        create = run(["gh", "pr", "create", "--base", BASE_BRANCH,
                      "--head", branch, "--fill"])
        if create.returncode != 0:
            return False, None, "\n".join(log)
        m = re.search(r"https://github\.com/\S+/pull/\d+", create.stdout)
        url = m.group(0) if m else ""
    return bool(url), (url or None), "\n".join(log)


def _port_up(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _testserver_error():
    """A failure signature from the dev/backend server log tails (first match wins)."""
    lines = []
    for name in ("backend.log", "dev.log"):
        try:
            lines += open(os.path.join(LOG_DIR, "testserver", name),
                          errors="ignore").read().splitlines()
        except OSError:
            pass
    if not lines:
        return ""
    tail = lines[-60:]
    for keys in (("EADDRINUSE", "address already in use", "already running"),
                 ("points out of the filesystem root", "TurbopackInternalError"),
                 # A stale worktree (older commit) whose layout predates the
                 # configured dev/backend command — surface the specific miss,
                 # not the generic npm-log-path line that follows it.
                 ("Missing script",), ("no such file or directory",),
                 ("FATAL", "panic:"), ("command not found", "not found"),
                 ("npm error", "Lifecycle script"),
                 ("Error:", "error:"), ("exit status",)):
        for ln in reversed(tail):
            if any(k in ln for k in keys):
                return ln.strip()[:240]
    return ""


def test_server_info():
    """Live dev-server status: which worktree, the URL, and crucially whether it
    is actually UP — and if not, why (so the panel isn't a silent lie)."""
    try:
        info = json.load(open(TEST_FILE))
    except (OSError, ValueError):
        return None
    info["web_up"] = _port_up(DEV_PORT)
    # When a separate backend is configured, health-check its own port; if not,
    # there's only one process, so backend just mirrors the frontend signal.
    has_backend = bool(info.get("has_backend") and BACKEND_PORT)
    info["backend_up"] = _port_up(BACKEND_PORT) if has_backend else info["web_up"]
    if not info["web_up"] or (has_backend and not info["backend_up"]):
        info["error"] = _testserver_error()
    return info


def _free_port(port):
    """SIGKILL whatever is LISTENing on a TCP port (frees it immediately).

    -sTCP:LISTEN is essential: a bare `lsof -ti tcp:PORT` also matches CLIENT
    connections to that port — e.g. the user's browser tab open on the dev
    server — so without it we'd kill Chrome, not the server."""
    if port:
        subprocess.run(f"lsof -ti tcp:{port} -sTCP:LISTEN | xargs kill -9 2>/dev/null",
                       shell=True)


def _kill_under_cwd(wt):
    """Kill every process whose working directory is inside this worktree.

    This is what catches the compiled `go run` binary: once `go run` itself is
    killed the binary is orphaned (reparented to init) and keeps holding its
    ports, and its argv is a go-build temp path — so neither the process group
    nor `pkill -f <worktree>` reaches it. Its CWD, however, is still the
    worktree. Matched via a single `lsof -d cwd` pass (no slow tree walk)."""
    wt = os.path.abspath(wt)
    try:
        out = subprocess.run(["lsof", "-d", "cwd", "-Fpn"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return
    pid = None
    for line in out.splitlines():
        if line[:1] == "p":
            pid = line[1:]
        elif line[:1] == "n" and pid and line[1:].startswith(wt):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def stop_test_server(wt=None):
    """Tear down the running test server AND any stragglers that could hold its
    ports — orphaned binaries from a prior run or a daemon restart included, so
    the next launch always starts from clean ports."""
    info = test_server_info()
    worktrees = set()
    if wt:
        worktrees.add(os.path.abspath(wt))
    if info:
        if info.get("worktree"):
            worktrees.add(os.path.abspath(info["worktree"]))
        for pid in info.get("pids", []):
            for killer in (os.killpg, os.kill):   # whole group first, then the pid
                try:
                    killer(pid, signal.SIGKILL)
                except OSError:
                    pass
    for w in worktrees:
        subprocess.run(["pkill", "-9", "-f", w], capture_output=True)  # argv match
        _kill_under_cwd(w)                                             # orphaned binaries
    _free_port(DEV_PORT)
    _free_port(BACKEND_PORT)
    try:
        os.remove(TEST_FILE)
    except OSError:
        pass


def _link_env_files(wt):
    """Symlink the project's gitignored env files (config `env_files`, paths
    relative to the repo root — e.g. "apps/server/.env") from the main checkout
    into the worktree. A git worktree never carries gitignored files, so a
    server that reads .env at startup would otherwise crash on missing secrets.
    Read live so a just-saved env_files takes effect without a restart."""
    linked = []
    for rel in (skill_config.get("env_files") or []):
        src = os.path.join(REPO, rel)
        dst = os.path.join(wt, rel)
        if not os.path.exists(src) or os.path.lexists(dst):
            continue  # nothing to link, or the worktree already has its own
        if not os.path.isdir(os.path.dirname(dst)):
            continue  # this worktree/branch doesn't have that app dir — skip
        try:
            os.symlink(os.path.abspath(src), dst)
            linked.append(rel)
        except OSError:
            pass
    return linked


def _link_env_files_all():
    """Backfill env_files into EVERY existing worktree. _link_env_files only
    runs on a web test-server start, but a mobile branch is tested by hand
    (npx expo run:ios) and never goes through it — so a worktree created before
    an env_files entry existed would be missing it. Runs on daemon startup (and
    thus after every config re-exec), idempotent, so any worktree the user cd's
    into already has its gitignored env linked."""
    if not (skill_config.get("env_files") and os.path.isdir(WORKTREE_BASE)):
        return
    for name in os.listdir(WORKTREE_BASE):
        wt = os.path.join(WORKTREE_BASE, name)
        if os.path.isdir(wt):
            _link_env_files(wt)


def _prepare_node_modules(wt):
    """Ensure the worktree has a REAL node_modules that Turbopack accepts.

    Worktrees show up in two broken shapes, both fixed the same way — clone the
    main checkout's node_modules in as a real directory (APFS copy-on-write:
    cheap on disk, ~one-time per worktree):
      - node_modules is a SYMLINK to the main checkout (set up that way to skip
        an install). Next 16 / Turbopack rejects a node_modules symlink that
        escapes the project root ('points out of the filesystem root').
      - node_modules is ABSENT — the worktree was never installed, so even
        'turbo' (node_modules/.bin/turbo) is missing ('turbo: command not found').
    A real, populated node_modules (or a link that stays inside the worktree) is
    left alone."""
    if not os.path.isdir(os.path.join(REPO, "node_modules")):
        return []          # nothing to clone from; not a node project set up here
    fixed = []
    bases = [wt]
    for sub in ("apps", "packages"):
        d = os.path.join(wt, sub)
        if os.path.isdir(d):
            bases += [os.path.join(d, x) for x in os.listdir(d)]
    wt_abs = os.path.abspath(wt)
    for base in bases:
        nm = os.path.join(base, "node_modules")
        if os.path.islink(nm):
            target = os.path.realpath(nm)
            # a link that resolves inside the worktree is fine — leave it
            if os.path.abspath(target).startswith(wt_abs + os.sep):
                continue
        elif os.path.isdir(nm):
            continue          # already a real dir
        elif base != wt:
            continue          # only auto-populate a MISSING root node_modules
                              # (monorepos hoist deps there, not per-workspace)
        rel = os.path.relpath(base, wt)
        src = os.path.join(REPO, rel, "node_modules")
        if not os.path.isdir(src):
            continue
        tmp = nm + ".materializing"
        subprocess.run(["rm", "-rf", tmp], capture_output=True)
        # -c = APFS clonefile (copy-on-write); fall back to a plain deep copy.
        r = subprocess.run(["cp", "-Rc", src, tmp], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["rm", "-rf", tmp], capture_output=True)
            r = subprocess.run(["cp", "-R", src, tmp], capture_output=True, text=True)
        if r.returncode == 0:
            try:
                if os.path.lexists(nm):
                    os.remove(nm)        # drop the symlink…
                os.rename(tmp, nm)       # …and atomically swap in the real dir
                fixed.append(base)
            except OSError:
                subprocess.run(["rm", "-rf", tmp], capture_output=True)
        else:
            subprocess.run(["rm", "-rf", tmp], capture_output=True)
    return fixed


def _link_workspace_dist(wt):
    """Monorepo workspace packages (packages/<name>) are consumed through a
    node_modules symlink, but their build output (dist/) is gitignored — so a
    fresh worktree has the source without a build, and the app fails to resolve
    e.g. '@scope/shared-core' ('Module not found'). Copy each package's built
    dist from the main checkout wherever the worktree lacks it (CoW clone).
    Idempotent; a no-op for projects with no packages/ dir. Note: a preview
    reuses the main checkout's build for any shared package the branch itself
    didn't rebuild — fine for a visual preview, not a substitute for CI."""
    copied = []
    pkgroot = os.path.join(REPO, "packages")
    if not os.path.isdir(pkgroot):
        return copied
    for name in os.listdir(pkgroot):
        src = os.path.join(pkgroot, name, "dist")
        pkgdir = os.path.join(wt, "packages", name)
        dst = os.path.join(pkgdir, "dist")
        if not os.path.isdir(src) or not os.path.isdir(pkgdir):
            continue
        if os.path.isdir(dst) and os.listdir(dst):
            continue  # the worktree already built this package
        if os.path.lexists(dst):
            subprocess.run(["rm", "-rf", dst], capture_output=True)
        r = subprocess.run(["cp", "-Rc", src, dst], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["cp", "-R", src, dst], capture_output=True)
        copied.append(name)
    return copied


def start_test_server(wt):
    if not os.path.isdir(wt):
        return None
    # Tear down the previous server AND this worktree's stragglers (orphaned
    # go/next binaries from an earlier run or a daemon restart), then wait for
    # the ports to actually clear before relaunching — otherwise the fresh
    # backend can't bind and the panel reports "backend not up".
    stop_test_server(wt)
    for _ in range(20):   # up to ~4s for the OS to release the ports
        if not _port_up(DEV_PORT) and not (BACKEND_PORT and _port_up(BACKEND_PORT)):
            break
        time.sleep(0.2)
    logd = os.path.join(LOG_DIR, "testserver")
    os.makedirs(logd, exist_ok=True)
    _link_env_files(wt)          # gitignored .env(s) the servers read at startup
    _prepare_node_modules(wt)    # turn a Turbopack-hostile node_modules symlink into a real dir
    _link_workspace_dist(wt)     # give monorepo workspace packages their gitignored build output

    def _launch(command, logname):
        # No `exec`: the command may be a compound line (e.g.
        # "cd apps/server && go run main.go"), and `exec cd …` fails because cd
        # is a shell builtin, not a program — which silently kills the launch.
        # start_new_session + killpg (in stop_test_server) still reaps children.
        return subprocess.Popen(
            ["bash", "-c", f"cd '{wt}' && {command}"],
            stdin=subprocess.DEVNULL,
            stdout=open(os.path.join(logd, logname), "w"),
            stderr=subprocess.STDOUT, start_new_session=True).pid

    pids = []
    # Backend first — the frontend usually needs its API already listening.
    if BACKEND_COMMAND:
        pids.append(_launch(BACKEND_COMMAND, "backend.log"))
    pids.append(_launch(DEV_COMMAND, "dev.log"))
    url = f"http://localhost:{DEV_PORT}"
    json.dump({"worktree": wt, "slug": os.path.basename(wt), "pids": pids,
               "web_url": url, "has_backend": bool(BACKEND_COMMAND)},
              open(TEST_FILE, "w"))
    return url


PDF_CACHE = os.path.join(tempfile.gettempdir(), "mgr-pdfcache")


def _pdftoppm():
    return (shutil.which("pdftoppm")
            or next((p for p in ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")
                     if os.path.isfile(p)), None))


def pdf_page_count(pdf):
    """Page count of a PDF via poppler's pdfinfo (0 on any failure)."""
    if not pdf or not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf):
        return 0
    exe = (shutil.which("pdfinfo")
           or next((p for p in ("/opt/homebrew/bin/pdfinfo", "/usr/local/bin/pdfinfo")
                    if os.path.isfile(p)), None))
    if not exe:
        return 0
    try:
        out = subprocess.run([exe, pdf], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"^Pages:\s*(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def _pdftotext():
    return (shutil.which("pdftotext")
            or next((p for p in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext")
                     if os.path.isfile(p)), None))

_HEAD_RE = re.compile(r'^\s*(?:Feature|Bug|Test|Problem|Issue|Notification|Step)?\s*\d+\.\d+(?:\.\d+)?\b', re.I)
_ID_RE = re.compile(r'(\d+\.\d+(?:\.\d+)?)')


def pdf_map(pdf):
    """Extract the doc's text layout (poppler pdftotext -bbox-layout) and segment it
    into SECTIONS — a leading 'Setup & example accounts' context block, then one per
    numbered finding heading — each with a precise per-page bounding box (normalized
    0..1). Powers the review board's specific section boxes. Cached by pdf+mtime."""
    if not pdf or not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf):
        return {"pages": [], "sections": []}
    exe = _pdftotext()
    if not exe:
        return {"pages": [], "sections": []}
    try:
        key = hashlib.sha1(("map|%s|%d" % (pdf, int(os.path.getmtime(pdf)))).encode()).hexdigest()
    except OSError:
        return {"pages": [], "sections": []}
    os.makedirs(PDF_CACHE, exist_ok=True)
    cache = os.path.join(PDF_CACHE, key + ".map.json")
    if os.path.isfile(cache):
        try:
            return json.load(open(cache))
        except Exception:
            pass
    try:
        import xml.etree.ElementTree as ET
        xml = subprocess.run([exe, "-bbox-layout", pdf, "-"], capture_output=True,
                             text=True, timeout=60).stdout
        root = ET.fromstring(xml)
    except Exception:
        return {"pages": [], "sections": []}
    loc = lambda e: e.tag.split("}")[-1]
    pages, blocks = [], []
    for pi, page in enumerate([e for e in root.iter() if loc(e) == "page"], 1):
        W = float(page.get("width") or 612); H = float(page.get("height") or 792)
        pages.append({"n": pi, "w": W, "h": H})
        for blk in [e for e in page.iter() if loc(e) == "block"]:
            lines = [" ".join((w.text or "") for w in ln.iter() if loc(w) == "word")
                     for ln in blk.iter() if loc(ln) == "line"]
            txt = " ".join(lines).strip()
            if not txt:
                continue
            blocks.append({"p": pi, "W": W, "H": H, "x0": float(blk.get("xMin")),
                           "y0": float(blk.get("yMin")), "x1": float(blk.get("xMax")),
                           "y1": float(blk.get("yMax")), "t": txt})

    def boxes_of(mem):
        by = {}
        for b in mem:
            d = by.get(b["p"])
            if not d:
                by[b["p"]] = d = [b["x0"], b["y0"], b["x1"], b["y1"], b["W"], b["H"]]
            d[0] = min(d[0], b["x0"]); d[1] = min(d[1], b["y0"])
            d[2] = max(d[2], b["x1"]); d[3] = max(d[3], b["y1"])
        return [{"page": p, "x": round(d[0]/d[4], 4), "y": round(d[1]/d[5], 4),
                 "w": round((d[2]-d[0])/d[4], 4), "h": round((d[3]-d[1])/d[5], 4)}
                for p, d in sorted(by.items())]

    hidx = [i for i, b in enumerate(blocks) if _HEAD_RE.match(b["t"])]
    sections = []
    if hidx and hidx[0] > 0:
        mem = blocks[0:hidx[0]]
        sections.append({"id": "", "title": "Setup & example accounts", "kind": "context",
                         "page": mem[0]["p"], "boxes": boxes_of(mem)})
    for k, h in enumerate(hidx):
        end = hidx[k+1] if k+1 < len(hidx) else len(blocks)
        mem = blocks[h:end]
        m = _ID_RE.search(blocks[h]["t"])
        sections.append({"id": m.group(1) if m else "", "title": blocks[h]["t"][:90],
                         "kind": "finding", "page": blocks[h]["p"], "boxes": boxes_of(mem)})
    out = {"pages": pages, "sections": sections}
    try:
        json.dump(out, open(cache, "w"))
    except OSError:
        pass
    return out


def render_pdf_page(pdf, page, dpi=130):
    """Rasterize ONE page of a PDF to a cached PNG (poppler's pdftoppm), so the review
    board can show the actual page portion instead of a broken <img src=...pdf>.
    Returns the PNG path, or None (bad input / no rasterizer / render failure)."""
    if not pdf or not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf):
        return None
    try:
        page = int(page)
    except (TypeError, ValueError):
        return None
    if page < 1 or page > 9999:
        return None
    exe = _pdftoppm()
    if not exe:
        return None
    try:
        key = hashlib.sha1(("%s|%d|%d|%d" % (pdf, int(os.path.getmtime(pdf)), page, dpi)).encode()).hexdigest()
    except OSError:
        return None
    os.makedirs(PDF_CACHE, exist_ok=True)
    out = os.path.join(PDF_CACHE, key + ".png")
    if os.path.isfile(out):
        return out
    try:
        r = subprocess.run([exe, "-png", "-singlefile", "-f", str(page), "-l", str(page),
                            "-r", str(dpi), pdf, os.path.join(PDF_CACHE, key)],
                           capture_output=True, timeout=30)
    except Exception:
        return None
    return out if (r.returncode == 0 and os.path.isfile(out)) else None


def _diffstat(diff, out):
    """Fill out['files']/['added']/['deleted'] from unified-diff text, keyed on each
    `diff --git a/X b/Y` header so deletes (+++ /dev/null), renames (no hunks) and
    binaries (no +/-) each get their own row."""
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[1] if " b/" in line else line[len("diff --git "):]
            cur = {"path": path, "added": 0, "deleted": 0}
            out["files"].append(cur)
        elif cur is None:
            continue
        elif line.startswith("rename from ") or line.startswith("rename to "):
            cur["rename"] = True
        elif line.startswith("Binary files") and line.rstrip().endswith("differ"):
            cur["binary"] = True
        elif line.startswith("+") and not line.startswith("+++"):
            cur["added"] += 1; out["added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            cur["deleted"] += 1; out["deleted"] += 1


def live_changes(wt):
    """Real-time diff of an in-flight run's worktree vs the base branch: committed +
    unstaged tracked changes, plus untracked new files synthesized as new-file blocks.
    Read-only (never touches the worktree index), so it's safe during a live run."""
    def git(*a):
        try:
            return subprocess.run(["git", "-C", wt, *a], capture_output=True,
                                  text=True, timeout=20).stdout
        except Exception:
            return ""
    base = (git("merge-base", "HEAD", f"origin/{BASE_BRANCH}").strip()
            or f"origin/{BASE_BRANCH}")
    diff = git("diff", base)
    for f in [x for x in git("ls-files", "--others", "--exclude-standard").split("\n") if x][:60]:
        p = os.path.join(wt, f)
        try:
            if os.path.getsize(p) > 200000:
                continue
            body = open(p, errors="replace").read()
        except OSError:
            continue
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()   # drop the phantom trailing element from a file ending in \n
        diff += ("\ndiff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n"
                 "@@ -0,0 +1,%d @@\n" % (f, f, f, len(lines))) + "".join("+" + l + "\n" for l in lines)
    out = {"archived": False, "live": True, "summary": "", "files": [], "added": 0, "deleted": 0,
           "diff": diff[:20000]}
    _diffstat(diff, out)
    return out


def task_changes(tid):
    """The edits a task's run produced, for the review board. If the run has settled
    it reads the archived diff; if it's still in flight (worktree present) it computes
    a LIVE diff from the worktree, so the board shows changes in real time."""
    import glob
    try:
        import archive
        root = archive.ARCHIVE_ROOT
    except Exception:
        root = CFG["archive_dir"]
    folders = sorted(glob.glob(os.path.join(root, "task-%02d-*" % tid)))
    if folders:
        folder = folders[0]
        out = {"archived": True, "live": False, "summary": "", "files": [], "added": 0, "deleted": 0, "diff": ""}
        sp = os.path.join(folder, "summary.md")
        if os.path.isfile(sp):
            out["summary"] = open(sp, errors="replace").read()[:4000]
        dp = os.path.join(folder, "changes.diff")
        if os.path.isfile(dp):
            diff = open(dp, errors="replace").read()
            out["diff"] = diff[:20000]
            _diffstat(diff, out)
        return out
    # not archived yet — show a LIVE diff from the run's worktree, if it exists
    t = core.get_task(tid)
    wt = (t or {}).get("worktree")
    if wt and os.path.isdir(wt):
        return live_changes(wt)
    return {"archived": False}


def remove_worktree(slug, branch=None):
    """One button: ship-task's teardown (stops servers, removes worktree, prunes,
    de-registers from the .code-workspace, deletes the branch ONLY if merged)."""
    args = ["bash", TEARDOWN, slug]
    if branch:
        args += ["--delete-branch", branch]
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def dispatcher_loop():
    """Background loop: keep up to CAP ship-task runs in flight."""
    while True:
        try:
            with _dispatch_lock:
                if repo_ok() and not is_paused() and core.active_count() < get_cap():
                    nxt = core.next_queued()
                    if nxt:
                        threading.Thread(target=_run_task, args=(nxt,), daemon=True).start()
                        time.sleep(2)  # let status flip to running before re-checking
                        continue
        except Exception as e:
            print("dispatcher error:", e)
        time.sleep(3)


# ----------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # The panel is a long-lived tab; without this the browser caches the
        # HTML/JS and keeps running stale code across UI updates (a cleanup
        # modal that never completes, buttons that don't match the daemon).
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        # "/" is the projects home page (the JS itself renders that mode when
        # location.pathname is "/" — see renderHome() in review.html); a
        # specific project's board lives at "/<slug>", handled just below.
        if p in ("/", "/index.html", "/review", "/review.html"):
            return self._serve_file("review.html", "text/html")
        if re.match(r"^/[a-zA-Z0-9_-]+$", p):
            slug = p[1:]
            projects = skill_config.load_projects()
            match = next((x for x in projects["projects"] if x["slug"] == slug), None)
            if not match:
                return self._send(404, b"no such project")
            if projects.get("active") != slug:
                # Switching projects needs the same full re-exec every other
                # config change does (CONFIG_PATH/DB_PATH are fixed at import
                # time) — respond with a tiny self-refreshing page instead of
                # making the browser wait on a request that outlives the
                # process it was sent to.
                skill_config.activate_project(slug)
                threading.Timer(0.6, _reexec).start()
                html = (b'<!doctype html><meta charset="utf-8">'
                        b'<meta http-equiv="refresh" content="2;url=/' + slug.encode() + b'">'
                        b'<body style="font:14px system-ui;padding:40px">'
                        b'Switching to <b>' + slug.encode() + b'</b> \xe2\x80\x94 reloading in a moment\xe2\x80\xa6</body>')
                return self._send(200, html, "text/html")
            return self._serve_file("review.html", "text/html")
        if p == "/api/task-count":
            # A scripted health check's non-tab equivalent of /api/state's task
            # count (panel-up.sh's stale-banner guard) — deliberately does NOT
            # touch LAST_POLL_FILE, unlike /api/state below. That marker means
            # "a real browser tab is polling"; a script hitting it here would
            # convince open_panel() a tab is already alive on every single
            # restart and it would stop ever auto-opening the browser.
            n = len([t for t in core.snapshot().get("tasks", []) if t.get("status") != "rejected"])
            return self._send(200, {"count": n})
        if p == "/api/projects":
            d = skill_config.load_projects()
            for proj in d["projects"]:
                proj["push_mode"] = skill_config.project_push_mode(proj["slug"])
                proj.update(skill_config.project_web(proj["slug"]))
            return self._send(200, d)
        if p == "/api/docs":
            # The active project's task-doc history — every source ever
            # ingested for it, so the panel can list them and switch which
            # one's findings are displayed (not just the single latest one).
            slug = _active_project_slug()
            return self._send(200, skill_config.load_docs(slug) if slug else {"docs": [], "active": None})
        if p == "/api/notion-status":
            # A deterministic (no LLM) check the panel calls BEFORE letting
            # the user submit a Notion URL — connector OAuth state lives in
            # Claude Code's own account config, so `claude mcp list`'s own
            # health check is the only real source of truth for it.
            connected = notion_connected()
            return self._send(200, {"connected": connected})
        if p == "/api/state":
            try:    # a live panel tab is polling — remember, so restarts don't open dupes
                with open(LAST_POLL_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            st = core.snapshot()
            st["paused"] = is_paused()
            st["cap"] = get_cap()
            st["repo"] = REPO                # so the panel's copy-commands cd to the right repo
            st["repo_ok"] = repo_ok()        # False -> the panel renders its setup screen
            st["config_path"] = skill_config.CONFIG_PATH
            # read live (not from the import-time CFG): the panel toggle writes it
            # without a restart, and each dispatch reads it fresh anyway.
            st["push_mode"] = skill_config.get("push_mode")
            st["has_web_ui"] = skill_config.get("has_web_ui")   # null=not yet asked, true/false=decided
            # Repo has an Expo/React-Native app at apps/mobile → the panel offers
            # a "📱 iOS" copy-command (build + run on the simulator), since that
            # can't be previewed by the web "Test this branch" flow.
            st["has_mobile"] = os.path.isdir(os.path.join(REPO, "apps", "mobile"))
            st["gh_available"] = skill_config.get("gh_available")  # decided once at install time
            st["base_branch"] = BASE_BRANCH
            st["skill_dir"] = HERE           # for panel-shown shell snippets (e.g. PDF copy)
            st["manual_pause"] = os.path.exists(PAUSE_FILE)
            st["manager_session"] = read_manager_session()  # for the Reconnect button
            st["notion"] = notion_map_path() is not None     # review board uses Notion source if set
            st["test_server"] = test_server_info()           # so the review board can show "Test this branch"
            steps = progress_snapshot()                       # live /manager ingest feed for the board
            st["progress"] = {"steps": steps,
                              "active": any(s["status"] == "active" for s in steps),
                              "done": bool(steps) and all(s["status"] != "active" for s in steps)}
            st["source_pdf"] = read_source_pdf()             # for the review board (PDF fallback)
            if not st["source_pdf"]:                          # fall back to a PDF in task evidence
                for t in st["tasks"]:
                    e = next((x for x in (t.get("evidence") or [])
                              if isinstance(x, str) and x.lower().endswith(".pdf") and os.path.isfile(x)), None)
                    if e:
                        st["source_pdf"] = e
                        break
            try:    # mtime of the served UI; the open tab auto-reloads when it
                    # changes so a shipped panel update never leaves stale JS.
                st["ui_version"] = int(os.path.getmtime(
                    os.path.join(UI_DIR, "review.html")))
            except OSError:
                st["ui_version"] = 0
            try:    # seconds left on a rate-limit cooldown, if any
                st["cooldown_left"] = max(0, int(
                    float(open(PAUSE_UNTIL_FILE).read()) - time.time()))
            except (OSError, ValueError):
                st["cooldown_left"] = 0
            for t in st["tasks"]:
                if t["status"] in ("running", "parked"):
                    pr = tail_progress(t.get("log_path"))
                    if pr:
                        t["progress"] = pr
                # Backfill / repair the worktree path so PR-ready cards can "Test
                # this branch". Re-parse when it's empty (an early parse before the
                # log had it left it stuck at "") OR when the stored path no longer
                # exists on disk (a cleaned-up worktree → clear the dead Test button).
                # Capture the real branch while the worktree is live so a later
                # Delete deletes the exact branch.
                wt_stored = t.get("worktree") or ""
                if t.get("log_path") and (not wt_stored or not os.path.isdir(wt_stored)):
                    wt = parse_worktree(t["log_path"]) or ""
                    if wt and not os.path.isdir(wt):
                        wt = ""                         # worktree removed — nothing to test
                    if wt != wt_stored:
                        branch = ""
                        if wt:
                            branch = next((w.get("branch") for w in list_worktrees()
                                           if os.path.abspath(w["path"]) == os.path.abspath(wt)), "") or ""
                        core.update_task(t["id"], worktree=wt, branch=(branch or None))
                        t["worktree"] = wt
                        if branch:
                            t["branch"] = branch
            # An empty board with a configured repo and no live /manager
            # session running has nothing for the human to act on otherwise —
            # proactively post the SAME intake question /manager itself would,
            # so "enter your task docs" is always available, not just while a
            # session is open. (A populated board can still ask for another
            # doc — that's the manual /api/intake/new trigger, not this one.)
            if st["repo_ok"] and not any(t["status"] != "rejected" for t in st["tasks"]):
                if ensure_intake_question():
                    st["pending_questions"] = core.list_questions(only_pending=True)
                    st["questions"] = core.list_questions()
            return self._send(200, st)
        if p == "/api/image":
            return self._serve_image((qs.get("path") or [""])[0])
        if p == "/api/pdf-page":
            png = render_pdf_page((qs.get("path") or [""])[0], (qs.get("page") or ["1"])[0])
            if png:
                return self._serve_image(png)
            return self._send(404, b"not found")
        if p == "/api/pdf-info":
            return self._send(200, {"pages": pdf_page_count((qs.get("path") or [""])[0])})
        if p == "/api/pdf-map":
            return self._send(200, pdf_map((qs.get("path") or [""])[0]))
        if p == "/api/notion-map":
            return self._send(200, read_notion_map() or {})
        if p == "/api/examples":
            return self._send(200, demo_examples())
        if p == "/api/answer":
            # the dispatched ship-task session polls here for the human's reply
            sid = (qs.get("session_id") or [""])[0]
            ans = core.poll_answer(sid) if sid else None
            return self._send(200, {"answer": ans})
        if p == "/api/worktrees":
            return self._send(200, {"worktrees": list_worktrees(),
                                    "test_server": test_server_info()})
        if p.startswith("/api/tasks/") and p.endswith("/changes"):
            try:
                tid = int(p.split("/")[3])
            except ValueError:
                return self._send(400, {"error": "bad id"})
            return self._send(200, task_changes(tid))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()
        if p == "/api/ask":
            # a running session relays a question into the inbox
            qid = core.add_question(
                (body.get("session_id") or ""), body.get("question", ""),
                body.get("options"), body.get("context", ""), body.get("task_id"),
            )
            return self._send(200, {"question_id": qid})
        if p == "/api/runinfo":
            # a running session reports its worktree URL, its PR once open, and
            # optionally real "after" screenshot(s) it actually captured (e.g.
            # via /browse) — a list of absolute image paths.
            core.set_runinfo(body.get("session_id", ""),
                             body.get("web_url"), body.get("server_url"),
                             body.get("pr_url"), body.get("test_plan"),
                             body.get("after_evidence"))
            return self._send(200, {"ok": True})
        if p == "/api/failtest":
            # back-compat: a still-failing exhausted feature test (verdict=fail).
            core.add_failtest(body.get("session_id", ""), body.get("test_name", ""),
                              body.get("error_output", ""), body.get("attempts", 0))
            return self._send(200, {"ok": True})
        if p == "/api/featuretest":
            # the feature-tester's verdict for a run (pass|skipped|blocked|fail) —
            # DISPLAY it on the task card with its turns. Read-only; not a question.
            core.set_featuretest(body.get("session_id", ""), body.get("verdict", "fail"),
                                 body.get("test", ""), body.get("cycles", 0),
                                 body.get("output", ""), body.get("turns"))
            return self._send(200, {"ok": True})
        if p == "/api/manager-session":
            # the /manager orchestrator registers its own Claude session id at
            # startup so the panel's "Reconnect" button can `claude --resume` it.
            # Require a UUID — the value is pasted into a shell, so never store junk.
            sid = (body.get("session_id") or "").strip()
            if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", sid):
                try: open(MANAGER_SESSION_FILE, "w").write(sid)
                except OSError: pass
                return self._send(200, {"ok": True})
            return self._send(400, {"error": "session_id must be a uuid"})
        if p == "/api/source-pdf":
            # /manager registers the batch's source QA PDF (absolute path) as
            # a task doc for the ACTIVE project — kept in that project's docs
            # history (skill_config.py), not just a single overwritten pointer.
            slug = _active_project_slug()
            if not slug:
                return self._send(400, {"error": "no active project"})
            path = (body.get("path") or "").strip()
            if path.lower().endswith(".pdf") and os.path.isfile(path):
                doc = skill_config.add_doc(slug, "pdf", path, body.get("title"))
                return self._send(200, {"ok": True, "doc": doc})
            return self._send(400, {"error": "path must be an existing .pdf"})
        if p == "/api/source-notion":
            # register the Notion/markdown doc-map (map.json from
            # notion_ingest.py) as a task doc for the ACTIVE project; pass
            # {"path": "<…/map.json>"} or {} to clear the active doc (history
            # is kept either way — see skill_config.py's docs registry).
            slug = _active_project_slug()
            if not slug:
                return self._send(400, {"error": "no active project"})
            path = (body.get("path") or "").strip()
            if not path:
                skill_config.activate_doc(slug, None)
                return self._send(200, {"ok": True, "cleared": True})
            if path.endswith(".json") and os.path.isfile(path):
                doc = skill_config.add_doc(slug, "notion", path, body.get("title"))
                return self._send(200, {"ok": True, "doc": doc})
            return self._send(400, {"error": "path must be an existing map.json"})
        if p == "/api/tasks":
            tid = core.add_task(
                body.get("title", "untitled"), body.get("description", ""),
                body.get("source", ""), body.get("evidence"), body.get("tier"),
                body.get("triage_note", ""), body.get("open_question", ""),
                body.get("status", "draft"),
            )
            return self._send(200, {"task_id": tid})
        if p.startswith("/api/tasks/") and p.endswith("/status"):
            tid = int(p.split("/")[3])
            core.set_status(tid, body.get("status"))
            return self._send(200, {"ok": True})
        if p.startswith("/api/tasks/") and p.endswith("/update"):
            tid = int(p.split("/")[3])
            core.update_task(tid, **body)
            return self._send(200, {"ok": True})
        if p.startswith("/api/questions/") and p.endswith("/answer"):
            qid = int(p.split("/")[3])
            q = core.get_question(qid)
            answer = body.get("answer", "")
            core.answer_question(qid, answer)
            # The intake question is special: answering it here IS "start the
            # batch" — nothing else will otherwise ever consume this answer.
            # Every other question (a real ship-task run's relayed question,
            # or a pre-dispatch ask_preference) already has its own live
            # process polling /api/answer for it; only manager-intake needs
            # the daemon itself to kick off a consumer.
            if q and q["session_id"] == "manager-intake":
                # The auto-launched session receives the answer directly as
                # /manager's invocation argument (auto-manager.sh), never by
                # polling /api/answer for it — so poll_answer will never mark
                # this row 'delivered' on its own. Do it here once the launch
                # actually starts, or the stale-answer grace period would
                # later reset this same, already-handled row back to pending.
                if launch_auto_manager(answer):
                    core.mark_delivered(qid)
            return self._send(200, {"ok": True})
        if p == "/api/pause":
            open(PAUSE_FILE, "w").close()
            return self._send(200, {"paused": True})
        if p == "/api/resume":
            # Manual Resume is an explicit override: clear BOTH the manual pause
            # and any rate-limit cooldown, so the button always dispatches now.
            for f in (PAUSE_FILE, PAUSE_UNTIL_FILE):
                if os.path.exists(f):
                    os.remove(f)
            return self._send(200, {"paused": False})
        if p == "/api/worktrees/remove":
            rc, out = remove_worktree(body.get("slug", ""), body.get("branch") or None)
            return self._send(200, {"ok": rc == 0, "output": out})
        if p == "/api/testserver/start":
            wt = body.get("path") or os.path.join(WORKTREE_BASE, body.get("slug", ""))
            url = start_test_server(wt)
            return self._send(200, {"ok": bool(url), "web_url": url})
        if p == "/api/testserver/stop":
            stop_test_server()
            return self._send(200, {"ok": True})
        if p == "/api/reset":
            # Clear All must actually clear the board — not just the tasks
            # table. The active project's task doc is separate state that
            # renderNotion() keeps rendering from regardless of the task
            # queue, so leaving it behind after a reset looks exactly like
            # "nothing got cleared": the full QA doc + all its findings stay
            # on screen. Wipes the WHOLE docs history too, not just the
            # active pointer — a fresh start really means fresh.
            n = core.reset_queue()
            progress_clear()
            slug = _active_project_slug()
            if slug:
                skill_config.save_docs(slug, {"docs": [], "active": None})
            return self._send(200, {"ok": True, "cleared": n})
        if p == "/api/progress":
            # Live /manager ingest feed. {clear:true} resets; else append a step
            # {label, detail?, status?}. A new step auto-completes the previous one.
            if body.get("clear"):
                progress_clear()
            else:
                progress_add(body.get("label", ""), body.get("detail", ""), body.get("status", "active"))
            return self._send(200, {"ok": True})
        if p == "/api/cap":
            set_cap(body.get("cap", get_cap()))
            return self._send(200, {"cap": get_cap()})
        if p == "/api/config":
            # Setup-mode escape hatch: the panel (or the agent) saves config
            # values here; the daemon then re-execs itself so module-level
            # settings reload. Only known keys are accepted; a repo path must
            # be an existing git checkout before anything is written.
            allowed = {"repo", "base_branch", "setup_command", "dev_command",
                       "dev_port", "verify_command", "panel_port", "dispatch_cap",
                       "env_files", "risk_keywords_extra", "memory_mcp",
                       "worktree_dir", "archive_dir", "push_mode", "has_web_ui",
                       "gh_available"}
            updates = {k: v for k, v in body.items() if k in allowed}
            if not updates:
                return self._send(400, {"ok": False, "error": "no recognized config keys"})
            cfgp = skill_config.CONFIG_PATH
            if "repo" in updates:
                updates["repo"] = os.path.expanduser(str(updates["repo"]).strip())
                if not os.path.exists(os.path.join(updates["repo"], ".git")):
                    ok, err, push_mode = init_local_repo(updates["repo"])
                    if not ok:
                        return self._send(400, {"ok": False, "error": err})
                    updates.setdefault("push_mode", push_mode)
                # A fresh install (no project registered yet, e.g. /manager's
                # own first-run flow) must still land in the project registry —
                # otherwise it'd silently write the flat legacy config.json and
                # never show up under 📁 Projects. skill_config.CONFIG_PATH was
                # fixed at import time, before this registration existed, so
                # compute the real target path fresh rather than trusting it.
                if skill_config.active_project() is None:
                    entry = skill_config.add_project(updates["repo"])
                    skill_config.activate_project(entry["slug"])
                    cfgp = skill_config.project_config_path(entry["slug"])
            try:
                cur = json.load(open(cfgp))
                if not isinstance(cur, dict):
                    cur = {}
            except (OSError, ValueError):
                cur = {}
            cur.update(updates)
            with open(cfgp, "w") as f:
                json.dump(cur, f, indent=2)
            # push_mode is read live (state endpoint + each dispatch), so a
            # mode toggle needs no restart — mid-batch toggles must not bounce
            # the daemon. Everything else is import-time config: re-exec.
            needs_restart = bool(set(updates) - {"push_mode", "has_web_ui", "gh_available"})
            if needs_restart:
                threading.Timer(0.6, _reexec).start()  # respond first, then restart
            return self._send(200, {"ok": True, "restarting": needs_restart})
        if p == "/api/browse-folder":
            # "+ Add project" wants a real native folder picker, not an in-page
            # dropdown — a browser can't hand page JS an absolute filesystem
            # path from its own file input for security reasons, but this
            # daemon runs locally on the same machine as the browser, so it can
            # just pop the OS's own native "choose folder" dialog and read the
            # path back directly. Blocks this request's thread until the user
            # picks or cancels (ThreadingHTTPServer keeps every other route free).
            path, err = None, None
            try:
                if sys.platform == "darwin":
                    r = subprocess.run(
                        ["osascript", "-e",
                         'POSIX path of (choose folder with prompt "Choose a project folder" '
                         'default location (path to home folder))'],
                        capture_output=True, text=True)
                    if r.returncode == 0:
                        path = r.stdout.strip()
                    elif "User canceled" not in (r.stderr or ""):
                        err = r.stderr.strip() or "folder picker failed"
                elif sys.platform.startswith("linux") and shutil.which("zenity"):
                    r = subprocess.run(["zenity", "--file-selection", "--directory",
                                        "--title=Choose a project folder",
                                        "--filename=" + os.path.expanduser("~") + "/"],
                                       capture_output=True, text=True)
                    if r.returncode == 0:
                        path = r.stdout.strip()
                    elif r.returncode != 1:  # 1 = user canceled in zenity's convention
                        err = r.stderr.strip() or "folder picker failed"
                else:
                    err = "no native folder picker available on this platform — type the path instead"
            except FileNotFoundError:
                err = "no native folder picker available on this platform — type the path instead"
            if err:
                return self._send(400, {"ok": False, "error": err})
            if not path:
                return self._send(200, {"ok": False, "canceled": True})
            return self._send(200, {"ok": True, "path": path})
        if p == "/api/projects":
            # Register a new project — purely a panel/daemon action, no /manager
            # (or any Claude session) needs to be involved. Same repo-creation
            # logic as the single-project setup screen: an existing git repo is
            # used as-is; a plain folder gets one created (identity-checked,
            # gh_available-gated), never guessing anything.
            path = os.path.expanduser(str(body.get("path") or "").strip())
            if not path:
                return self._send(400, {"ok": False, "error": "path required"})
            if not os.path.isabs(path):
                return self._send(400, {"ok": False, "error": f"path must be absolute, got: {path}"})
            if not os.path.exists(os.path.join(path, ".git")):
                ok, err, _push_mode = init_local_repo(path)
                if not ok:
                    return self._send(400, {"ok": False, "error": err})
            try:
                entry = skill_config.add_project(path, body.get("name"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, "project": entry})
        if p == "/api/projects/activate":
            slug = str(body.get("slug") or "").strip()
            try:
                skill_config.activate_project(slug)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            threading.Timer(0.6, _reexec).start()  # respond first, then restart into the new project
            return self._send(200, {"ok": True})
        if p == "/api/projects/push-mode":
            # A pure panel/website preference, settable per project straight
            # from the projects list — no need to open a project first, and
            # never something /manager decides or asks about.
            slug = str(body.get("slug") or "").strip()
            try:
                skill_config.set_project_push_mode(slug, body.get("push_mode"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True})
        if p == "/api/projects/web-config":
            # Set a project's web-preview settings (does it have a browsable
            # frontend, and the dev command + port to boot it) straight from the
            # projects list — for ANY project, no need to open it first.
            slug = str(body.get("slug") or "").strip()
            updates = {}
            if "has_web_ui" in body:
                updates["has_web_ui"] = bool(body["has_web_ui"])
            for cmd_key in ("dev_command", "backend_command"):
                if cmd_key in body:
                    v = body[cmd_key]
                    updates[cmd_key] = (str(v).strip() or None) if v is not None else None
            for port_key in ("dev_port", "backend_port"):
                if port_key in body:
                    v = body[port_key]
                    try:
                        updates[port_key] = int(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        updates[port_key] = None
            try:
                skill_config.set_project_config(slug, updates)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            # has_web_ui is read live; the dev/backend command+port are
            # import-time globals, so only re-exec when they change for the
            # ACTIVE project.
            restart = (slug == _active_project_slug() and bool(
                {"dev_command", "dev_port", "backend_command", "backend_port"}
                & set(updates)))
            if restart:
                threading.Timer(0.6, _reexec).start()
            return self._send(200, {"ok": True, "restarting": restart})
        if p == "/api/projects/remove":
            # Stops tracking a project — never touches the actual repo, git
            # history, branches, or PRs, exactly like Clear All never touches
            # worktrees. If the removed project was the active one, re-exec
            # so the daemon picks up whichever project (if any) is next.
            slug = str(body.get("slug") or "").strip()
            try:
                active_changed = skill_config.remove_project(slug)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if active_changed:
                threading.Timer(0.6, _reexec).start()
            return self._send(200, {"ok": True, "active_changed": active_changed})
        if p == "/api/docs/activate":
            # Switch which of the active project's ingested docs is displayed
            # — a pure panel action, live (no restart: the board just reads
            # docs.json fresh on the next /api/state poll).
            slug = _active_project_slug()
            if not slug:
                return self._send(400, {"ok": False, "error": "no active project"})
            doc_id = body.get("id")
            try:
                skill_config.activate_doc(slug, int(doc_id) if doc_id is not None else None)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True})
        if p == "/api/intake/new":
            # "+ New task doc" — ensures the intake question is available even
            # when the board already has tasks (the automatic /api/state
            # trigger only fires on an EMPTY board); same question either way,
            # so a /manager run later finds one card either way.
            ensure_intake_question()
            return self._send(200, {"ok": True})
        if p.startswith("/api/tasks/") and p.endswith("/rework"):
            tid = int(p.split("/")[3])
            ok = start_rework(tid, body.get("feedback", ""))
            return self._send(200, {"ok": ok})
        if p.startswith("/api/tasks/") and p.endswith("/rerun"):
            # Run a task again FROM SCRATCH: drop every trace of the prior
            # attempt (session, worktree, branch, PR, logs, test artifacts) and
            # re-queue it. The dispatcher then gives it a fresh worktree off the
            # current base branch and links it correctly — the fix for a task
            # left with a stale/mismatched worktree. resume=0 guarantees it does
            # NOT reuse the old session. The old branch/PR on GitHub is left
            # untouched; the old local worktree is left in place (may be shared).
            tid = int(p.split("/")[3])
            if not core.get_task(tid):
                return self._send(404, {"ok": False, "error": "no such task"})
            # If the stale worktree happens to be the one under test, stop it.
            t = core.get_task(tid)
            ts = test_server_info()
            if ts and t.get("worktree") and ts.get("worktree") and \
                    os.path.abspath(ts["worktree"]) == os.path.abspath(t["worktree"]):
                stop_test_server()
            core.update_task(
                tid, status="queued", resume=0, attempts=0,
                session_id=None, worktree=None, branch=None, pr_url=None,
                log_path=None, web_port=None, web_url=None, server_url=None,
                test_plan=None, stopped_reason=None, failtest_info=None,
                featuretest=None)
            return self._send(200, {"ok": True})
        if p.startswith("/api/tasks/") and p.endswith("/resume"):
            # Continue a run that was cut off mid-task (session limit / wifi).
            tid = int(p.split("/")[3])
            ok = start_rework(tid,
                "You were interrupted mid-task (a usage/session limit or a network "
                "drop), not because the work was done. Resume exactly where you left "
                "off in the SAME worktree: finish the remaining steps, push to the "
                "same branch/PR, keep CI green, and stop at PR-ready.")
            return self._send(200, {"ok": ok})
        if p.startswith("/api/tasks/") and p.endswith("/cleanup"):
            # After the human merges the PR: tear down the worktree + the (now
            # merged) branch, then remove the task from the panel.
            tid = int(p.split("/")[3])
            t = core.get_task(tid)
            out = "no worktree on record"
            if t and t.get("worktree"):
                slug = os.path.basename(t["worktree"])
                branch = t.get("branch") or next((w.get("branch") for w in list_worktrees()
                               if os.path.abspath(w["path"]) == os.path.abspath(t["worktree"])),
                              "feature/" + slug)
                _, out = remove_worktree(slug, branch)
            # if the test server was this worktree, stop it first
            ts = test_server_info()
            if ts and t and ts.get("worktree") and \
                    os.path.abspath(ts["worktree"]) == os.path.abspath(t.get("worktree") or ""):
                stop_test_server()
            core.delete_task(tid)
            return self._send(200, {"ok": True, "output": out})
        if p.startswith("/api/tasks/") and p.endswith("/open-pr"):
            # PR mode: push this task's branch and open (or look up) its PR, then
            # link EVERY task on that same branch to the PR (a batched fix shares
            # one branch). The work already lives in the worktree; this just gets
            # it onto GitHub as a PR when the original run couldn't.
            tid = int(p.split("/")[3])
            t = core.get_task(tid)
            if not t:
                return self._send(404, {"ok": False, "error": "no such task"})
            branch = t.get("branch")
            if not branch:
                return self._send(400, {"ok": False,
                    "error": "this task has no branch — nothing to open a PR from"})
            ok, pr_url, out = open_pr(branch, t.get("worktree"))
            if ok and pr_url:
                def _is_url(u):
                    return isinstance(u, str) and u.strip().lower().startswith(
                        ("http://", "https://"))
                # Fill EVERY task on this branch that lacks a real PR link —
                # including ones whose pr_url holds a stale "still blocked…"
                # status message rather than an actual URL.
                for other in core.list_tasks():
                    if (other.get("branch") or "") == branch and not _is_url(other.get("pr_url")):
                        core.update_task(other["id"], pr_url=pr_url)
            return self._send(200 if ok else 502,
                              {"ok": ok, "pr_url": pr_url, "output": out})
        if p.startswith("/api/tasks/") and p.endswith("/delete"):
            # "Just delete this" — abandon a task from ANY state: stop its servers,
            # remove the worktree, FORCE-delete the branch (even if unmerged, since
            # the work is being discarded), then drop the task from the panel.
            tid = int(p.split("/")[3])
            t = core.get_task(tid)
            out = "no worktree on record"
            if t and t.get("worktree"):
                slug = os.path.basename(t["worktree"])
                branch = t.get("branch") or next((w.get("branch") for w in list_worktrees()
                               if os.path.abspath(w["path"]) == os.path.abspath(t["worktree"])),
                              "feature/" + slug)
                ts = test_server_info()
                if ts and ts.get("worktree") and \
                        os.path.abspath(ts["worktree"]) == os.path.abspath(t["worktree"]):
                    stop_test_server()
                _, out = remove_worktree(slug, branch)
                # remove_worktree only deletes a MERGED branch; this is an explicit
                # discard, so force-delete the branch if it's still there.
                if branch:
                    fb = subprocess.run(["git", "-C", REPO, "branch", "-D", branch],
                                        capture_output=True, text=True)
                    if fb.returncode == 0:
                        out += f"\nforce-deleted branch {branch}"
            core.delete_task(tid)
            return self._send(200, {"ok": True, "output": out})
        return self._send(404, {"error": "not found"})

    def _serve_file(self, name, ctype):
        path = os.path.join(UI_DIR, name)
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, b"ui not found")

    def _serve_image(self, path):
        import mimetypes
        if not path or not os.path.isfile(path):
            return self._send(404, b"not found")
        if os.path.splitext(path)[1].lower() not in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf"):
            return self._send(403, b"unsupported file type")
        with open(path, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(path)[0] or "image/png")


def open_panel():
    """Best-effort: open the control panel in Chrome (fallback: default browser).
    Disable with MANAGER_NO_OPEN=1. Skipped when a panel tab is ALREADY polling
    (last /api/state within 30s) — restart-first restarts the daemon constantly,
    and without this guard every restart opened one more duplicate tab."""
    if os.environ.get("MANAGER_NO_OPEN"):
        return
    try:
        # A REAL tab polls /api/state every 1-8s, so a fresh marker means a tab
        # is alive right now. (Scripted health checks must probe "/" instead —
        # a probe that touched /api/state would suppress this open forever.)
        if time.time() - os.path.getmtime(LAST_POLL_FILE) < 12:
            print(f"panel already open (tab polling) — not opening another: {URL}")
            return
    except OSError:
        pass  # never polled -> first run, do open
    # "/" is the multi-project home page now — /manager still opens straight
    # into the active project's own board (today's UX), falling back to home
    # only when nothing is configured/active yet.
    ap = skill_config.active_project()
    target = f"{URL}/{ap['slug']}" if ap else URL
    try:
        if sys.platform == "darwin":
            if subprocess.run(["open", "-a", "Google Chrome", target],
                              capture_output=True).returncode != 0:
                subprocess.run(["open", target], capture_output=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", target], capture_output=True)
    except Exception:
        pass


def init_local_repo(repo_path):
    """The panel's direct equivalent of SKILL.md's "create a new repo for me"
    chat sub-flow — used by /api/config when the saved repo path doesn't exist
    yet or isn't a git checkout, so pasting a fresh path into the panel works
    end to end instead of dead-ending on "not a git repo".

    Same safety rules as the chat flow: NEVER guess a git identity (stop and
    hand back the exact command if one isn't configured) and NEVER decide
    GitHub linkage here — that was already decided once at install time
    (config `gh_available`); an HTTP handler has no interactive channel to ask
    it fresh, so an unset gh_available just means "stay local".

    Returns (ok, error_or_None, push_mode_or_None)."""
    try:
        os.makedirs(repo_path, exist_ok=True)
    except OSError as e:
        return False, f"couldn't create {repo_path}: {e}", None
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
    email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
    if not name or not email:
        return False, (
            "git isn't configured with an identity yet — run this yourself, "
            'then try again: git config --global user.name "Your Name" && '
            'git config --global user.email "you@example.com"'), None
    r = subprocess.run(["git", "init", "-b", "main", repo_path], capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"git init failed: {r.stderr.strip()}", None
    if not any(f != ".git" for f in os.listdir(repo_path)):
        with open(os.path.join(repo_path, "README.md"), "w") as f:
            f.write(f"# {os.path.basename(repo_path.rstrip('/')) or 'project'}\n")
    subprocess.run(["git", "-C", repo_path, "add", "-A"], capture_output=True)
    r = subprocess.run(["git", "-C", repo_path, "commit", "-m", "Initial commit"],
                       capture_output=True, text=True)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        return False, f"initial commit failed: {r.stderr.strip()}", None
    if skill_config.get("gh_available") is True:
        r = subprocess.run(["gh", "repo", "create", "--private", "--source", ".", "--push"],
                           capture_output=True, text=True, cwd=repo_path)
        if r.returncode == 0:
            return True, None, "pr"
        return True, None, "local"  # repo is still usable locally even if gh push failed
    return True, None, "local"


def free_port(port):
    """Kill whatever currently holds `port` so a fresh `serve` always REPLACES a
    stale daemon (and thereby picks up the latest code). The manager owns its port,
    so the holder is a prior managerd instance. SIGTERM, wait ~3s, then SIGKILL."""
    import signal
    me = os.getpid()
    def holders():
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True).stdout.split()
        except Exception:
            return []
        return [int(x) for x in out if x.isdigit() and int(x) != me]
    pids = holders()
    if not pids:
        return
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        except Exception: pass
    for _ in range(30):                      # up to ~3s for a graceful exit
        time.sleep(0.1)
        if not holders():
            break
    else:
        for pid in holders():
            try: os.kill(pid, signal.SIGKILL)
            except Exception: pass
        time.sleep(0.3)
    print(f"restart: freed port {port} (replaced stale daemon {pids})")


def _reexec():
    """Restart this daemon in place (same PID) so freshly-saved config loads.
    Python sockets are close-on-exec, so the port frees itself."""
    os.execv(sys.executable, [sys.executable, os.path.join(HERE, "managerd.py"), "serve"])


def serve():
    if skill_config.migrate_legacy_single_project():
        # CONFIG_PATH/DB_PATH are fixed at import time — this process's copies
        # are already stale (computed before the migration ran). Re-exec into
        # a fresh process instead of patching globals in place; the new one
        # imports against the just-written projects.json and picks up the
        # project-scoped config.json/manager.db cleanly.
        _reexec()
        return
    free_port(PORT)   # restart-first: replace any running daemon so the latest code loads
    core.init_db()
    if not repo_ok():
        # Friendly setup mode instead of refusing to start: the panel shows a
        # setup screen (paste a repo path -> saved via /api/config -> restart),
        # and nothing can dispatch until the repo is valid.
        print(f"manager: no target repo configured yet ({REPO or 'unset'}).")
        print(f"  -> panel is in SETUP MODE at {URL} — enter your repo path there,")
        print(f"     or edit {skill_config.CONFIG_PATH} (see config.example.json).")
    # An explicit MANAGER_CAP env on `serve` is authoritative and is written to
    # the cap file so it sticks; otherwise seed the file with the default once.
    if os.environ.get("MANAGER_CAP"):
        set_cap(os.environ["MANAGER_CAP"])
    elif not os.path.exists(CAP_FILE):
        set_cap(DEFAULT_CAP)
    reconcile_orphans()       # re-attach watchers to runs from a prior daemon
    rescan_done_for_interruptions()  # demote interrupted runs out of PR-ready
    _link_env_files_all()     # backfill env_files into every existing worktree
    threading.Thread(target=dispatcher_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"manager up:  {URL}   (cap={get_cap()}, repo={REPO})")
    print(f"open the control panel at {URL}")
    threading.Timer(0.6, open_panel).start()  # after the socket is listening
    srv.serve_forever()


# ----------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="manager orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("init")
    sub.add_parser("reset")
    sp = sub.add_parser("add")
    sp.add_argument("--title", required=True)
    sp.add_argument("--description", default="")
    sp.add_argument("--source", default="")
    sp.add_argument("--tier", type=int)
    sp.add_argument("--triage-note", default="")
    sp.add_argument("--open-question", default="")
    sp.add_argument("--evidence", default="[]", help="json list of image paths")
    sp.add_argument("--status", default="draft")
    sub.add_parser("list")
    sp2 = sub.add_parser("status")
    sp2.add_argument("task_id", type=int)
    sp2.add_argument("status")
    sp3 = sub.add_parser("cap", help="set max concurrent runs on the LIVE daemon")
    sp3.add_argument("n", type=int)
    sub.add_parser("resume", help="clear any pause and let dispatch continue now")
    args = ap.parse_args()

    if args.cmd == "serve":
        return serve()
    if args.cmd == "cap":
        # File-backed, so a running daemon picks it up on its next loop (~3s).
        print(f"cap = {set_cap(args.n)}")
        return
    if args.cmd == "resume":
        for f in (PAUSE_FILE, PAUSE_UNTIL_FILE):
            if os.path.exists(f):
                os.remove(f)
        print("dispatch resumed")
        return
    core.init_db()
    if args.cmd == "init":
        print("ok")
    elif args.cmd == "reset":
        n = core.reset_queue()
        # Best-effort: also clear the LIVE daemon's in-memory progress feed (a
        # separate process; this CLI call can't touch its memory directly). If
        # a fresh run's queue is cleared but the daemon's stale progress banner
        # from a prior run isn't, refresh gets stuck showing "Setting up your
        # board..." forever even though the queue is empty.
        try:
            import urllib.request
            urllib.request.urlopen(
                urllib.request.Request(f"{URL}/api/progress", data=b'{"clear":true}',
                                       headers={"Content-Type": "application/json"}, method="POST"),
                timeout=2)
        except Exception:
            pass
        print(f"cleared {n} task(s)")
    elif args.cmd == "add":
        tid = core.add_task(
            args.title, args.description, args.source, json.loads(args.evidence),
            args.tier, args.triage_note, args.open_question, args.status,
        )
        print(tid)
    elif args.cmd == "list":
        for t in core.list_tasks():
            print(f"#{t['id']:>3} [{t['status']:>7}] T{t['tier']} {t['title']}")
    elif args.cmd == "status":
        core.set_status(args.task_id, args.status)
        print("ok")


if __name__ == "__main__":
    main()
