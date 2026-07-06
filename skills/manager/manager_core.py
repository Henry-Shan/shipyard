"""
manager_core — the data layer for the /manager orchestrator.

A single SQLite database holds three things:

  tasks      — the curated work queue (one row per dispatchable unit of work)
  questions  — the "suggestions inbox": questions a running /ship-task session
               relayed to the human, plus the human's answers
  events     — an append-only activity log for the UI timeline

Everything here is pure stdlib (sqlite3) so the daemon has zero install step.
The HTTP daemon (managerd.py) and the agent CLI both import this module; it is
the one source of truth for state. Keep business logic here, not in the daemon.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "MANAGER_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "manager.db"),
)

# Task lifecycle. A task moves queued -> running, may bounce to parked while it
# waits on a human answer, then settles in done / failed / rejected.
TASK_STATUSES = (
    "queued",      # confident; eligible for autonomous dispatch
    "needs_input", # agent investigated and still needs ONE human preference call
    "running",     # a headless /ship-task session is working it
    "parked",      # the running session asked a question and is waiting
    "done",        # PR-ready: green PR open, waiting for the human's final merge
    "failed",      # ship-task stopped at a gate or errored out
    "blocked",     # cannot be done here (external dep / investigate-only / needs scope)
    "rejected",    # dropped
    "draft",       # legacy: extracted but not yet acted on (autonomous mode skips this)
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',     -- e.g. "QA doc p4 Bug 1.1.2"
    evidence      TEXT NOT NULL DEFAULT '[]',    -- json: list of image file paths
    tier          INTEGER,                       -- ship-task tier 0..3 (null = untriaged)
    triage_note   TEXT NOT NULL DEFAULT '',      -- why this tier / dedup result vs the base branch
    open_question TEXT NOT NULL DEFAULT '',      -- what must be resolved before/while dispatch
    status        TEXT NOT NULL DEFAULT 'draft',
    sort_order    INTEGER NOT NULL DEFAULT 0,    -- human ranking; lower = sooner
    session_id    TEXT,                          -- headless claude session running this
    worktree      TEXT,
    branch        TEXT,
    pr_url        TEXT,
    log_path      TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,                  -- which ship-task run is asking
    task_id      INTEGER,                        -- fk -> tasks.id (may be null)
    question     TEXT NOT NULL,
    options      TEXT NOT NULL DEFAULT '[]',     -- json: suggested answer options
    context      TEXT NOT NULL DEFAULT '',       -- ship-task's own notes (PR link, test steps)
    answer       TEXT,                           -- human's reply (null = pending)
    status       TEXT NOT NULL DEFAULT 'pending',-- pending | answered | delivered
    created_at   REAL NOT NULL,
    answered_at  REAL,
    delivered_at REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c):
    """Idempotent column adds so an existing manager.db gains new fields."""
    have = {r["name"] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
    for col, decl in (("web_port", "INTEGER"), ("web_url", "TEXT"),
                      ("server_url", "TEXT"),
                      ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                      # resume=1 => the next dispatch should `claude --resume` the
                      # existing session_id (reusing its worktree) instead of a
                      # fresh run. Set when a rate-limited run is auto-requeued.
                      ("resume", "INTEGER NOT NULL DEFAULT 0"),
                      # copyable manual-test guide the run reports when it opens a PR
                      ("test_plan", "TEXT"),
                      # the run's git worktree path (for the per-task Test button)
                      ("worktree", "TEXT"),
                      # why a run stopped early (session/usage limit, network drop)
                      ("stopped_reason", "TEXT"),
                      # feature-tester: json of the last exhausted-fix-loop failing
                      # test {test, attempts, error} — null once cleared on dispatch
                      ("failtest_info", "TEXT"),
                      # feature-tester: the full verdict record for the card —
                      # {verdict,test,cycles,output,turns}; ALL outcomes, not just fail
                      ("featuretest", "TEXT"),
                      # real screenshot(s) of the FIXED behavior (json list of paths),
                      # captured by the run itself (e.g. via /browse) once the feature
                      # is verified — shown as first-hand "after" evidence next to the
                      # finding's original "before" screenshots. Never fabricated:
                      # stays '[]' unless the run actually captured one.
                      ("after_evidence", "TEXT NOT NULL DEFAULT '[]'")):
        if col not in have:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    # questions table: a `kind` discriminates a plain preference question from a
    # "verify-fixed" one (answering yes deletes the candidate instead of queueing it).
    qhave = {r["name"] for r in c.execute("PRAGMA table_info(questions)").fetchall()}
    if "kind" not in qhave:
        c.execute("ALTER TABLE questions ADD COLUMN kind TEXT NOT NULL DEFAULT ''")


def _now():
    return time.time()


def log_event(c, kind, message="", task_id=None):
    c.execute(
        "INSERT INTO events (task_id, kind, message, created_at) VALUES (?,?,?,?)",
        (task_id, kind, message, _now()),
    )


# ---------------------------------------------------------------- tasks

def add_task(title, description="", source="", evidence=None, tier=None,
             triage_note="", open_question="", status="draft", sort_order=None):
    evidence = evidence or []
    with connect() as c:
        if sort_order is None:
            row = c.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM tasks").fetchone()
            sort_order = row["n"]
        cur = c.execute(
            """INSERT INTO tasks
               (title, description, source, evidence, tier, triage_note,
                open_question, status, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (title, description, source, json.dumps(evidence), tier, triage_note,
             open_question, status, sort_order, _now(), _now()),
        )
        tid = cur.lastrowid
        log_event(c, "task.created", title, tid)
        return tid


