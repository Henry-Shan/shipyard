# manager — the agent that runs /ship-task for you

The brain above `/ship-task`. You hand it a messy work source (a Notion page, a
QA PDF + its screenshots, a folder, or a typed list); it grounds each item in
the repo and the live base branch, drops the stuff that's **already shipped**,
consolidates duplicates, triages what's left to a `/ship-task` tier, and puts it
in a queue you can watch and steer from a browser. It dispatches several
`/ship-task` runs at once and surfaces any questions they raise in one
**suggestions inbox** where you reply — your reply is relayed back into the
running session (because `AskUserQuestion` can't work headless).

## Run it

Driven by the agent (open Claude Code in your target repo):

```
/manager ~/Desktop/qa-findings.pdf
```

Or by hand:

```bash
cd ~/.claude/skills/manager
python3 managerd.py init
python3 managerd.py serve     # → http://127.0.0.1:<panel_port from config.json>
```

Open the URL. Queued tasks dispatch automatically (up to `dispatch_cap`).
Parked runs appear in the inbox; answer them there.

## Pieces

| File | Role |
|------|------|
| `SKILL.md` | the agent's reasoning: ingest → ground → dedup → consolidate → triage → enqueue → dispatch → relay |
| `panel-up.sh` | ALL of Phase 0 as one command: init → restart-first daemon → wait → open tab → start the live feed → print PANEL_URL/CONFIG_OK/REPO/… (~0.6s) |
| `install-gh.sh` | no-sudo GitHub CLI install (official release binary → `~/.local/bin`) for PR mode |
| `skill_config.py` | loads `config.json` (the one source of project truth) + env overrides |
| `manager_core.py` | SQLite queue + suggestions inbox + relay state (the source of truth) |
| `managerd.py` | HTTP daemon: serves the panel, the JSON API, the dispatcher loop, and the relay |
| `dispatch.sh` | launches one headless `/ship-task` run with the relay preamble injected |
| `auto-manager.sh` | launches one headless `/manager` ingest run — triggered by the daemon itself when the panel's task-doc question is answered directly on the website, no terminal needed |
| `dedup_index.py` | deterministic already-shipped pre-filter (git log + archive) |
| `archive.py` | persists each finished run (log/diff/summary) into `archive_dir` |
| `notion_ingest.py` | optional Notion-page ingest (needs the user's Notion connector) |
| `ui/review.html` · `ui/index.html` | the localhost control panel (doc + tasks · classic queue view) |

## Config

`config.json` (copy `config.example.json`; written by the installer) — repo path,
base branch, worktree/archive dirs, panel port, dispatch cap, setup/dev/verify
commands, extra risk keywords, optional memory MCP. Env vars still override:
`MANAGER_REPO` · `MANAGER_PORT` · `MANAGER_CAP` · `MANAGER_BASE_BRANCH` · … plus
`MANAGER_PERMS` (default `--dangerously-skip-permissions` for unattended runs —
see the security note in the repo README) and `MANAGER_MODEL` (optional model
override for dispatched runs).

## Multiple projects

One daemon, one port, any number of projects — click the **Review** title
(top left of the panel) to go to the projects home page, see all of them and
switch, or add a new one (an existing repo, or a plain folder to create one
at). This is purely a panel/daemon action; no `/manager` session is needed to
add or switch projects, only to actually run a batch inside one. Each project
gets its own state under `projects/<slug>/` (`config.json` + `manager.db`);
`gh_available` and `panel_port` stay in the top-level `config.json` since
those are machine-level facts, not per-project ones. `projects.json` is the
registry. An existing single-project install migrates automatically the first
time the daemon boots with this version — the original `config.json`/
`manager.db` are copied, never moved, so nothing is lost even if the migration
is somehow re-run.

## Answering from the website alone (no terminal)

Each project's board proactively asks its task-doc question (Notion URL, PDF,
markdown path, or typed tasks) whenever it has nothing queued and nothing
already pending — you don't have to have run `/manager` yourself for that
question to be there. **Answering it directly on the panel is enough to
actually start the batch:** the daemon launches a real, unattended `/manager`
session itself (`auto-manager.sh`, same `--dangerously-skip-permissions`
posture as every dispatched `/ship-task` run — see the security note in the
repo README) with your answer as its work source. Progress shows up in the
same live feed a terminal-launched `/manager` run would produce.

Single-flight: only one such auto-launched ingest run goes at a time per
project. Answering "+ New task doc" again while one is still busy just queues
that answer — the next time the board polls after the first run finishes, it
notices the still-unprocessed answer and launches it automatically; nothing
is lost, and nothing double-launches.

## API (for the agent / scripts)

- `POST /api/tasks` `{title,description,source,tier,evidence,open_question}` → enqueue
- `POST /api/tasks/<id>/status` `{status}` · `POST /api/tasks/<id>/update` `{...}`
- `POST /api/ask` `{session_id,question,options,context,task_id}` — a run relays a question (parks the task)
- `GET  /api/answer?session_id=…` — the run polls for the human's reply (delivered once)
- `POST /api/questions/<id>/answer` `{answer}` — the human replies from the panel
- `GET  /api/state` — everything the UI needs (tasks, counts, inbox, activity)
- `GET  /api/tasks/<id>/changes` — the task's captured diff (files/added/deleted/full diff text)

### Attaching a real "after" screenshot

The "See the effect" before/after view shows the finding's original screenshots
as "before" evidence automatically. It never fabricates an "after" screenshot —
dispatched runs can't safely boot a browser mid-batch (many run in parallel and
would fight over ports), so there's no automatic capture. To attach a real one:
1. Click **"Test this branch"** on a PR-ready task (boots that one worktree's
   dev server — the panel already serializes this to one at a time).
2. Ask Claude to open the live URL with `/browse`, drive to the fixed behavior,
   and screenshot it.
3. Attach the screenshot: `POST /api/tasks/<id>/update {"after_evidence": ["<abs path to the screenshot>"]}`.

It then appears in "See the effect" right alongside the actual test output,
as genuine first-hand proof — never a placeholder.

## Guarantees it keeps from ship-task

Every dispatched run still goes branch → PR to the base branch → green CI →
**human approval** → merge. The relay only moves that approval from the terminal
into the panel; the manager never merges, and never writes product code itself.
