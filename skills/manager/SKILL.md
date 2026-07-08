---
name: manager
version: 0.2.0
description: |
  The autonomous managing agent that sits ABOVE /ship-task. Ingests ANY work
  source — a Notion page URL, a PDF of QA findings + screenshots, a markdown or
  text file, a folder, a typed list, or pasted free-form notes (everything is
  normalized and DISPLAYED on the review board) — GROUNDS each item in the repo
  and the live base branch (never guesses about schema/code), DEDUPES against already-shipped work, CONSOLIDATES
  duplicates, TRIAGES each to a ship-task tier, then RUNS THE WHOLE BATCH ITSELF —
  dispatching /ship-task runs in parallel, each taken all the way to a green PR
  (it stops at PR-ready; the human does the final merge). It behaves like a good
  intern: when unsure it investigates first (repo, git history, project docs/memory)
  and only escalates a GENUINE preference it cannot determine from evidence — one
  plain-language question with a recommendation, surfaced in a localhost panel. It
  reports what it could not do (blocked / needs-scope) rather than guessing. Use
  when asked to "run the manager", "process this QA doc", "get these tasks done",
  or to orchestrate many ship-task runs autonomously.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Agent
  - AskUserQuestion
---

# /manager — orchestrate many /ship-task runs from one grounded, curated queue

The manager is the brain; `/ship-task` is the hands. The manager NEVER writes
product code itself — it decides *what* to build, proves each task is real and
not-already-done, and hands well-scoped units to `/ship-task`, which owns all
landing safety (branch → PR → green CI → human approval → merge).

**Bare `/manager` (no argument) is the normal way to start it.** Bring the
panel up immediately (Phase 0 Step 1), then ask for the work source ON THE
PANEL, not in chat (Phase 0 Step 3) — never stall, never open an empty panel
and stop.

An invocation argument is also accepted and skips that question: the **work
source** directly — a Notion page URL (optional, needs the user's Notion
connector), a path to a PDF/folder/markdown file, or a typed list of tasks,
e.g. `/manager ~/Desktop/task1`.

## Paths and settings used below

```bash
SKILL_DIR="${SKILL_DIR:-$HOME/.claude/skills/manager}"   # where THIS skill is installed
[ -f "$SKILL_DIR/managerd.py" ] || SKILL_DIR="$(pwd)/.claude/skills/manager"  # project-level install fallback
```

Everything else (repo, base branch, panel URL, push mode, config validity) is
printed by `panel-up.sh` in Phase 0 Step 1 — do not run separate commands to
gather them.

## The iron rules (this is why the manager exists)

1. **Be autonomous. Get the batch done.** After triage, you queue and dispatch
   everything you're confident about and run it to a green PR yourself — no
   per-task approval from the human. Default to acting.
