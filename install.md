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

## Step 1 — Get the skills into `~/.claude/skills/`

Clone the repo and copy the three skill folders (they must be siblings — the
scripts resolve each other by relative path):

```bash
git clone https://github.com/Henry-Shan/agent-warehouse.git /tmp/manager-skill-install
mkdir -p ~/.claude/skills
cp -R /tmp/manager-skill-install/skills/manager      ~/.claude/skills/manager
cp -R /tmp/manager-skill-install/skills/ship-task    ~/.claude/skills/ship-task
cp -R /tmp/manager-skill-install/skills/feature-tester ~/.claude/skills/feature-tester
rm -rf /tmp/manager-skill-install
chmod +x ~/.claude/skills/manager/*.sh ~/.claude/skills/ship-task/*.sh ~/.claude/skills/feature-tester/*.sh
```

(If `git clone` is unavailable, download and unpack the tarball instead:
`curl -L https://github.com/Henry-Shan/agent-warehouse/archive/refs/heads/main.tar.gz | tar xz`
and copy from the extracted `*/skills/` directory.)

If any of the three folders already exists, ask the user before overwriting.

## Step 2 — Check prerequisites and REPORT the result

Run each check and collect the results into one clear report. Missing items are
**reported, not silently tolerated** — tell the user exactly what's missing, why
it's needed, and how to install it. Only poppler is optional.

```bash
python3 -c 'import sys; assert sys.version_info >= (3,10), sys.version; print("python3 OK:", sys.version.split()[0])'
git --version
gh auth status          # must show a logged-in account — ship-task opens PRs with gh
claude --version        # the Claude Code CLI — dispatch.sh launches headless runs with it
pdftoppm -v             # poppler — OPTIONAL: only needed for PDF ingest / the panel's PDF view
```

| Check | Needed for | If missing |
|---|---|---|
| `python3` ≥ 3.10 | every script (daemon, dedup, config) | install Python 3.10+ |
| `git` | worktrees, dedup, branches | install git |
| `gh auth status` logged in | opening PRs | `gh auth login` |
| `claude` CLI | dispatching headless runs | install Claude Code |
| poppler (`pdftoppm`) | **optional** — PDF ingest + panel PDF view | `brew install poppler` / `apt install poppler-utils` |

If a **required** item is missing, stop after the report and let the user decide;
do not continue to a half-working install without saying so.

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
4. **Dev command** (default `npm run dev`) — starts the app for manual testing.
5. **Dev port** (default `3000`) — where the dev command serves; the panel
   health-checks it.
6. **Verify command** (default `npm run lint && npm test`) — the quality gate
   every ship-task run must pass before committing.
7. **Env files** (default none) — repo-relative gitignored files (e.g. `.env`,
   `apps/web/.env.local`) to symlink into each worktree.
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

Everything else (skills, selftests) is already done; the daemon refuses to start
until "repo" is set, so nothing half-configured can run by accident.

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