def update_task(task_id, **fields):
    if not fields:
        return
    if "evidence" in fields and not isinstance(fields["evidence"], str):
        fields["evidence"] = json.dumps(fields["evidence"])
    if "after_evidence" in fields and not isinstance(fields["after_evidence"], str):
        fields["after_evidence"] = json.dumps(fields["after_evidence"])
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with connect() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)
        log_event(c, "task.updated", ", ".join(fields.keys()), task_id)


def set_status(task_id, status, **extra):
    assert status in TASK_STATUSES, f"bad status {status}"
    update_task(task_id, status=status, **extra)


def get_task(task_id):
    with connect() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _task_dict(row) if row else None


def list_tasks(status=None):
    q = "SELECT * FROM tasks"
    args = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY sort_order ASC, id ASC"
    with connect() as c:
        return [_task_dict(r) for r in c.execute(q, args).fetchall()]


def next_queued():
    """The highest-ranked task eligible to dispatch."""
    with connect() as c:
        row = c.execute(
            "SELECT * FROM tasks WHERE status='queued' ORDER BY sort_order ASC, id ASC LIMIT 1"
        ).fetchone()
        return _task_dict(row) if row else None


def active_count():
    """How many slots are occupied (a parked task still owns its claude process)."""
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('running','parked')"
        ).fetchone()
        return row["n"]


def _task_dict(row):
    d = dict(row)
    d["evidence"] = json.loads(d.get("evidence") or "[]")
    d["after_evidence"] = json.loads(d.get("after_evidence") or "[]")
    return d


# ---------------------------------------------------------------- relay / inbox

def add_question(session_id, question, options=None, context="", task_id=None, kind=""):
    """A running ship-task session relays a question to the human. Idempotent
    for identical pending questions: re-posting the same (session, question)
    returns the existing row instead of stacking duplicates on the board."""
    options = options or []
    with connect() as c:
        dup = c.execute(
            "SELECT id FROM questions WHERE session_id=? AND question=? AND status='pending'",
            (session_id, question),
        ).fetchone()
        if dup:
            return dup["id"]
        if task_id is None:
            r = c.execute("SELECT id FROM tasks WHERE session_id=?", (session_id,)).fetchone()
            task_id = r["id"] if r else None
        cur = c.execute(
            """INSERT INTO questions
               (session_id, task_id, question, options, context, status, created_at, kind)
               VALUES (?,?,?,?,?, 'pending', ?, ?)""",
            (session_id, task_id, question, json.dumps(options), context, _now(), kind),
        )
        if task_id:
            # Only a LIVE run gets parked; a pre-dispatch preference call (set to
            # needs_input by ask_preference) keeps that status.
            cur_status = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if cur_status and cur_status["status"] == "running":
                c.execute("UPDATE tasks SET status='parked', updated_at=? WHERE id=?",
                          (_now(), task_id))
                log_event(c, "task.parked", question[:120], task_id)
        return cur.lastrowid


_FIXED_RE = re.compile(r"\b(already fixed|is fixed|fixed|drop|done|resolved|yes)\b", re.I)