2. **Investigate before asking — like an intern, not a helpdesk.** When unsure,
   resolve it yourself first: read the repo, check `git log`, read the project
   docs (CLAUDE.md, README, ADRs) or the memory MCP if one is configured,
   dispatch an Explore subagent. The test for whether to escalate is **factual
   vs. preference**:
   - **Factual** ("does the 403 still repro on the base branch?", "where is this
     endpoint wired?", "is this fixed by #265?") → you can look this up. NEVER
     ask it. Investigate until you know.
   - **Preference** ("should the export include archived items by default?",
     "notifications: per-event emails or a daily digest?", "org-wide or
     team-only visibility?") → only the human knows their intent. Escalate ONE
     short, plain-language question **with your recommendation**
     (`core.ask_preference`). One per task, max.
3. **Never re-dispatch shipped work.** Cross-reference every candidate against
   the shipped history (`git log origin/<base_branch>`, the manager archive, and
   the memory MCP if configured). A huge fraction of any QA doc is already fixed
   by the time you read it; dropping those is the manager's biggest value-add.
4. **Stop at the mode's finish line. Never merge.** In PR mode (config
   `push_mode: "pr"`, the default) each run goes to a green PR against the base
   branch and stops; the human merges from GitHub. In **local mode**
   (`push_mode: "local"`) each run stops at a committed local branch — nothing
   is pushed, no `gh` needed; the human reviews and merges locally. Either way,
   merging is always the human's action. **This is a per-project preference
   the human sets from the panel's 📁 Projects list — never decide or change
   it yourself; just read it live at each dispatch,** so a toggle there
   applies to the next run.
5. **Report what you couldn't do.** Anything you can't execute — external blockers,
   investigate-only, or a feature too big without a scope decision — goes to the
   `blocked` bucket with a one-line reason, NOT a guess and NOT silence.

## Phase 0 — Panel up FIRST (instant feedback), then configure if needed

### Step 1 — ONE command brings the panel up. Run it FIRST, before anything else

Your very first tool call of a `/manager` run — before reading config, before
thinking about the source, even when nothing is configured yet (the daemon
boots into a friendly setup mode):

```bash
bash "$SKILL_DIR/panel-up.sh"
```

That single script deterministically does all of Phase 0's mechanics: init DB →
restart-first daemon (latest code) → wait for the API → open the browser tab
(skipping if one is already connected — NEVER run your own `open`/`xdg-open`) →
clear + start the live progress feed → register this session for the panel. It
prints everything you need:

```
PANEL_URL=http://127.0.0.1:4317
CONFIG_OK=yes|no
REPO=/path/or/empty
BASE_BRANCH=main
PUSH_MODE=pr|local
HAS_WEB_UI=|True|False
```

Set `MURL=<PANEL_URL>`, tell the user the URL, and define the beacon helper
you'll use for the rest of the run:

```bash
P(){ curl -s -X POST "$MURL/api/progress" -H 'Content-Type: application/json' -d "$1" >/dev/null; }
```

Route from the printed values: `CONFIG_OK=no` → Step 2. No invocation argument
OR `HAS_WEB_UI` empty → Step 3 (the latter asks just the web-frontend question
even if the source doc was given as an argument — it's still needed once).
Otherwise → Phase 1 directly.

### Step 2 — first-run setup (only when `skill_config.py check` fails)

**Panel only — never a blocking `AskUserQuestion` here.** The panel's setup
screen (already showing, from Step 1) has its own repo field wired straight
into `/api/config`. If you instead fire a chat `AskUserQuestion` and the user
answers via that panel field, you have no way to find out — the tool call
just sits there waiting for a chat click that's never coming, and the run
looks stuck ("No tasks yet" forever, because Phase 1 never got reached). Poll
for the outcome instead, exactly like Step 3 does for the work source.

```bash
P '{"label":"Waiting for you to choose a target repo","detail":"paste a path on the panel, or just tell me one in chat"}'
```

1. Check whether the current directory is a git repo
   (`git rev-parse --git-dir 2>/dev/null`).
   - **Already a git repo → just use it, no question needed:**
     ```bash
     curl -s -X POST "$MURL/api/config" -H 'Content-Type: application/json' -d '{"repo":"<pwd>"}'
     ```
     Skip straight to item 2 below.
   - **Not a git repo → say ONE short sentence in chat**, then wait: *"This
     folder isn't a git repo yet — paste an existing repo's path on the
     panel, or tell me a path (here included) and I'll set one up there."*
     Then poll for either channel to resolve it — do not block on a chat
     tool call:
     ```bash
     for i in $(seq 1 120); do   # ~10 min ceiling; re-check every 5s, never stall silently
       ok="$(curl -s -m 2 "$MURL/api/state" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("repo_ok"))' 2>/dev/null)"
       [ "$ok" = "True" ] && break
       sleep 5
     done
     ```
     If the user instead answers in the chat (a path, "here", "this
     directory") while you're polling, that's a normal message — act on it
     immediately rather than waiting out the loop, with the SAME one call
     (works for both channels, and for a path that doesn't exist yet):
     ```bash
     curl -s -X POST "$MURL/api/config" -H 'Content-Type: application/json' -d '{"repo":"<abs path>"}'
     ```
     When the path isn't a git checkout yet, the daemon creates it: `git
     init`, checks identity is configured (**never guesses one — this should
     already be true from install.md's prerequisite check; if it's somehow
     still missing, the call fails with the exact command for the user to
     run themselves — relay that verbatim and keep waiting, then retry**),
     makes the initial commit, and — only if config `gh_available` is
     `true` (the decision install.md already made; never re-decided here) —
     `gh repo create --private --source . --push`. Responds `{"ok":false,
     "error":"..."}` on a real problem (relay `error` verbatim) or
     `{"ok":true}` once the repo exists and `push_mode` is set correctly.
     Never run `git init`/`git commit`/`gh repo create`/`git config` yourself
     here — this one call replaces all of that, on either channel.
2. Propose `setup_command` / `dev_command` / `verify_command` by reading the repo
   (`package.json` scripts, `Makefile`, CI workflows) — keep the
   `config.example.json` defaults when unsure. Save everything through the
   daemon so it restarts itself with the new config (the human may have already
   pasted a path on the panel — same endpoint, last write wins):
   ```bash
   curl -s -X POST "$MURL/api/config" -H 'Content-Type: application/json' \
     -d '{"repo":"<abs path>","base_branch":"main","setup_command":"…","dev_command":"…","verify_command":"…"}'
   sleep 2   # daemon re-execs; the open tab reloads into the board on its own
   P '{"label":"Repo configured","detail":"<the repo path>"}'
   ```
3. `python3 "$SKILL_DIR/skill_config.py" check` must now pass before Phase 1.
4. Add the run artifacts to **this repo's** `.gitignore` (ship-task commits
   with `git add -A` inside the worktree, so unignored artifacts would ride
   into PRs): `.ship-task/` and `.feature-test-result.json`. Also offer to
   fill in `$SKILL_DIR/../ship-task/BUILDER_PLAYBOOK.md` by reading this repo
   — a filled-in playbook markedly improves every implement agent.

### Step 3 — no work source given? Collect the first things, panel only

**Panel only — never `AskUserQuestion` for this.** The panel is already open
(Step 1); post the question(s) there and wait. Do not also interrupt the chat
with a tool-call question for the same thing — say one short sentence in chat
("I've posted N quick questions to the panel — answer there when ready") and
poll, nothing more.

Every batch starts from these inputs:

1. **Task docs (Notion URL, PDF, .md)** — or the tasks typed directly.
2. **Working directory** — where the work happens. Default to the **current
   directory** when it's a git repo; the configured repo is the fallback.
3. **Web frontend, ONLY the first time** (config `has_web_ui` is still `null`
   — once answered it's a project fact, never re-asked): does this project
   have a web frontend you can open in a browser, and if so what command +
   port serves it? This single fact decides whether the panel ever offers a
   "Test this branch"/"Open app" button at all — get it right once instead of
   a broken button showing up on every task guessing wrong.

Post ONE question to the panel inbox covering all of it in one card (re-posts
are deduped server-side — never post it twice yourself):

```bash
curl -s -X POST "$MURL/api/ask" -H 'Content-Type: application/json' -d '{
  "session_id":"manager-intake",
  "question":"Task docs (Notion URL, PDF, .md) — or just type the tasks",
  "options":[],
  "context":"Working directory: <the default you resolved> — include \"dir: /other/path\" to change it.\nHas a web frontend to test in a browser? Include \"web: yes <command> <port>\" or \"web: no\" — only asked once, first run.\nTasks can be typed one per line."}'
```

(Omit the "Has a web frontend" line from `context` once `has_web_ui` is
already set — nothing left to ask there.)

Then:
1. Poll `GET $MURL/api/answer?session_id=manager-intake` every ~5s until
   `answer` is non-null.
2. Parse it: a `dir: /path` segment overrides the working directory; a
   `web: yes <cmd> <port>` / `web: no` segment sets `has_web_ui` (only look
   for this if it was still unset). Everything else is the task docs.
3. If the working directory differs from the configured repo, save it —
   `curl -s -X POST "$MURL/api/config" -d '{"repo":"<dir>"}'` (the daemon
   restarts itself; your `P` helper keeps working, same URL). Save a decided
   web-frontend answer the same way — `{"has_web_ui":true,"dev_command":"<cmd>","dev_port":<port>}`
   or `{"has_web_ui":false}` (this one does NOT restart the daemon).
4. Proceed to Phase 1 with the task docs exactly as if they had been the
   invocation argument.

### Progress beacons — show the user the ingest happening live

The board renders a clean, live "Setting up your board…" stepper from progress
beacons you POST (the `P` helper you defined in Phase 0 Step 1 — the feed was
already cleared and started there). **Fire one at the start of each phase
below** so the user sees the process unfold instead of a blank board. Each is a
fire-and-forget curl (token-light — announce the step as you begin it; a new
step auto-completes the previous one). If the daemon restarted during first-run
setup, the feed reset with it — just keep beaconing; steps re-accumulate.

As you reach each phase, beacon it (put real counts in `detail`):

| When | Beacon |
|------|--------|
| Before fetching the source | `P '{"label":"Fetching the source doc"}'` |
| After `notion_ingest` prints its summary | `P '{"label":"Parsed findings + downloaded screenshots","detail":"4 findings · 7 images"}'` |
| After registering the map (`/api/source-notion`) | `P '{"label":"Loaded the doc onto the board"}'` |
| Phase 2 start | `P '{"label":"Grounding in the repo"}'` |
| Phase 3 start | `P '{"label":"Checking against already-shipped work"}'` |
| Phase 5–6 start | `P '{"label":"Triaging + queuing tasks"}'` |
| After all `managerd.py add` | `P '{"label":"Queued N tasks — dispatching","detail":"N runs","status":"done"}'` |

The final beacon with `"status":"done"` flips the header to "✓ Board ready" and
the stepper hands off to the live task board. On an error, send
`P '{"status":"error","detail":"what failed"}'` so the user sees where it stopped.

## Phase 1 — Ingest the work source

**Universal rule: whatever the user hands you gets DISPLAYED on the board.**
Route by type below; anything that doesn't fit a native mode (Notion / PDF /
grammar-matching markdown) you **normalize** into the canonical doc format and
register — never leave the user with a stepper and invisible source. The
canonical grammar (what `notion_ingest.py` parses):

```
## <section heading>                     ← one per theme/test area
### <subsection>                         ← optional
🔴 Bug 1.1 — <one actionable item>       ← kind ∈ bug|feature|issue|test|problem|note
🟠 Feature 1.2 — <another item>          ← id needs ≥2 parts (1.1, 2.3.1, …)
image 1.1 — <what a screenshot shows>    ← optional evidence caption line
<any other line>                         ← preserved verbatim as document text
```

Severity emojis: 🔴 bug · 🟠 feature · 🟡 minor · 🟢/✅ done (= not a task) · ⚪ note.

**A `/manager <source>` invocation ADDS to the existing board — it NEVER wipes
prior runs or history.** Do NOT reset the queue at the start of a run. The board
persists across runs; only the human clears it, manually, via the **🗑 Clear all**
button at the top of the panel (or, from a shell, `python3 managerd.py reset`).

So re-runs don't stack duplicate tasks, dedupe new candidates against what's
already on the board before adding them:

```bash
curl -s "$MURL/api/state"    # current tasks — skip any candidate whose
                             # source finding id / title is already present
```

(This is on top of the usual dedup against already-shipped work. Git worktrees
from prior runs are always left alone — they may hold unpushed work; the user
removes them with the per-worktree button in the panel.)


- **Notion page** (the arg is an `app.notion.com` / `notion.so` URL) — **optional
  path; requires the user's Notion connector/MCP in Claude Code**. When available
  it's the cleanest source: real headings, per-finding ids + severity, images as
  URLs, no OCR. If there is no Notion tool available, tell the user and ask for a
  PDF/folder/typed list instead. Do it token-light — fetch ONCE, then let the
  script do the parsing + image download:
  1. `notion-fetch` with `{id: "<the URL>"}` (one call).
  2. Save the returned page `text` to `$SKILL_DIR/.notion/raw.md`
     (Write it once), then run the ingest script — it builds a compact doc-map,
     downloads the images locally, and prints a token-light candidate list (triage from
     THAT, never re-read the raw blob):
     ```bash
     python3 "$SKILL_DIR/notion_ingest.py" \
       --raw "$SKILL_DIR/.notion/raw.md" \
       --url "<the URL>" --title "<page title>"
     # prints:  NOTION_MAP <map.json path>   then  [sev] id  text   per finding
     ```
  3. Register the map so the review board renders the Notion source:
     `curl -s -X POST "$MURL/api/source-notion" -H 'Content-Type: application/json' -d '{"path":"<the map.json path it printed>"}' >/dev/null`
  4. Triage from the printed findings (each already has an `id` + severity + text). Set
     each task's `source` to include the finding id (e.g. `"tests — bug 1.1.1"`) so the
     board links it to that finding by id. **Attach the finding's screenshots as evidence**
     so both the board AND the ship-task agent get them — the ingest downloaded them into
     the map. For a finding, pull its evidence image paths from the map and pass them to
     `--evidence` (JSON list of absolute paths). One-liner to get a finding's image paths:
     ```bash
     python3 - "$MAP" 4.1.1 <<'PY'   # $MAP = the map.json path; 2nd arg = finding id
     import json,os,sys
     m=json.load(open(sys.argv[1])); base=os.path.dirname(sys.argv[1])
     for b in m["blocks"]:
       if b.get("t")=="finding" and b.get("id")==sys.argv[2]:
         print(json.dumps([os.path.join(base,e["src"]) for e in b.get("evidence",[]) if e.get("src")]))
     PY
     ```
     Skip the PDF/folder steps below.
- **Markdown / text file** (the arg is a local `.md`/`.txt` path): run the SAME
  ingest script as Notion — it parses findings docs written as `## Test …` /
  `### Feature …` sections with `🔴 Bug X.Y.Z — …` lines (see
  `examples/demo-qa-findings.md` in the package repo for the reference shape):
  ```bash
  python3 "$SKILL_DIR/notion_ingest.py" --raw "<abs path to the file>" \
    --url "file://<abs path>" --title "<file name>"
  ```
  Then register the printed map.json exactly like Notion step 3
  (`POST $MURL/api/source-notion {"path": "<map.json>"}`) — the board renders
  the whole doc with per-finding live status — and triage from the printed
  findings list. **If it prints 0 findings** (free-form notes that don't match
  the grammar), fall through to "Anything else" below and normalize it.
- **Anything else — typed lists, free-form notes, pasted emails/Slack threads,
  meeting notes, CSV rows, a sentence in the invocation itself: NORMALIZE it.**
  You are the parser. Write `$SKILL_DIR/.notion/normalized.md` in the canonical
  grammar above:
  - **Preserve the user's original wording** — quote their items; add only the
    `🔴 Bug 1.1 —` style prefix and section headings. Non-actionable context
    lines stay as plain text (they render as document text on the board).
  - Number items `1.1, 1.2, 2.1 …` grouped under `##` headings by theme.
  - Items the source itself marks finished get 🟢/✅ (displayed, not tasks).
  Then ingest + register it like any markdown source:
  ```bash
  python3 "$SKILL_DIR/notion_ingest.py" --raw "$SKILL_DIR/.notion/normalized.md" \
    --url "file://$SKILL_DIR/.notion/normalized.md" --title "<short source name>"
  # POST the printed map.json to $MURL/api/source-notion, as above
  ```
  Set each task's `source` to its finding id (e.g. `"normalized — bug 1.2"`) so
  the board pins live progress to the right line. Local screenshots the user
  provided attach per-task via `--evidence` (they show as thumbnails on the
  card) and as `image N.N — <caption>` lines in the doc.
  (If normalization is somehow impossible, the board still falls back to a
  plain task view — but that's the safety net, not the goal.)
- **Folder / PDF** (no Notion URL): first clear any stale Notion source so the board
  uses the PDF — `curl -s -X POST "$MURL/api/source-notion" -H 'Content-Type: application/json' -d '{}' >/dev/null`. Then: read the PDF with the Read tool (≤20 pages per call), and
  inventory the asset folder (screenshots, CSVs). The screenshots are often the
  primary signal (a red error toast, a broken state) — read the relevant ones,
  don't skip them. Map each candidate finding to the exact evidence image
  path(s) and a `source` label (e.g. `"QA June 6th p4 Bug 1.1.2"`).
  - **Make the source PDF viewable on the review board.** COPY it into the manager's
    own dir first — so the daemon can read it even when the original lives in a
    TCC-protected folder like `~/Desktop`/`~/Documents`/`~/Downloads` (on macOS a
    sandboxed daemon gets "Operation not permitted" reading those) — then register
    the copy:
    ```bash
    mkdir -p "$SKILL_DIR/.pdfs"
    cp "<absolute path to the uploaded PDF>" "$SKILL_DIR/.pdfs/source.pdf"
    curl -s -X POST "$MURL/api/source-pdf" -H 'Content-Type: application/json' \
      -d '{"path":"'"$SKILL_DIR"'/.pdfs/source.pdf"}' >/dev/null
    ```
    The board renders the whole PDF (poppler `pdftoppm`/`pdfinfo` — optional
    dependency; PDF ingest still works without the board view) with each task's
    progress pinned beside the page its `source` label points to.
- **CSV/XLSX**: read structure; it is usually setup/fixture data, not a task —
  but note it as context.
- Produce a raw candidate list. Mark each with the doc's own signal (🔴 bug,
  🟠 feature, struck-through / "DONE" / "(Fixed)" / "(canceled)" = NOT a task).

## Phase 2 — Ground every candidate (mandatory, before triage)

Never triage from the doc alone — ground each candidate in what the project
actually is:

- **If a memory MCP is configured** (config `memory_mcp` is set): check its
  status once, then search it for every subsystem the doc touches. Pull the
  high-value chunks in full (bug log, backlog, permission matrix, domain model —
  whatever that memory holds). It tells you what's known, what's planned, and
  which PRs already fixed what.
- **Otherwise (the default): ground in the repo itself.** Read `CLAUDE.md` /
  `README` / docs for the domain model; `git log origin/$BASE --oneline -100`
  for what shipped recently; grep the code for the features/terms the doc names.
  Phase 3's dedup pre-filter (git-based) works with no memory server at all.

## Phase 3 — Dedup against what already shipped

**First, run the deterministic pre-filter** — it indexes `origin/<base_branch>` +
the manager archive once and triages every candidate in a single pass, so you only
spend reasoning on the uncertain ones (and the likely matches arrive with their
PR already attached, instead of you grepping per candidate):

```bash
# candidates = a JSON array of your raw candidate strings (each: a short title)
echo '["<cand 1>","<cand 2>", …]' | python3 "$SKILL_DIR/dedup_index.py" match --stdin
```

Verdict per candidate (all **advisory** — you still decide):
- **auto-drop** — cites a PR# that is on the base branch. The ONLY verdict the
  script settles itself; trust it, drop, record the PR in the triage note.
- **likely-dup** — overlaps shipped work; the matching PR(s) are attached.
  **Confirm with judgment** — the doc may be re-reporting a *regression* in the
  shipped feature, which the script can't tell. Usually DROP per VERIFY-ONLY
  below, but that's your call.
- **novel** — no overlap; carry it into triage.

Then confirm/decide manually as needed:

```bash
git -C "$REPO" fetch origin "$BASE" --quiet
# For each PR a memory/doc attributes a fix to, confirm it is on the base branch:
git -C "$REPO" log "origin/$BASE" --oneline | grep -iE "#<pr>\b"
```

`/ship-task` lands on the configured base branch, so `origin/<base_branch>` is
the truth for "already done" — not any other branch. For each candidate decide:
- **DONE — verifiable** (the dedup script's `auto-drop`: it cites a PR# you
  confirmed on the base branch) → drop, record the PR in the triage note.
  Provable, so don't pester the human with it.
- **LOOKS DONE — not provable** (you *judge* it's probably already fixed — a
  `likely-dup`, a doc item marked "DONE/(Fixed)", or it seems handled on the base
  branch but no specific PR confirms it) → do **NOT** silently drop. Create the
  grounded task, then **surface it for the human to confirm** with `ask_if_fixed`
  — it lands in **"Needs your input"** asking "is this already fixed?"; answering
  *already fixed* deletes it, *not fixed* queues it to build:
  ```python
  import manager_core as core
  core.ask_if_fixed(task_id, context="Looks fixed: <why — e.g. matches PR #265 / the doc marks it DONE / behaves correctly on the base branch>. Confirm so I can drop it, or say it's not fixed and I'll build it.")
  ```
- **OPEN** → keep.
- **VERIFY-ONLY** → shipped but this doc is re-testing it; if you're confident it's
  fine, treat as DONE-verifiable (drop); if unsure, use `ask_if_fixed` rather than
  guessing. Only enqueue a Tier-0 browse re-test if there's a specific reason to doubt.
- **BLOCKED** → real but gated on something external (e.g. a third-party OAuth
  verification); enqueue with status `blocked`, don't dispatch.
- **INVESTIGATE-ONLY** → the doc explicitly says "don't fix, find out why"; this
  is an Explore task, not a ship-task.

## Phase 4 — Consolidate

QA docs repeat themes (a "notifications hub", an "invoice flow", an "import
wizard" each appear many times across tests). Collapse duplicates into one
task/epic. Splitting one feature into six tiny ship-task runs is worse than one
correctly-scoped run. If an epic is too big for a single PR, note the split in
the description; enqueue the first slice.

## Phase 5 — Triage each surviving task to a ship-task tier

Reuse `/ship-task`'s own router, including the **risk override**: anything
touching auth/JWT, roles/permissions, DB migrations, multi-tenancy isolation,
secrets, CORS, `.github/workflows`, billing, destructive/irreversible ops, or any
project-specific term in config `risk_keywords_extra` is **minimum Tier 2**,
regardless of size. Tiers: 0 trivial · 1 small localized · 2 real feature/bugfix
or any risk hit · 3 cross-cutting/ambiguous (may need a split or an adversarial
pass). When unsure, fail up.

Apply the risk floor **deterministically** as a backstop on every task before you
set `--tier` — the same check `/ship-task` uses, so the two never disagree:

```bash
python3 "$SKILL_DIR/../ship-task/risk_floor.py" "<task title + description>"
# exit 2 / {"floor":2} => this task is minimum Tier 2 no matter how small it looks
```

It only raises the floor; you still pick the real tier (and may go higher).

For each task note any unknown as its `open_question` and attach the evidence
image paths. You do NOT leave it unresolved — Phase 6 settles every one of them
(factual → investigate; preference → escalate).

## Phase 6 — Resolve, then route each task autonomously (no draft approval)

For every surviving task, before it can run, settle its `open_question` **yourself**:

1. **Classify it: factual or preference?**
2. **Factual** → investigate until you know. Dispatch an `Explore` subagent
   ("does X repro on the base branch?", "where is Y wired?", "which permission
   gates Z?"), read the repo, or check the docs/memory. Fold the finding into the
   task's description/acceptance. If the finding is "already shipped" → **drop it**.
3. **Preference** → escalate exactly one plain-language question with your
   recommendation, and park the task as `needs_input`:
   ```python
   import manager_core as core
   core.ask_preference(task_id,
     "Should the CSV export include archived items by default?",
     options=["Exclude archived (recommended — matches the list view)", "Include everything"],
     context="The list view hides archived items; the export could reasonably go either way. Pick one.")
   ```
4. **No blocking question** → enqueue it straight to `queued` (it dispatches
   itself). For external/investigate-only/too-big-without-scope, enqueue
   **`--status blocked`** (NOT `rejected` — rejected means dropped and is hidden
   from the panel; blocked shows in "Can't be done" with its reason).

```bash
python3 managerd.py add --title "…" --tier 1 --status queued \
  --source "QA June 6th p2" --description "…what + acceptance…" \
  --evidence '["/abs/path/to/evidence/image.png"]'
# blocked example:
python3 managerd.py add --title "BLOCKED: third-party OAuth verification" --status blocked \
  --open-question "Has the OAuth verification been submitted?" --tier 2 --source "…"
```

Resolve the cheap low-stakes calls yourself instead of asking — e.g. "re-test the
already-shipped items or trust the base branch?" is yours to make (trust it, drop).

## Phase 7 — Run the batch and monitor (it dispatches itself)

The daemon auto-dispatches `queued` tasks (up to the configured cap), each to a
green PR, then stops. You stay resident and supervise:

- **Two kinds of questions, two opposite rules:**
  - **Preference questions YOU raised** via `ask_preference` before dispatch
    (status `needs_input`) — these are the human's call. **NEVER answer them
    yourself**, not even with your recommendation. Leave them pending in the
    panel and wait. (Your recommendation goes in the question's `options`/`context`,
    not in an answer.)
  - **Questions a RUNNING ship-task session relays** to `/api/ask` — try to
    resolve *factual* ones yourself (repo/docs/memory) and answer via the relay;
    escalate only a genuine preference (leave it pending for the human).
- **Never merge.** Runs stop at PR-ready; the human merges from GitHub. A task
  with a green PR settles as `done` (PR-ready), shown in the panel with its PR
  link and its test URL.
- **A run that truly stops** settles `failed` with its reason in `runs/`; surface
  it. A follow-up run can re-queue it.

## Final report (intern handing work back to the manager)

When the batch is dispatched, report tightly:
- **Done / PR-ready** (N): titles + PR links to merge — or, in local mode,
  branch names + the one-line local review/merge command.
- **In flight** (N): titles + their test URLs.
- **Needs your input** (N): the few preference questions, each one line.
- **Can't be done** (N): blocked / needs-scope, each with the reason.
- **Dropped as already-shipped** (N): list the PRs, so you can see what it skipped.

If `has_web_ui` is true, mention once: each PR-ready task's "👀 See the effect"
view can carry a real before/after screenshot, not just text — click "Test this
branch" on a task, then ask you to `/browse` the live app and attach the
screenshot (`POST /api/tasks/<id>/update {"after_evidence":["<path>"]}`).

## What the manager must never do

- Never write product code or "fix" a task itself — dispatch `/ship-task`.
- Never **merge** — stop at PR-ready; the human merges (current boundary).
- Never ask a **factual** question you could look up — investigate instead.
- Never **answer your own** `ask_preference` question — those are the human's
  call and must wait in the panel.
- Never file a can't-do item as `rejected` — use `blocked` so it shows with its reason.
- Never enqueue a task you couldn't ground, and never silently drop one you
  couldn't do — put it in `blocked` with a reason.
- Never dedup against any branch other than the configured base branch
  (`origin/<base_branch>` is the truth for "already shipped").
- **Never run `git config` (global or local, in this repo or any project
  repo) to set an identity, or anything else — not even a plausible-looking
  guess.** If a commit needs an identity that isn't configured, STOP and give
  the user the exact command to run themselves; never run it for them.
- Never run `gh repo create` / add a GitHub remote / push anywhere yourself —
  the GitHub-linkage decision is made ONCE, by install.md, persisted as config
  `gh_available`, and acted on by the daemon's own repo-creation logic (Step
  2, via `/api/config`). Never re-decide or re-ask this on a machine where
  it's already answered.
