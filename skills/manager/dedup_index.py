#!/usr/bin/env python3
"""
dedup_index.py — cheap, deterministic dedup PRE-FILTER for /manager Phase 3.

The manager's biggest value-add is dropping already-shipped work; today it
re-reasons that from scratch with the LLM for every candidate, issuing many
`git log | grep` / memory searches. This builds a shipped-work index ONCE from
`git log origin/<base_branch>` + the manager archive, then matches every candidate
so the LLM only has to reason over the UNCERTAIN ones — and even those arrive with
their likely-matching PR pre-attached, instead of the agent grepping per candidate.

SAFETY (this is a pre-filter, never the decider):
  • AUTO-DROP only on the ONE unambiguous signal — an explicit PR# in the
    candidate that is confirmed on the base branch. Nothing semantic auto-drops.
  • EVERY title/keyword match (even a near-identical title) → "likely-dup": the
    match (and its PR) is surfaced and the LLM confirms — because a QA doc may be
    re-reporting that a *shipped* feature regressed, which only the LLM can judge.
  • No overlap → "novel". Verdicts are ADVISORY: the manager LLM makes the final
    keep/drop call and can override any verdict.

Usage:
  python3 dedup_index.py build                      # print the shipped index (debug)
  python3 dedup_index.py match "<candidate text>"   # one candidate
  echo '["cand 1","cand 2", …]' | python3 dedup_index.py match --stdin   # batch
  python3 dedup_index.py --selftest                 # offline logic checks

Settings come from skill_config (config.json + env overrides: MANAGER_REPO,
MANAGER_ARCHIVE, MANAGER_BASE_BRANCH). `build`/`match` fetch the base branch
best-effort. Output: JSON.
"""
import glob
import json
import os
import re
import subprocess
import sys

import skill_config

REPO = skill_config.get("repo")
BASE_BRANCH = skill_config.get("base_branch")
ARCHIVE_ROOT = skill_config.get("archive_dir")
LOG_LIMIT = int(os.environ.get("DEDUP_LOG_LIMIT", "3000"))

# Generic words that don't disambiguate one task from another — dropped before
# matching so overlap reflects DOMAIN nouns (invoice, order, customer…), not
# scaffolding (button, page, fix…). Domain nouns are intentionally NOT listed.
_STOP = {
    "the", "a", "an", "to", "for", "in", "of", "and", "or", "on", "with", "from",
    "by", "at", "as", "is", "are", "be", "it", "its", "this", "that", "these",
    "those", "into", "via", "per", "out", "up", "off", "down", "all", "any",
    "fix", "fixes", "fixed", "fixing", "add", "adds", "added", "adding", "update",
    "updates", "updated", "remove", "removes", "removed", "make", "makes", "made",
    "set", "sets", "use", "using", "support", "enable", "enabled", "allow",
    "allows", "show", "shows", "hide", "hides", "handle", "improve", "ensure",
    "prevent", "wire", "wires", "wired", "move", "moved", "rename", "renamed",
    "bump", "bug", "feature", "feat", "chore", "refactor", "doc", "docs", "test",
    "tests", "wip", "page", "pages", "view", "views", "button", "buttons", "modal",
    "form", "field", "fields", "section", "panel", "list", "table", "row", "rows",
    "card", "cards", "ui", "ux", "web", "app", "apps", "server", "client", "api",
    "endpoint", "flow", "screen", "tab", "menu", "item", "items", "new", "old",
    "default", "correct", "proper", "small", "minor", "issue", "error", "errors",
    "wrong", "not", "no", "when", "where", "which", "should", "does", "doesnt",
    "cant", "wont", "still", "again", "also", "only", "now", "yet", "some",
}
_PR_RE = re.compile(r"(?:#|pull/|\bpr\s*#?\s*)(\d{1,6})\b", re.IGNORECASE)
_CC_PREFIX = re.compile(r"^\s*[a-z]+(\([^)]*\))?!?:\s*", re.IGNORECASE)


def _run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def _tokens(text):
    """Significant domain tokens of a string (lowercased, stopwords/scaffolding removed)."""
    s = _CC_PREFIX.sub("", text or "")
    s = _PR_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    out = set()
    for w in s.split():
        if len(w) >= 3 and not w.isdigit() and w not in _STOP:
            out.add(w)
    return out


def _pr_numbers(text):
    return {m.group(1) for m in _PR_RE.finditer(text or "")}


def build_index(do_fetch=True):
    """Return {'base_prs': set[str], 'entries': [ {pr, source, subject, tokens} ]}."""
    if do_fetch:
        _run(["git", "-C", REPO, "fetch", "origin", BASE_BRANCH, "--quiet"], timeout=45)
    entries, base_prs = [], set()

    rc, out, _ = _run(
        ["git", "-C", REPO, "log", f"origin/{BASE_BRANCH}",
         f"-n{LOG_LIMIT}", "--pretty=format:%s"], timeout=60)
    if rc == 0:
        for subj in out.splitlines():
            subj = subj.strip()
            if not subj:
                continue
            prs = _pr_numbers(subj)
            base_prs |= prs
            entries.append({
                "pr": next(iter(prs)) if prs else None,
                "source": "base",
                "subject": subj,
                "tokens": _tokens(subj),
            })

    # Prior manager runs that already reached a PR (in-flight; avoid re-dispatch).
    for meta_path in glob.glob(os.path.join(ARCHIVE_ROOT, "task-*", "meta.json")):
        try:
            m = json.load(open(meta_path))
        except (OSError, ValueError):
            continue
        title = m.get("title") or ""
        if not title:
            continue
        entries.append({
            "pr": (_pr_numbers(m.get("pr_url") or "") or {None}).pop(),
            "source": "archive",
            "subject": title + " — " + (m.get("status") or ""),
            "tokens": _tokens(title),
        })
    return {"base_prs": base_prs, "entries": entries}


