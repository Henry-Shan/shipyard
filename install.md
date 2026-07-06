# Install the manager skill package

You are an AI coding agent installing this package for a user. Work through the
steps in order. Do not skip the prerequisite report or the config interview.
Report everything that's missing — never fail silently.

This package contains three Claude Code skills that work together:

- **manager** — orchestrates a batch of tasks: ingests a work source, dedupes
  against shipped work, triages, and dispatches parallel headless ship-task runs
  with a localhost control panel.
- **ship-task** — builds ONE feature/fix in its own git worktree and stops at a
  PR-ready branch.
- **feature-tester** — an independent post-build test agent ship-task invokes.

> ⚠ **Security — read this to the user before finishing.** The manager
> dispatches autonomous headless `claude` runs with
> `--dangerously-skip-permissions` (see `skills/manager/dispatch.sh`; override
> with `MANAGER_PERMS`). Those agents can edit files, run commands, push
> branches, and open PRs in the configured repo. Every run stops at PR-ready
> and **never merges** — merging is always the human's action. The user should
> read `dispatch.sh` before first use.

## Step 1 — Install OR update (this step is idempotent)

The repo is cloned once to a persistent location and each skill is **symlinked**
into `~/.claude/skills/` (the skills must stay siblings — the scripts resolve
each other by relative path). Updating is the same step re-run: the pull brings
new code and any newly added skills; user state (`config.json`, `manager.db`,
run history) is gitignored inside the clone and survives every pull.

```bash
WAREHOUSE=~/.claude/agent-warehouse
if [ -d "$WAREHOUSE/.git" ]; then
  git -C "$WAREHOUSE" log --oneline HEAD..origin/main 2>/dev/null | head  # show the user what's new
  git -C "$WAREHOUSE" pull --ff-only
else
  git clone https://github.com/Henry-Shan/agent-warehouse.git "$WAREHOUSE"
fi
mkdir -p ~/.claude/skills
for s in "$WAREHOUSE"/skills/*/; do
  name="$(basename "$s")"
  [ -e ~/.claude/skills/"$name" ] || ln -s "$s" ~/.claude/skills/"$name"
done
chmod +x "$WAREHOUSE"/skills/*/*.sh
ls -l ~/.claude/skills/    # report which skills are now linked (note any NEW ones to the user)
```

Handle these cases:
- **Old copy-based install** (`~/.claude/skills/manager` is a real directory,
  not a symlink): preserve the user's state first —
  `cp ~/.claude/skills/manager/config.json "$WAREHOUSE/skills/manager/config.json"`
  (plus `manager.db` and `runs/` if present) — then, with the user's OK, replace
  each real directory with the symlink.
- **A skill folder exists but is neither a symlink into the warehouse nor the
  old install**: name-collision with something else the user has — ask before
  touching it.
- **Update run** (clone already existed): after pulling, skip straight to
  Step 4's smoke checks, re-run `skill_config.py check`, tell the user what
  changed and whether any NEW skills were linked, and stop — no interview needed.

(If `git` is unavailable, fall back to the tarball:
`curl -L https://github.com/Henry-Shan/agent-warehouse/archive/refs/heads/main.tar.gz | tar xz`
into `$WAREHOUSE` — but note that updates then require re-downloading.)

## Step 2 — Check prerequisites, REPORT, then OFFER TO FIX

Run each check and collect the results into one clear report. Missing items are
**reported, not silently tolerated** — but don't stop at the report either:
propose the exact fix commands and, with the user's OK, run them yourself and
re-check. Only poppler is optional.

```bash
python3 -c 'import sys; assert sys.version_info >= (3,9), sys.version; print("python3 OK:", sys.version.split()[0])'
git --version
gh auth status          # must show a logged-in account — ship-task opens PRs with gh
claude --version        # the Claude Code CLI — dispatch.sh launches headless runs with it
pdftoppm -v             # poppler — OPTIONAL: only needed for PDF ingest / the panel's PDF view
```

