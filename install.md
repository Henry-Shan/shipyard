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

**The checklist above IS the step-by-step report — do not also build a table
recapping it at the end.** Wide multi-column tables wrap and garble in a
terminal; more importantly, the user just watched these steps happen and
doesn't need them repeated. The one thing you print after `[3/3]` is Step 4's
short "how to use" message — nothing else, unless something is still broken
(then say what, in one line, before the how-to-use block).

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
> **This also happens without a terminal.** Answering the panel's task-doc
> question directly on the website — no `/manager` session running — makes
> the daemon launch that same kind of unattended agent itself
> (`skills/manager/auto-manager.sh`, same permission posture). There is no
> confirmation step between answering on the panel and a real, unattended
> session starting in the configured repo.
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

**Prove the update actually happened — don't just assume `pull` worked.**
Capture the commit before and after, and treat a failed fast-forward as a
real failure, not something to silently continue past (an install that
*looks* done but is still running old code is worse than one that visibly
stopped):

```bash
WAREHOUSE=~/.claude/agent-warehouse
if [ -d "$WAREHOUSE/.git" ]; then
  OLD_SHA="$(git -C "$WAREHOUSE" rev-parse --short HEAD)"
  git -C "$WAREHOUSE" fetch origin main --quiet
  git -C "$WAREHOUSE" log --oneline HEAD..origin/main 2>/dev/null | head  # preview what's new
  if ! git -C "$WAREHOUSE" pull --ff-only; then
    echo "UPDATE FAILED — $WAREHOUSE has local changes blocking a fast-forward pull." >&2
    echo "Report this to the user and STOP here; do not continue as if updated." >&2
    echo "Show them: git -C \"$WAREHOUSE\" status" >&2
    echo "Then ask before running either: git -C \"$WAREHOUSE\" stash   (keep the changes)" >&2
    echo "                             or: git -C \"$WAREHOUSE\" reset --hard origin/main   (discard them)" >&2
    exit 1
  fi
  NEW_SHA="$(git -C "$WAREHOUSE" rev-parse --short HEAD)"
  if [ "$OLD_SHA" = "$NEW_SHA" ]; then
    echo "Already up to date at $NEW_SHA"
  else
    echo "Updated $OLD_SHA -> $NEW_SHA"
  fi
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

Report the actual `Updated <old> -> <new>` / `Already up to date at <sha>` line
to the user verbatim — that's the proof it truly updated, not just a claim.

Handle these cases:
- **Old copy-based install** (`~/.claude/skills/manager` is a real directory,
  not a symlink): preserve the user's state first —
  `cp ~/.claude/skills/manager/config.json "$WAREHOUSE/skills/manager/config.json"`
  (plus `manager.db` and `runs/` if present) — then, with the user's OK, replace
  each real directory with the symlink.
- **A skill folder exists but is neither a symlink into the warehouse nor the
  old install**: name-collision with something else the user has — ask before
  touching it.
- **Update run** (clone already existed): still run Step 2's prerequisite
  checks — don't skip straight to Step 3. They're cheap and usually all
  green, but skipping them is exactly how a real gap (no git identity
  configured, `gh` never set up) stops being caught here and instead
  surfaces later, mid-`/manager`-run, on a machine that's been running this
  skill for a while but never got the checks run against it. After Step 2 +
  Step 3, tell the user what changed and whether any NEW skills were linked,
  and stop — still no project interview on an update.

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
git config user.name && git config user.email   # identity for commits ship-task/manager make
gh auth status          # must show a logged-in account — ship-task opens PRs with gh
claude --version        # the Claude Code CLI — dispatch.sh launches headless runs with it
pdftoppm -v             # poppler — OPTIONAL: only needed for PDF ingest / the panel's PDF view
```

| Check | Needed for | If missing |
|---|---|---|
| `python3` ≥ 3.9 | every script (stdlib only — **macOS's built-in Python qualifies**) | see fix flow below |
| `git` | worktrees, dedup, branches | install git (macOS: `xcode-select --install`) |
| git identity (`user.name`+`user.email`) | every commit ship-task/manager makes, in EITHER mode | have the user run it themselves — see below; **never run `git config` for them** |
| `gh` + logged in | opening PRs — **only needed in PR mode**; in local mode (`push_mode: "local"`, nothing pushed) skip it entirely | `brew install gh` / `apt install gh`, then `gh auth login` |
| `claude` CLI | dispatching headless runs | install Claude Code |
| poppler (`pdftoppm`) | **optional** — PDF ingest + panel PDF view | `brew install poppler` / `apt install poppler-utils` |

**Fix git identity FIRST, before anything else on this list, if it's missing —
this is the one check that matters no matter which mode the user ends up in,
and getting it out of the way here means `/manager` never has to stop mid-run
to ask for it.** Tell the user exactly (never run this for them, never guess a
name/email):
> Type this here (fill in your real name/email) — then tell me to continue:
> `git config --global user.name "Your Name" && git config --global user.email "you@example.com"`

Then re-run `git config user.name && git config user.email` yourself to
confirm before moving on to the rest of this step.

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
   - **Use local mode instead** → skip gh entirely, permanently for now (see
     the persistence note below) — nothing pushes anywhere, no GitHub account
     is ever touched.
   - **I'll install it myself** → treat it the same as "use local mode" for
     now (see below) so nothing is half-working meanwhile; the user can come
     back and re-run this install line later once `gh auth login` is done, and
     this check will flip the decision to GitHub-linked automatically.
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

**Persist the gh decision — this is the ONLY place it's ever decided.** Once
`gh auth status` succeeds (whether it was already working or you just fixed
it), or the user picks local mode / defers it, record the outcome in
`config.json` right now. Neither `/manager` nor `/ship-task` will ask about
GitHub linkage again — a later "create a new repo for me" flow just reads this
and acts on it, silently:

```bash
python3 - <<'EOF'
import json, os
path = os.path.expanduser("~/.claude/skills/manager/config.json")
try:
    with open(path) as f: cfg = json.load(f)
    if not isinstance(cfg, dict): cfg = {}
except (OSError, ValueError):
    cfg = {}
cfg["gh_available"] = GH_OK              # True if `gh auth status` succeeded, else False
if not GH_OK:
    cfg["push_mode"] = "local"           # no gh yet -> nothing should try to push/open a PR
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
EOF
```

(Substitute the real boolean for `GH_OK` — don't leave the literal in place.)

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

## Step 4 — Print "how to use" (this is the ENTIRE final message)

Print exactly this shape, filled in — nothing before it, nothing after it. No
table, no step-by-step recap, no restating what's installed or what passed:

```
Installed ✓ — manager, ship-task, feature-tester.

  1. cd <your target repo> && claude
  2. /manager
     (first run there asks its own quick setup: repo, base branch, web
     frontend or not, PR vs. local — one time per project)

  Single task, no orchestration:  /ship-task "add a CSV export button"

  ⚠ Runs use `claude --dangerously-skip-permissions` — read
  ~/.claude/skills/manager/dispatch.sh before your first batch.
```

If gh ended up unavailable/deferred (`gh_available: false`), append exactly
ONE line: `Local mode for now — no gh, nothing pushes. Say the word if you
want PR mode set up later.` If something is still genuinely broken (a real
prerequisite that couldn't be fixed), say that in one line above the block
instead — never silently print success.
