#!/usr/bin/env python3
"""
archive.py — persist everything a manager run produced into the archive dir
(config `archive_dir`), organized one folder per task so a finished batch is
fully recoverable later.

Layout:
  <archive_dir>/
    INDEX.md                                  # one row per archived run
    task-22-uploads-item-image-403/
      meta.json        # task row: title, source, tier, status, pr_url, session, evidence, timestamps
      summary.md       # human-readable card (what/where/PR/test url)
      run.log          # raw stream-json transcript of the headless ship-task run
      transcript.md    # distilled, readable: assistant text + tool calls, in order
      changes.diff     # the actual code change (gh pr diff, else worktree diff)

Called automatically by the daemon when a run settles (done/failed), and available
as a CLI to backfill the whole queue:  python3 archive.py --all
"""
import os, re, json, shutil, subprocess, time

import manager_core as core
import skill_config

REPO = skill_config.get("repo")
BASE_BRANCH = skill_config.get("base_branch")
ARCHIVE_ROOT = skill_config.get("archive_dir")
WORKTREE_DIR = skill_config.get("worktree_dir")


def _slug(s, n=48):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-") or "untitled"


def _run(args, cwd=None, timeout=60):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def _pr_number(pr_url):
    m = re.search(r"/pull/(\d+)", pr_url or "")
    return m.group(1) if m else None


def _distill_transcript(log_path):
    """Turn the stream-json run log into a readable transcript: each assistant
    text block and tool call, in order. Best-effort; skips unparseable lines."""
    out = []
    try:
        lines = open(log_path, errors="ignore").read().splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        typ = o.get("type")
        if typ == "assistant":
            for blk in (o.get("message", {}) or {}).get("content", []) or []:
                if blk.get("type") == "text" and blk.get("text", "").strip():
                    out.append(blk["text"].strip())
                elif blk.get("type") == "tool_use":
                    inp = blk.get("input", {}) or {}
                    arg = (inp.get("file_path") or inp.get("command")
                           or inp.get("description") or inp.get("pattern")
                           or inp.get("path") or "")
                    out.append(f"`🔧 {blk.get('name','')}` {str(arg)[:200]}")
        elif typ == "result":
            r = o.get("result")
            if r:
                out.append(f"\n---\n**result:** {str(r)[:1000]}")
    return "\n\n".join(out)


def _find_worktree_for(task):
    """Best-effort: locate this task's dev worktree so we can diff it when there's
    no PR yet. Match by branch (if recorded) else by a slug of the title."""
    if not os.path.isdir(WORKTREE_DIR):
        return None
    cands = [os.path.join(WORKTREE_DIR, d) for d in os.listdir(WORKTREE_DIR)]
    cands = [d for d in cands if os.path.isdir(d)]
    branch = task.get("branch")
    if branch:
        for d in cands:
            rc, head, _ = _run(["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"])
            if rc == 0 and head.strip() == branch:
                return d
    key = _slug(task["title"], 24).split("-")[0]  # weak heuristic on first word
    for d in cands:
        if key and key in os.path.basename(d).lower():
            return d
    return None


def _capture_diff(task):
    """The actual code change for this run. Prefer the PR diff (authoritative),
    fall back to the worktree's diff vs the base branch."""
    pr = _pr_number(task.get("pr_url"))
    if pr:
        # run gh inside the target repo so it infers owner/repo from the remote
        rc, out, _ = _run(["gh", "pr", "diff", pr], cwd=REPO or None, timeout=90)
        if rc == 0 and out.strip():
            return out, f"gh pr diff #{pr}"
    wt = _find_worktree_for(task)
    if wt:
        _run(["git", "-C", wt, "fetch", "origin", BASE_BRANCH, "--quiet"], timeout=60)
        rc, out, _ = _run(["git", "-C", wt, "diff", f"origin/{BASE_BRANCH}...HEAD"], timeout=60)
        if rc == 0 and out.strip():
            return out, f"git diff origin/{BASE_BRANCH}...HEAD ({os.path.basename(wt)})"
        # include uncommitted work too
        rc, out2, _ = _run(["git", "-C", wt, "diff"], timeout=60)
        if rc == 0 and out2.strip():
            return out2, f"git diff (uncommitted, {os.path.basename(wt)})"
    return "", "no diff captured (no PR and no matching worktree)"