def _score(cand_tokens, entry_tokens):
    """Overlap coefficient |∩|/min(|·|), but 0 unless ≥2 significant words are shared."""
    shared = cand_tokens & entry_tokens
    if len(shared) < 2:
        return 0.0, shared
    denom = min(len(cand_tokens), len(entry_tokens))
    return (len(shared) / denom if denom else 0.0), shared


def match_one(candidate, index):
    cand_tokens = _tokens(candidate)
    cand_prs = _pr_numbers(candidate)

    # 1) Strongest signal: an explicit PR# in the candidate that is on the base branch.
    shipped_pr = cand_prs & index["base_prs"]
    if shipped_pr:
        pr = sorted(shipped_pr)[0]
        return {
            "verdict": "auto-drop",
            "reason": f"candidate cites #{pr}, which is on origin/{BASE_BRANCH}",
            "matches": [{"pr": pr, "source": "base", "score": 1.0}],
        }

    # 2) Title-overlap ranking.
    scored = []
    for e in index["entries"]:
        sc, shared = _score(cand_tokens, e["tokens"])
        if sc > 0:
            scored.append((sc, len(shared), e, sorted(shared)))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [{
        "pr": e["pr"], "source": e["source"], "subject": e["subject"],
        "score": round(sc, 3), "shared": shared,
    } for sc, _n, e, shared in scored[:3]]

    if not scored:
        return {"verdict": "novel", "reason": "no shipped item shares ≥2 domain words",
                "matches": []}

    best_sc = scored[0][0]
    # NOTHING semantic auto-drops — a strong title match still goes to the LLM,
    # because the candidate may be re-reporting a regression in the shipped work.
    if best_sc >= 0.5:
        near = " (near-identical title)" if best_sc >= 0.9 else ""
        return {"verdict": "likely-dup",
                "reason": f"overlaps shipped work{near} (best {best_sc:.2f}) — confirm before enqueueing",
                "matches": top}
    return {"verdict": "novel",
            "reason": f"only weak overlap (best {best_sc:.2f}) — treat as new",
            "matches": top}


def _selftest():
    # Offline: hand-built index, assert the verdict logic + thresholds.
    idx = {
        "base_prs": {"256", "255"},
        "entries": [
            {"pr": "256", "source": "base",
             "subject": "feat(invoices): hide actions on upcoming draft invoices",
             "tokens": _tokens("feat(invoices): hide actions on upcoming draft invoices")},
            {"pr": "255", "source": "base",
             "subject": "feat(sharing): browse collaborator inventory by default",
             "tokens": _tokens("feat(sharing): browse collaborator inventory by default")},
            {"pr": None, "source": "base",
             "subject": "fix sid bar hamburger (mobile)",
             "tokens": _tokens("fix sid bar hamburger (mobile)")},
        ],
    }
    checks = [
        # explicit PR# on the base branch -> auto-drop
        ("Bug: see PR #256, invoice issue", "auto-drop"),
        # strong domain overlap with #256 -> likely-dup (LLM confirms)
        ("Draft invoices show actions that error on upcoming orders", "likely-dup"),
        # overlap with collaborator-inventory PR -> likely-dup
        ("Collaborator inventory should browse by default", "likely-dup"),
        # unrelated -> novel
        ("Add a dark mode toggle to the settings screen", "novel"),
        # single shared word only -> novel (needs >=2)
        ("Mobile keyboard covers the input", "novel"),
    ]
    fails = []
    for text, exp in checks:
        got = match_one(text, idx)["verdict"]
        if got != exp:
            fails.append(f"{text!r}: got {got}, expected {exp}")
    # Guard the safety property: weak overlap must NEVER auto-drop.
    weak = match_one("update the order list header", idx)
    if weak["verdict"] == "auto-drop":
        fails.append("SAFETY: weak overlap auto-dropped")
    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST PASSED: {len(checks)} cases + safety guard")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(_selftest())
    if not args:
        print(__doc__)
        sys.exit(2)
    cmd = args[0]
    if cmd == "build":
        idx = build_index()
        print(json.dumps({
            "base_prs": sorted(idx["base_prs"]),
            "entries": len(idx["entries"]),
            "sample": [e["subject"] for e in idx["entries"][:8]],
        }, indent=2))
        return
    if cmd == "match":
        idx = build_index()
        rest = args[1:]
        if "--stdin" in rest:
            cands = json.load(sys.stdin)
        else:
            cands = [" ".join(rest)]
        results = [{"candidate": c, **match_one(c, idx)} for c in cands]
        print(json.dumps(results, indent=2))
        return
    print(f"dedup_index.py: unknown command {cmd!r}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
