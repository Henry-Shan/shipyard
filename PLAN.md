# manager-cloud — hosted control plane for the manager skill

Turn the local-only manager panel into a **solo, hosted product**: a website +
backend that lets one user drive Claude Code runs on their own computer(s) from
anywhere. Execution and Claude auth stay **100% local** (the Raft model — see
their privacy policy); only task *metadata* ever crosses to the cloud.

## Locked decisions

- **Solo** — no teams, channels, DMs, or sharing. Data model is just
  `user → machines → projects → tasks`.
- **Claude-only for now** — the daemon keeps a thin adapter seam so other
  agents can slot in later, but there's one adapter today: `claude`.
- **Testing stays on-machine** — "Test this branch" boots the dev server on the
  machine the daemon runs on. The *remote-visible* artifact is the **screenshot
  evidence** the daemon already captures (`after_evidence` + `/browse`), synced
  up as an image (allowed across the boundary). No public tunnel to localhost.
- **Transport: outbound long-poll over stdlib HTTP.** The daemon dials *out* to
  the relay (works behind NAT, no inbound ports — the whole trick), and does it
  with `urllib` + `http.server` so the zero-pip-install ethos of this package
  holds. WebSocket is a later latency optimization, not a requirement.

## "Machine" = one computer running the daemon

The daemon is a background process on the user's laptop/desktop — the thing that
actually holds the git repos, the `claude` CLI login, and can spawn processes.
"Add a machine" in the dashboard = install + pair the daemon there
(`npx … --api-key sk_machine_…`, exactly the Raft flow). A solo user usually has
one machine, occasionally two (work + home) — each paired independently, each
revocable on its own.

## Architecture — three planes

```
┌──────────────────────── CLOUD (you host) ────────────────────────┐
│  Web dashboard (Phase 2)      Control-plane API + DB (Phase 2)     │
│         │                            │  metadata only              │
│         └──────────► Relay hub (this pass) ◄──────────────────────┤
└──────────────────────────────│────────────────────────────────────┘
                               │  outbound long-poll, dialed FROM the machine
┌───────────────────────────────▼──────────── USER'S MACHINE ────────┐
│  local daemon (managerd.py — already built, unchanged)             │
│         ▲ 127.0.0.1 HTTP API                                        │
│  relay_client.py (this pass) — pure bridge: local API ↔ relay      │
│         └── spawns claude via dispatch.sh / auto-manager.sh         │
└─────────────────────────────────────────────────────────────────────┘
```

The relay client is a **standalone bridge process**, deliberately *not* wired
into `managerd.py` — it only talks to the daemon over the existing 127.0.0.1
HTTP API (`/api/state`, `/api/questions/<id>/answer`, `/api/progress`). That
keeps the battle-tested daemon untouched and the new networking isolated.

## The metadata boundary (copied from Raft, enforced in `relay_client.py`)

**Allowed up:** task title/description/status/tier/source/pr_url/test_plan,
questions + answers, the progress feed, counts, project display facts
(repo *basename*, base_branch, push_mode, has_web_ui), screenshot images.

**Never leaves the machine:** code, diffs, file contents, worktree paths, run
logs, session ids, `config.json`, full repo paths, and above all the Claude
credentials (which live only in the local `claude` CLI login).

## Security model — the load-bearing part

Going hosted is a real privilege escalation: a remote command could otherwise
run `claude --dangerously-skip-permissions` on someone's machine. Controls, in
order of importance:

1. **The machine is the trust boundary, never the cloud.** The relay only moves
   messages. `relay_client.py` decides what actually runs. A compromised relay
   or web account cannot, by itself, execute anything.
2. **Arming is LOCAL-ONLY and defaults OFF** (`remote_exec_armed: false`). A
   relayed command that would launch a run is **refused** unless the user has
   armed *on the machine itself* (config edit / local flag). The dashboard can
   *show* armed state but has no way to *set* it — otherwise arming would be
   remotely forgeable and the whole gate would be theatre.
3. **Per-machine tokens** — hashed at rest, scoped to the user, one-click
   revoke = instant kill switch (Phase 2).
4. **Remote-triggered runs default to a tighter posture** (`--permission-mode
   acceptEdits` via the existing `MANAGER_PERMS`) since no human is at the
   terminal.
5. **Audit log** of every relayed command (Phase 2).

## Auth — three separate layers

1. **user ↔ website** — Supabase/Clerk/Auth.js (Phase 2, don't build it).
2. **machine token** — `sk_machine_…`, hashed, per machine (Phase 2 for the
   real store; Phase 0 accepts a dev token).
3. **Claude** — the local CLI login. Never crosses the wire, ever.

## Phased build

- **Phase 0 — prove the loop (THIS PASS):** relay hub + daemon bridge + metadata
  filter + arm gate, plus a minimal read-only-plus-one-action demo page. Proven
  end-to-end in an isolated harness. **Not** the real web app.
- **Phase 1:** dashboard can send the full command set; polished pairing UX.
- **Phase 2:** accounts, Postgres, project management UI, hashed/revocable
  tokens, audit log.
- **Phase 3:** billing, onboarding, screenshot-based remote "see the effect",
  WebSocket transport upgrade, the agent adapter seam filled with a 2nd agent.

## Stack

- **Daemon + relay client:** Python stdlib (reuse everything already built).
- **Relay hub:** Python stdlib now; move to Node/Go + Redis fan-out when
  connection count justifies it.
- **Web app (Phase 2):** Next.js, porting `review.html`'s render logic; live via
  the relay. DB: Postgres (Supabase/Neon).

## This pass ships

- `cloud/relay_server.py` — stdlib relay hub (long-poll down, state up, command
  queue, minimal demo dashboard at `/`).
- `skills/manager/relay_client.py` — stdlib bridge with the metadata filter and
  the local-only arm gate.
- `remote_exec_armed` config default (OFF).
- An isolated end-to-end test proving: hosted page → command → relay → daemon,
  state syncing back up, and the arm gate refusing to execute by default.
