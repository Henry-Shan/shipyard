# Install the manager skill package

You are an AI coding agent installing this package for a user. Work through the
steps in order. Report everything that's missing — never fail silently.

**Scope: this installs the skills onto this machine, nothing more.** Do NOT
ask about a target repo, base branch, dev/build/test commands, or anything
else project-specific — none of that belongs here. The user picks a project
later, in a separate Claude Code session, by running `/manager` inside it;
that first run interviews them for exactly that project's settings. Keep this
install fast, clean, and generic.

Report progress as a running checklist as you go — mark each item done the
moment it finishes, don't wait until the end to report everything at once:

```
[1/3] Installing skills…       ✓
[2/3] Checking prerequisites…  ✓ (or: ⚠ 1 issue — see below)
[3/3] Running smoke tests…     ✓
```

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
>
> **Never run `git config` (global or local) to set an identity or anything
> else — not even a plausible-looking guess — and never link a repo to GitHub
> (`gh repo create`, adding a remote, pushing) without asking first whether
> the user wants that at all.** If a git operation needs something that isn't
> configured, stop and give the user the exact command to run themselves.

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
  Step 3's smoke checks, tell the user what changed and whether any NEW
  skills were linked, and stop.

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
   - **Use local mode instead** → skip gh entirely; they can set
     `push_mode: "local"` during the first `/manager` run in their project
     (and switch any time after from the panel's PR/Local button).
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

## Step 3 — Smoke-check (no real dispatch)

```bash
cd ~/.claude/skills/manager
python3 managerd.py init                        # creates the SQLite queue
python3 dedup_index.py --selftest               # offline dedup logic check
python3 ../ship-task/risk_floor.py --selftest   # risk-floor battery
bash ../ship-task/verify.sh --selftest          # verify harness (expects 1 PASS, 1 FAIL)
```

All four must succeed (verify.sh's selftest intentionally shows one simulated
FAIL and exits 1 — that IS the pass condition for the harness).

## Step 4 — Print "how to use"

Tell the user:

```
Installed. Nothing project-specific was asked or configured — that happens
next, automatically, the first time you run /manager inside a real project.

To use it:

  1. Open Claude Code in your target repo:   cd <repo> && claude
  2. Run:   /manager
     First run in a new repo, it asks a few quick setup questions right there
     (repo path, base branch, whether there's a web frontend to test, PR vs.
     local mode) — then every run after that just works.
  3. A control panel opens at http://127.0.0.1:<panel_port> — watch runs,
     answer the few questions that need you, and merge the finished PRs
     from GitHub. Runs always stop at PR-ready; nothing merges itself.

  Single tasks (no orchestration): /ship-task "add a CSV export button"

  ⚠ Dispatched runs use `claude --dangerously-skip-permissions` (headless
  agents that edit files, run commands, push branches, open PRs in your
  configured repo — and never merge). Read
  ~/.claude/skills/manager/dispatch.sh before your first batch.
```
