# Builder Playbook — read this BEFORE you write code

A 2-minute orientation + the repo's footguns, so an implement agent builds in the
right place, mirrors the right patterns, and doesn't repeat mistakes that cost
prior runs.

> **⚠ CUSTOMIZE THIS FILE FOR YOUR PROJECT.** The sections marked *(project)*
> are placeholders — fill them in from your repo's CLAUDE.md / architecture
> docs / hard-won failures, or ask an agent to draft them ("read this repo and
> fill in BUILDER_PLAYBOOK.md"). The **Build-loop discipline** section at the
> bottom is universal — keep it as is. A filled-in playbook is one of the
> highest-leverage things you can give the implement agent.

## The 30-second model *(project)*

```
<Draw your architecture here: clients, services, shared packages, databases —
 the boxes and arrows a new engineer needs before touching anything.>
```

## The vertical slice *(project)*

A feature/endpoint in this repo usually needs, in order:
1. *(e.g. server: handler + route registration + tenant/permission filtering)*
2. *(e.g. shared package: API function + types + re-export from the index)*
3. *(e.g. rebuild shared packages so consumers see fresh types)*
4. *(e.g. client wiring: which layer calls the API, which state store to use)*

Doing only some layers ships a half-wired feature — list every layer here.

## Footguns *(project)*

Each entry should be a rule distilled from a real failure, e.g.:
1. *(stale build artifacts: what must be rebuilt after editing shared code)*
2. *(which client libraries are deliberately NOT used where)*
3. *(the tenancy/permission check every new handler must include)*
4. *(test stack quirks: which runner/mocks, where the canonical template test lives)*
5. *(anything that is silently tolerated locally but fails in CI)*

## Build-loop discipline (universal — keep)

- **Locate first.** Read the failing test(s), pull the symbols/paths they reference,
  grep the repo for them → a short list of the EXACT files/functions to touch. Edit
  those (+ their transitive types); don't search the repo from scratch.
- **Inner loop = the focused test only.** Iterate with the repo's single-file test
  command — fast. Do NOT run the full `verify.sh` every cycle.
- **Full `verify.sh --root "$WT"` once** before you return — confirm no regressions.
- **Stuck?** Don't guess: root-cause with `superpowers:systematic-debugging` /
  `/investigate`; `/codex` challenge if the structure may be wrong (when installed).
- **Never** weaken/skip/delete a test, leave stubs/TODOs, or disable code to pass.
- **Worktree rooting** — under ship-task, EVERY build/test/git command runs in
  `$WT` (the worktree), never the main checkout. Test files are written under
  `$WT` too (else the commit step won't stage them).