def answer_question(question_id, answer):
    """The human answers via the UI. A preference question on a not-yet-dispatched
    task (needs_input) releases it to the queue; a question from a live run
    (parked/running) lets that run continue. A 'verify_fixed' question is special:
    answering that it IS already fixed DELETES the candidate; otherwise it queues."""
    with connect() as c:
        c.execute(
            "UPDATE questions SET answer=?, status='answered', answered_at=? WHERE id=?",
            (answer, _now(), question_id),
        )
        row = c.execute(
            "SELECT q.task_id, q.kind, q.options, t.status FROM questions q "
            "LEFT JOIN tasks t ON t.id=q.task_id WHERE q.id=?",
            (question_id,),
        ).fetchone()
        if not (row and row["task_id"]):
            return
        tid = row["task_id"]
        if row["kind"] == "verify_fixed":
            # the FIRST option is the "already fixed — drop" choice. If the answer is
            # one of the offered options, trust the choice (index 0 == fixed). Only for
            # free-text do we sniff intent — and respect negations ("not fixed", "keep").
            opts = []
            try:
                opts = json.loads(row["options"] or "[]")
            except Exception:
                pass
            ans = (answer or "").strip()
            if opts and ans in opts:
                is_fixed = (ans == opts[0])
            else:
                low = ans.lower()
                negated = re.search(r"\b(not|isn'?t|keep|build|still|never|n'?t)\b", low)
                is_fixed = bool(_FIXED_RE.search(low)) and not negated
            if is_fixed:
                log_event(c, "task.answered", f"already-fixed confirmed -> delete: {answer[:80]}", tid)
                c.execute("DELETE FROM questions WHERE task_id=?", (tid,))
                c.execute("DELETE FROM tasks WHERE id=?", (tid,))
                return
            c.execute("UPDATE tasks SET status='queued', updated_at=? WHERE id=?", (_now(), tid))
            log_event(c, "task.answered", f"not fixed -> queued: {answer[:80]}", tid)
            return
        nxt = "queued" if row["status"] == "needs_input" else "running"
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (nxt, _now(), tid))
        log_event(c, "task.answered", f"-> {nxt}: {answer[:100]}", tid)


def ask_preference(task_id, question, options=None, context=""):
    """The agent escalates ONE genuine preference call it couldn't resolve by
    investigation. Parks the task as needs_input until the human answers."""
    set_status(task_id, "needs_input")
    return add_question(f"agent-task-{task_id}", question, options, context, task_id)


def ask_if_fixed(task_id, context=""):
    """For a candidate that LOOKS already shipped: instead of silently dropping it,
    park it in the inbox and ask the human to confirm. Answering 'already fixed'
    DELETES it; 'not fixed' queues it to be built."""
    set_status(task_id, "needs_input")
    return add_question(f"agent-task-{task_id}",
                        "This looks already fixed — is it?",
                        ["✓ Already fixed — drop it", "✗ Not fixed — build it"],
                        context, task_id, kind="verify_fixed")