| Check | Needed for | If missing |
|---|---|---|
| `python3` ≥ 3.9 | every script (stdlib only — **macOS's built-in Python qualifies**) | see fix flow below |
| `git` | worktrees, dedup, branches | install git (macOS: `xcode-select --install`) |
| `gh` + logged in | opening PRs — **only needed in PR mode**; in local mode (`push_mode: "local"`, nothing pushed) skip it entirely | `brew install gh` / `apt install gh`, then `gh auth login` |
| `claude` CLI | dispatching headless runs | install Claude Code |
| poppler (`pdftoppm`) | **optional** — PDF ingest + panel PDF view | `brew install poppler` / `apt install poppler-utils` |

**Fix flow** (after presenting the report). Never dump manual instructions and
stop — always end with ONE clickable question (AskUserQuestion) whose options
do the work:

1. **With a package manager** (`brew` on macOS, `apt`/`dnf` on Linux): assemble
   one fix plan for everything missing and offer it as the recommended option —
   e.g. "Install gh with brew and log you in? (Recommended)". On yes, run the
   installs and re-run the checks; show the updated table.
2. **Without a package manager — gh still installs with one click, no sudo.**
   The repo ships a deterministic installer that downloads the official gh
   release binary into `~/.local/bin` and wires PATH:
   ```bash
   bash ~/.claude/skills/manager/install-gh.sh    # no sudo, no brew; idempotent
   ```
   Offer these options:
   - **Install gh now — no sudo needed (Recommended)** → run the script, then
     hand the user the one manual step (below).
   - **Use local mode instead** → skip gh entirely; set `push_mode: "local"`
     in Step 3 (they can switch later — the same script works any time).
   - **I'll install it myself** → give the fix-later commands and continue in
     local mode so nothing is half-working meanwhile.
3. **The login is the ONE interactive step** — `gh auth login` prompts can't be
   answered by you. Right after the install, tell the user exactly:
   > Type `! gh auth login` here (runs it in this session) — pick GitHub.com →
   > HTTPS → Login with a web browser. Takes ~30 seconds.
   Then verify with `gh auth status` before moving on. If a new PATH entry was
   added, prefix your own checks with `export PATH="$HOME/.local/bin:$PATH"`.
4. **poppler is optional and library-heavy** — there's no clean no-sudo path,
   so without a package manager just note "PDF ingest / the panel's PDF view
   won't be available" and move on. Never block the install on it.
5. **Python:** 3.9+ is enough, so this is rarely a problem. If `python3` really
   is older, first look for an already-installed newer one
   (`command -v python3.12 python3.11 python3.10 python3.9`; on macOS also
   `/usr/bin/python3`) before proposing an install.

## Step 3 — Interview the user, then write `config.json`

Ask the user for these values (offer the defaults shown). Look at their target
repo first so your suggested answers are informed (e.g. read `package.json` /
`Makefile` / CI workflows to propose setup/dev/verify commands):

1. **Target repo path** (required) — absolute path to the git checkout the
   manager will work on.
2. **Base branch** (default `main`) — the integration branch: dedup truth,
   worktree base, and PR target. Check `git -C <repo> remote show origin` for
   the default branch.
3. **Setup command** (default `npm install`) — run once in each fresh worktree.
4. **Does this project have a web frontend you can open in a browser?** This
   decides whether the panel ever offers a "Test this branch"/"Open app"
   button — get it right once here rather than a broken button showing up
   later. Look at `package.json`/README to propose an informed default before
   asking (a `dev`/`start` script that boots a UI vs. one that's clearly an
   API/CLI/worker with nothing to browse to).
   - **Yes** → ask **Dev command** (default `npm run dev`) and **Dev port**
     (default `3000`) — the command + port that serve the browsable app.
     Set config `has_web_ui: true`.
   - **No** (API/library/CLI/backend-only) → skip dev command/port entirely;
     set config `has_web_ui: false`. The panel will show a plain-language
     before/after of each fix instead of a live-app button.
5. **Verify command** (default `npm run lint && npm test`) — the quality gate
   every ship-task run must pass before committing.
6. **Env files** (default none) — repo-relative gitignored files (e.g. `.env`,
   `apps/web/.env.local`) to symlink into each worktree.
7. **Push mode** (default `pr`) — where finished work lands: `pr` pushes each
   branch and opens a GitHub PR (needs `gh`); `local` stops at a committed
   local branch — no push, no PR, no `gh`, nothing leaves the machine. If the
   user has no GitHub remote or declined installing `gh`, recommend `local`
   (they can toggle any time from the panel's PR/Local button).
8. **Optional**: extra risk keywords (project terms that must force extra rigor,
   e.g. a tenancy column name), and a memory MCP server name if they have one.

Then write the answers to `~/.claude/skills/manager/config.json` using
`~/.claude/skills/manager/config.example.json` as the template, and validate:

```bash
python3 ~/.claude/skills/manager/skill_config.py check   # must print "config OK"
python3 ~/.claude/skills/manager/skill_config.py show    # echo the resolved config to the user
```

Also suggest customizing `~/.claude/skills/ship-task/BUILDER_PLAYBOOK.md` — it's
a template for the target repo's architecture + footguns, and a filled-in
playbook markedly improves the implement agent. Offer to draft it now by reading
the target repo.

Finally, add the run artifacts to the **target repo's** `.gitignore` (ship-task
commits with `git add -A` inside the worktree, so unignored artifacts would ride
into PRs):

```
.ship-task/
.feature-test-result.json
```

**If the user has no target repo on this machine yet** (or wants to decide
later): do NOT write a config.json with guessed values. Finish the install
without it and include in your final report exactly how to complete setup later:

```
cp ~/.claude/skills/manager/config.example.json ~/.claude/skills/manager/config.json
# edit "repo" + the commands, then:
python3 ~/.claude/skills/manager/skill_config.py check
```

Everything else (skills, selftests) is already done. Without a repo the daemon
boots into a **setup screen** (paste a repo path there, or answer Claude's
setup question on the first `/manager` run) — and nothing can dispatch until a
valid repo is saved, so a half-configured install can't run by accident.

## Step 4 — Smoke-check (no real dispatch)

```bash
cd ~/.claude/skills/manager
python3 managerd.py init                        # creates the SQLite queue
python3 dedup_index.py --selftest               # offline dedup logic check
python3 ../ship-task/risk_floor.py --selftest   # risk-floor battery
bash ../ship-task/verify.sh --selftest          # verify harness (expects 1 PASS, 1 FAIL)
```

All four must succeed (verify.sh's selftest intentionally shows one simulated
FAIL and exits 1 — that IS the pass condition for the harness).

## Step 5 — Print "how to use"

Tell the user:

```
Installed. To use it:

  1. Open Claude Code in your target repo:   cd <repo> && claude
  2. Run the manager on a work source:
       /manager <notion page url>          # needs your Notion connector
       /manager ~/Desktop/qa-findings.pdf  # PDF of findings (poppler for the board view)
       /manager "fix X; add Y; investigate Z"
  3. A control panel opens at http://127.0.0.1:<panel_port> — watch runs,
     answer the few questions that need you, and merge the finished PRs
     from GitHub. Runs always stop at PR-ready; nothing merges itself.

  Single tasks (no orchestration): /ship-task "add a CSV export button"

  ⚠ Dispatched runs use `claude --dangerously-skip-permissions` (headless
  agents that edit files, run commands, push branches, open PRs in your
  configured repo — and never merge). Read
  ~/.claude/skills/manager/dispatch.sh before your first batch.
```
