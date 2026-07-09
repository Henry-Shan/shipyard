#!/usr/bin/env python3
"""
relay_client.py — the LOCAL bridge between this machine's manager daemon and
the hosted relay hub (see ../../PLAN.md and ../../cloud/relay_server.py).

It is a standalone process, deliberately NOT wired into managerd.py: it only
talks to the local daemon over the existing 127.0.0.1 HTTP API, and dials OUT
to the relay. That keeps the battle-tested daemon untouched and makes this the
single place where the two security guarantees are enforced:

  1. THE METADATA BOUNDARY. Only display metadata is pushed up (task
     titles/status/PR-url/test-plan, questions+answers, progress, counts,
     project display facts). Code, diffs, logs, worktree paths, session ids,
     full repo paths, and Claude credentials NEVER cross — because this filter
     never puts them on the wire.

  2. THE ARM GATE. A relayed command that would actually run `claude` is
     REFUSED unless the user has armed this machine LOCALLY
     (`remote_exec_armed: true` in config.json, default false). Arming cannot
     be set over the relay — otherwise a compromised cloud could forge it and
     the gate would be theatre. The machine is the trust boundary, not the
     cloud.

Run:  MANAGER_RELAY_URL=https://relay.example \\
      MANAGER_MACHINE_TOKEN=sk_machine_... \\
      MANAGER_MACHINE_NAME="my-laptop" \\
      python3 relay_client.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.parse

import skill_config

RELAY_URL = os.environ.get("MANAGER_RELAY_URL", "").rstrip("/")
TOKEN = os.environ.get("MANAGER_MACHINE_TOKEN", "").strip()
MACHINE_NAME = os.environ.get("MANAGER_MACHINE_NAME") or os.uname().nodename
STATE_PUSH_EVERY = 3          # seconds between metadata pushes up
POLL_EVERY = 1.5             # seconds between command polls (SHORT-poll — works
                            # against a serverless relay like Vercel that can't
                            # hold a connection open; also fine for the local one)


def _local_url():
    return f"http://127.0.0.1:{skill_config.get('panel_port')}"


def _armed():
    """Read LIVE from config each call so toggling it takes effect with no
    restart — and so a stale in-memory value can never leave the gate open."""
    return bool(skill_config.get("remote_exec_armed"))


def _http(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _local_get(path):
    return _http("GET", _local_url() + path)


def _local_post(path, body):
    return _http("POST", _local_url() + path, body)


# ---------------------------------------------------------------- metadata

# The ONLY task fields allowed across the boundary. Everything else
# (worktree, log_path, session_id, evidence file paths, branch, diff_source,
# raw featuretest blob) stays on this machine.
_TASK_FIELDS = ("id", "title", "description", "status", "tier", "source",
                "pr_url", "test_plan", "created_at", "updated_at")
_Q_FIELDS = ("id", "question", "options", "context", "status", "answer", "created_at")


def _filtered_state():
    """Build the metadata-only snapshot pushed up — the boundary is enforced
    HERE, by only ever copying whitelisted fields onto the wire."""
    try:
        s = _local_get("/api/state")
    except Exception:
        return None
    try:
        pj = _local_get("/api/projects")
    except Exception:
        pj = {"projects": [], "active": None}
    tasks = [{k: t.get(k) for k in _TASK_FIELDS}
             for t in s.get("tasks", []) if t.get("status") != "rejected"]
    questions = [{k: q.get(k) for k in _Q_FIELDS} for q in s.get("questions", [])]
    repo = s.get("repo") or ""
    # The projects list for the dashboard's home page. The repo PATH is
    # included (the user drives their own machine from their own token-gated
    # dashboard and needs to tell projects apart) — but still nothing about
    # the repo's CONTENTS ever crosses; this is a display label, not code.
    projects = [{"slug": p.get("slug"), "name": p.get("name"), "path": p.get("path"),
                 "push_mode": p.get("push_mode"),
                 "active": p.get("slug") == pj.get("active")}
                for p in pj.get("projects", [])]
    return {
        "tasks": tasks,
        "questions": questions,
        "progress": s.get("progress", {}),
        "counts": s.get("counts", {}),
        "paused": s.get("paused"),
        "cap": s.get("cap"),
        "projects": projects,
        "active_slug": pj.get("active"),
        "project": {
            # basename only — never the full local filesystem path
            "repo_name": os.path.basename(repo.rstrip("/")) if repo else None,
            "base_branch": s.get("base_branch"),
            "push_mode": s.get("push_mode"),
            "has_web_ui": s.get("has_web_ui"),
            "armed": _armed(),        # shown in the dashboard; settable only here
        },
    }


def _uplink_loop():
    while True:
        st = _filtered_state()
        if st is not None:
            try:
                _http("POST", RELAY_URL + "/api/relay/state",
                      {"token": TOKEN, "name": MACHINE_NAME, "state": st})
            except Exception:
                pass
        time.sleep(STATE_PUSH_EVERY)


# ---------------------------------------------------------------- commands

def _handle(cmd):
    """Dispatch one relayed command. Returns a small result dict for the ack."""
    ctype = cmd.get("type")
    payload = cmd.get("payload") or {}
    if ctype == "ping":
        return {"ok": True, "pong": True, "armed": _armed()}

    if ctype == "new_intake":
        # Safe: just makes the task-doc question available. Executes nothing.
        try:
            _local_post("/api/intake/new", {})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if ctype == "post_intake":
        # This is the one that can START A RUN — gate it hard on local arming.
        answer = (payload.get("answer") or "").strip()
        if not answer:
            return {"ok": False, "error": "empty answer"}
        if not _armed():
            # Refuse to execute. Surface WHY on the local board so the human
            # (who is the only one who can arm) sees it, and report the refusal
            # back up so the dashboard can say "waiting for local approval".
            try:
                _local_post("/api/progress", {
                    "label": "⚠ Remote task doc received — not started",
                    "detail": "This machine isn't armed for remote runs. Set "
                              "\"remote_exec_armed\": true in config.json here to "
                              "allow it, then resend.",
                    "status": "error"})
            except Exception:
                pass
            return {"ok": False, "refused": "not_armed"}
        # Armed: route through the SAME local path a human answer takes, so all
        # the existing gates (Notion-connected check, single-flight, etc.) still
        # apply. Ensure a question exists, then answer it.
        try:
            _local_post("/api/intake/new", {})
            s = _local_get("/api/state")
            q = next((x for x in s.get("pending_questions", [])
                      if x.get("session_id") == "manager-intake"), None)
            if not q:
                return {"ok": False, "error": "no intake question to answer"}
            _local_post(f"/api/questions/{q['id']}/answer", {"answer": answer})
            return {"ok": True, "started": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Board CONTROL commands — not code execution (they never run claude), so
    # they route straight through to the local API without the arm gate. Only
    # post_intake, which actually starts an agent, is gated.
    if ctype in ("pause", "resume", "clear_all", "set_cap"):
        route = {"pause": "/api/pause", "resume": "/api/resume",
                 "clear_all": "/api/reset", "set_cap": "/api/cap"}[ctype]
        body = {"cap": payload.get("cap")} if ctype == "set_cap" else {}
        try:
            _local_post(route, body)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Project management (home-page actions) — switch which project is active,
    # stop tracking one, or flip its PR/local mode. Not code execution, so no
    # arm gate. remove_project only stops TRACKING; it never touches the repo,
    # branches, or PRs (same as the local panel's 🗑).
    if ctype in ("activate_project", "remove_project", "set_push_mode"):
        route = {"activate_project": "/api/projects/activate",
                 "remove_project": "/api/projects/remove",
                 "set_push_mode": "/api/projects/push-mode"}[ctype]
        body = {"slug": payload.get("slug")}
        if ctype == "set_push_mode":
            body["push_mode"] = payload.get("push_mode")
        try:
            _local_post(route, body)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown command: {ctype}"}


def _downlink_loop():
    backoff = 1
    poll = RELAY_URL + "/api/relay/poll?" + urllib.parse.urlencode({"token": TOKEN})
    while True:
        try:
            r = _http("GET", poll, timeout=15)
            backoff = 1
        except Exception:
            time.sleep(min(backoff, 15))
            backoff *= 2
            continue
        cmd = r.get("command")
        if not cmd:
            time.sleep(POLL_EVERY)   # nothing queued — wait, then poll again
            continue
        result = _handle(cmd)
        try:
            _http("POST", RELAY_URL + "/api/relay/ack",
                  {"token": TOKEN, "cmd_id": cmd.get("id"), "result": result})
        except Exception:
            pass


def main():
    if not RELAY_URL or not TOKEN:
        sys.exit("relay_client: set MANAGER_RELAY_URL and MANAGER_MACHINE_TOKEN")
    print(f"relay_client: machine={MACHINE_NAME!r} armed={_armed()} "
          f"relay={RELAY_URL} local={_local_url()}")
    t = threading.Thread(target=_uplink_loop, daemon=True)
    t.start()
    _downlink_loop()


if __name__ == "__main__":
    main()