def poll_answer(session_id):
    """Called by the dispatched session: returns the oldest answered-but-undelivered
    answer for this session and marks it delivered, or None if nothing is waiting."""
    with connect() as c:
        row = c.execute(
            """SELECT id, answer FROM questions
               WHERE session_id=? AND status='answered'
               ORDER BY answered_at ASC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE questions SET status='delivered', delivered_at=? WHERE id=?",
                  (_now(), row["id"]))
        return row["answer"]


def set_runinfo(session_id, web_url=None, server_url=None, pr_url=None, test_plan=None,
                 after_evidence=None):
    """A dispatched ship-task run reports the localhost URL its worktree is
    serving on (so the human can test that task's branch), its PR once open,
    a copyable step-by-step manual-test guide for that change, and — only when
    it genuinely captured one (e.g. via /browse against the live app) —
    screenshot(s) of the FIXED behavior, shown as first-hand "after" evidence
    next to the finding's original "before" screenshots. Never fabricated: a
    run with nothing to show simply omits after_evidence."""
    with connect() as c:
        row = c.execute("SELECT id, test_plan FROM tasks WHERE session_id=?",
                        (session_id,)).fetchone()
        if not row:
            return
        fields, vals = [], []
        if web_url:
            fields.append("web_url=?"); vals.append(web_url)
        if server_url:
            fields.append("server_url=?"); vals.append(server_url)
        if pr_url:
            fields.append("pr_url=?"); vals.append(pr_url)
        # test_plan is WRITE-ONCE: the first run to open a PR sets the manual-test
        # guide and it stays put. A later "Doesn't work →" rework reuses the same
        # session and may re-report a guide; we ignore it so the human always sees
        # the original steps they were testing against.
        if test_plan and not (row["test_plan"] or "").strip():
            fields.append("test_plan=?"); vals.append(test_plan)
        if after_evidence:
            fields.append("after_evidence=?"); vals.append(json.dumps(after_evidence))
        if not fields:
            return
        fields.append("updated_at=?"); vals.append(_now())
        vals.append(row["id"])
        c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", vals)
        # Log a breadcrumb. A pr-only or test_plan-only report has no serving URL,
        # so coalesce to a sensible message (events.message is NOT NULL — a bare
        # None here used to crash the daemon thread).
        core_url = web_url or server_url
        if core_url:
            evt, msg = "task.serving", core_url
        elif pr_url:
            evt, msg = "task.pr", pr_url
        else:
            evt, msg = "task.runinfo", ""
        c.execute("INSERT INTO events (task_id, kind, message, created_at) VALUES (?,?,?,?)",
                  (row["id"], evt, msg, _now()))


def set_featuretest(session_id, verdict, test="", cycles=0, output="", turns=None):
    """Record the feature-tester's verdict for a run so the panel can DISPLAY it on
    the task card — for EVERY outcome (pass | skipped | blocked | fail), not just
    failures. `turns` is an optional list of {n, note, result} cycle summaries (what
    the tester/builder did each round). Read-only display; the loop already ran
    automatically. Cleared on the next (re)dispatch."""
    try:
        cycles = int(cycles or 0)
    except (TypeError, ValueError):
        cycles = 0
    if verdict not in ("pass", "fail", "blocked", "skipped"):
        verdict = "fail"
    safe_turns = []
    for t in (turns or [])[:12]:
        if isinstance(t, dict):
            safe_turns.append({
                "n": t.get("n"),
                "note": str(t.get("note", ""))[:200],
                "result": str(t.get("result", ""))[:24],
            })
    info = json.dumps({
        "verdict": verdict,
        "test": test or "",
        "cycles": cycles,
        "output": (output or "")[:1200],
        "turns": safe_turns,
    })
    with connect() as c:
        row = c.execute("SELECT id FROM tasks WHERE session_id=?",
                        (session_id,)).fetchone()
        if not row:
            return
        c.execute("UPDATE tasks SET featuretest=?, updated_at=? WHERE id=?",
                  (info, _now(), row["id"]))
        c.execute("INSERT INTO events (task_id, kind, message, created_at) VALUES (?,?,?,?)",
                  (row["id"], "task.featuretest", f"{verdict}: {test or 'feature test'}", _now()))


def add_failtest(session_id, test_name="", error_output="", attempts=0):
    """Back-compat shim — a still-failing exhausted feature test is verdict 'fail'."""
    set_featuretest(session_id, "fail", test=test_name, cycles=attempts, output=error_output)


def list_questions(only_pending=False):
    q = "SELECT * FROM questions"
    if only_pending:
        q += " WHERE status='pending'"
    q += " ORDER BY created_at DESC LIMIT 200"
    with connect() as c:
        out = []
        for r in c.execute(q).fetchall():
            d = dict(r)
            d["options"] = json.loads(d.get("options") or "[]")
            out.append(d)
        return out


def list_events(limit=100):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def snapshot():
    """Everything the UI needs in one shot."""
    tasks = list_tasks()
    counts = {}
    for s in TASK_STATUSES:
        counts[s] = sum(1 for t in tasks if t["status"] == s)
    return {
        "tasks": tasks,
        "counts": counts,
        "active": active_count(),
        "questions": list_questions(),
        "pending_questions": list_questions(only_pending=True),
        "events": list_events(40),
        "now": _now(),
    }


def delete_task(task_id):
    """Remove a task from the queue entirely (used after the human has merged its
    PR and cleaned up the worktree). Its questions go too; the event log keeps a
    breadcrumb."""
    with connect() as c:
        c.execute("DELETE FROM questions WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        log_event(c, "task.removed", "cleaned up after merge", task_id)


def reset_queue():
    """Clear all tasks, questions, and events — a fresh /manager run starts clean.
    Does NOT touch git worktrees (those may hold unpushed work; remove via button)."""
    with connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        c.execute("DELETE FROM questions")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM tasks")
        log_event(c, "queue.reset", f"cleared {n} task(s) from a previous run")
        return n


def new_session_id():
    return str(uuid.uuid4())


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
