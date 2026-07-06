# manager — an autonomous engineering manager for Claude Code

> **🤖 Are you an AI agent asked to install this?** Follow the steps in
> [`install.md`](./install.md) — it is written for you. Install all three
> skills (`manager`, `ship-task`, `feature-tester`); they are siblings and
> depend on each other.

Hand it a messy work source — a Notion page of QA findings, a PDF full of
screenshots, or a typed list — and it turns that into merged-ready pull
requests: grounded in your repo, deduped against what already shipped, triaged,
built in parallel by autonomous agents, and handed back to you as green PRs with
manual-test guides. You review and merge; it does everything else.

```
you: /manager ~/Desktop/qa-findings.pdf
        │
        ▼
┌─────────────────────  manager (the brain)  ─────────────────────┐
│ ingest → ground in the repo → dedup vs shipped work → triage    │
│ → queue → dispatch N parallel workers → relay their questions   │
│ localhost control panel: live board · inbox · test buttons      │
└──────────────┬───────────────────────────────────────────────────┘
               ▼  (up to dispatch_cap at once, each in its own git worktree)
     ship-task (the hands)             feature-tester (the verifier)
     branch → tests → build →   ←──    a FRESH agent proves the key
     verify gate → PR-ready            behavior with one blind test
               │
               ▼
     green PR on your base branch  →  YOU test + merge (always a human)
```

## The three skills

| Skill | Role |
|---|---|
| `skills/manager` | **The brain.** Ingests the work source, grounds every candidate in the repo, drops already-shipped work (deterministic git-based pre-filter + judgment), consolidates duplicates, triages to a tier, then dispatches and supervises many headless ship-task runs. Ships with a localhost control-panel daemon (`managerd.py`) — live task board, the source doc with per-finding progress, a suggestions inbox for the few questions only you can answer, and per-branch "Test this" buttons. |
| `skills/ship-task` | **The hands.** A "send a task, walk away" pipeline for one feature/fix: risk-aware routing (trivial → direct edit; real features → blind spec-only test agent, separate implement agent, hard verify gate), all inside an isolated git worktree, ending at a pushed branch + open PR. It never merges. |
| `skills/feature-tester` | **The independent verifier.** After the build is green, a fresh agent that never saw the implementation writes ONE focused integration test of the key behavior; the builder fixes until it passes. A recorded verdict gates the commit. |

The behavioral core: **autonomous by default, investigate before asking** (only
genuine preference questions reach you, each with a recommendation), **never
re-dispatch shipped work**, **stop at PR-ready, never merge**, and **report what
couldn't be done** instead of guessing.

## Install (one line)

Paste this into Claude Code:

```
set up https://raw.githubusercontent.com/Henry-Shan/agent-warehouse/main/install.md
```

The agent copies the three skills into `~/.claude/skills/`, checks prerequisites
(python3 ≥ 3.9, git, `gh` auth, the `claude` CLI; poppler only for PDF ingest),
**offers to install anything missing**, interviews you for your project's
settings, and writes `config.json`.

Also works with [`npx skills add Henry-Shan/agent-warehouse`](https://github.com/vercel-labs/skills)
thanks to the standard `skills/<name>/SKILL.md` layout (then copy
`config.example.json` → `config.json` yourself).

## Update

Paste the **same install line again** — it's idempotent. The installer keeps a
git clone at `~/.claude/agent-warehouse` with each skill symlinked into
`~/.claude/skills/`, so an update is a `git pull` (your `config.json`, queue DB,
and run history are gitignored and survive), and any skill added to this repo
later gets linked automatically. Manual equivalent:

```bash
git -C ~/.claude/agent-warehouse pull
```

## Configure

One file: `skills/manager/config.json` (gitignored; `config.example.json` is the
committed template):

```json
{
  "repo": "/abs/path/to/target/repo",
  "base_branch": "main",
  "worktree_dir": "~/.manager-worktrees",
  "archive_dir": "~/.manager-archive",
  "panel_port": 4317,
  "dispatch_cap": 2,
  "setup_command": "npm install && npm run build",
  "dev_command": "npm run dev",
  "dev_port": 3000,
  "verify_command": "npm run lint && npm test",
  "env_files": [],
  "risk_keywords_extra": [],
  "memory_mcp": null
}
```

Env vars (`MANAGER_REPO`, `MANAGER_PORT`, `MANAGER_CAP`, `MANAGER_BASE_BRANCH`,
…) override the file. Optionally fill in
`skills/ship-task/BUILDER_PLAYBOOK.md` with your repo's architecture + footguns —
it's the orientation doc every implement agent reads first.

## Use

```
cd /your/repo && claude

/manager https://notion.so/your-qa-page        # Notion (needs your Notion connector)
/manager ~/Desktop/qa-findings.pdf             # PDF + screenshots (poppler for the board view)
/manager "fix the 403 on upload; add CSV export; investigate slow search"

/ship-task "add a CSV export button to the items table"   # single task, no orchestration
```

The control panel opens at `http://127.0.0.1:4317` — watch the batch run, answer
the occasional preference question, click "Test this branch" to boot any run's
worktree, and merge finished PRs from GitHub.

## ⚠ Security — read before first use

**The manager dispatches autonomous agents with permission checks disabled.**
`skills/manager/dispatch.sh` launches headless `claude` runs with
`--dangerously-skip-permissions` (a headless run would otherwise stall on its
first tool prompt). Those agents can **edit files, run commands, push branches,
and open PRs in the configured repo** — treat a batch like giving a contractor
push access to feature branches.

What bounds them:

- **Never merge.** Every run stops at PR-ready; merging is always a human action
  from GitHub. This boundary is enforced in the skill instructions *and* the
  dispatch prompt, and there is no merge code anywhere in the package.
- Never push to the base branch — the only path in is a merged PR.
- All edits happen in isolated worktrees under `worktree_dir`, never your checkout.
- A hard verify gate (`verify_command`) + an independent feature test gate must
  pass before anything is committed.
- Tighter posture available: `MANAGER_PERMS="--permission-mode acceptEdits"`.

**Read `skills/manager/dispatch.sh` before your first batch** — it's ~150 lines
and it is exactly what runs on your machine.

## Screenshots

_(coming soon — the review board with a QA doc + live per-finding progress, the
suggestions inbox, and a PR-ready card with its manual-test guide)_

## Requirements

- macOS or Linux, `python3` ≥ 3.9 (stdlib only, no pip installs — **macOS's
  built-in Python is enough**), `git`, [`gh`](https://cli.github.com)
  (authenticated), the `claude` CLI.
- Optional: poppler (`pdftoppm`) for PDF ingest; a Notion connector in Claude
  Code for Notion ingest; a memory MCP server if you set `memory_mcp`.