def archive_task(tid):
    """Write the full record for one task. Idempotent — safe to re-run."""
    t = core.get_task(tid)
    if not t:
        return None
    folder = os.path.join(ARCHIVE_ROOT, f"task-{tid:02d}-{_slug(t['title'])}")
    os.makedirs(folder, exist_ok=True)

    # 1) raw run log
    log_path = t.get("log_path")
    if log_path and os.path.exists(log_path):
        shutil.copyfile(log_path, os.path.join(folder, "run.log"))
        transcript = _distill_transcript(log_path)
        if transcript:
            with open(os.path.join(folder, "transcript.md"), "w") as f:
                f.write(f"# {t['title']}\n\nSession `{t.get('session_id','')}`\n\n{transcript}\n")

    # 2) code changes
    diff, diff_src = _capture_diff(t)
    with open(os.path.join(folder, "changes.diff"), "w") as f:
        f.write(diff if diff else f"(no diff: {diff_src})\n")

    # 3) metadata
    meta = {
        "id": t["id"], "title": t["title"], "status": t["status"],
        "tier": t.get("tier"), "source": t.get("source"),
        "pr_url": t.get("pr_url"), "web_url": t.get("web_url"),
        "session_id": t.get("session_id"), "branch": t.get("branch"),
        "evidence": t.get("evidence"), "diff_source": diff_src,
        "description": t.get("description"), "test_plan": t.get("test_plan"),
        "archived_at": time.time(),
    }
    with open(os.path.join(folder, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # 4) human-readable summary
    ev = t.get("evidence") or []
    md = [
        f"# Task #{t['id']} — {t['title']}",
        "",
        f"- **Status:** {t['status']}",
        f"- **Tier:** {t.get('tier')}",
        f"- **Source:** {t.get('source') or '—'}",
        f"- **PR:** {t.get('pr_url') or '—'}",
        f"- **Test URL:** {t.get('web_url') or '—'}",
        f"- **Session:** `{t.get('session_id') or '—'}`",
        f"- **Diff captured via:** {diff_src}",
        f"- **Evidence:** {', '.join(ev) if ev else '—'}",
        "",
        "## Description / acceptance",
        "",
        (t.get("description") or "—"),
        "",
        "## How to test",
        "",
        ("```\n" + t["test_plan"] + "\n```") if t.get("test_plan") else "_(test guide reported with the PR)_",
        "",
        "## Files",
        "- `run.log` — raw stream-json of the headless run",
        "- `transcript.md` — readable transcript (assistant + tool calls)",
        "- `changes.diff` — the code change this run produced",
        "- `meta.json` — machine-readable record",
    ]
    with open(os.path.join(folder, "summary.md"), "w") as f:
        f.write("\n".join(md) + "\n")

    _update_index()
    return folder


def _update_index():
    rows = []
    for t in core.list_tasks():
        folder = f"task-{t['id']:02d}-{_slug(t['title'])}"
        if not os.path.isdir(os.path.join(ARCHIVE_ROOT, folder)):
            continue
        pr = t.get("pr_url") or ""
        prcell = f"[PR]({pr})" if pr else "—"
        rows.append((t["id"], t["status"], t["title"], prcell, folder))
    rows.sort()
    lines = [
        "# Manager runs — archive index",
        "",
        f"_Updated {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} run(s)._",
        "",
        "| # | Status | Task | PR | Folder |",
        "|---|--------|------|----|--------|",
    ]
    for tid, st, title, prcell, folder in rows:
        safe = title.replace("|", "\\|")
        lines.append(f"| {tid} | {st} | {safe} | {prcell} | `{folder}/` |")
    os.makedirs(ARCHIVE_ROOT, exist_ok=True)
    with open(os.path.join(ARCHIVE_ROOT, "INDEX.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def archive_all(statuses=None):
    """Archive every task (optionally only certain statuses). Returns folders."""
    done = []
    for t in core.list_tasks():
        if statuses and t["status"] not in statuses:
            continue
        if not (t.get("log_path") or t.get("pr_url")):
            continue  # nothing to capture for a never-dispatched task
        f = archive_task(t["id"])
        if f:
            done.append(f)
    return done


if __name__ == "__main__":
    import argparse
    core.init_db()
    ap = argparse.ArgumentParser(description="archive manager runs into the archive dir")
    ap.add_argument("--all", action="store_true", help="archive every dispatched task")
    ap.add_argument("--task", type=int, help="archive one task id")
    args = ap.parse_args()
    if args.task:
        print("archived:", archive_task(args.task))
    else:
        folders = archive_all()
        print(f"archived {len(folders)} run(s) -> {ARCHIVE_ROOT}")
        for f in folders:
            print("  ", os.path.basename(f))
