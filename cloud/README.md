# manager cloud — Phase 0 relay

The hosted side of the manager (see [`../PLAN.md`](../PLAN.md)). This directory
is the **cloud control plane**, not part of the installed skill — the skill's
local half is `skills/manager/` (the daemon + `relay_client.py`).

Phase 0 proves the one new primitive the product needs: a machine behind NAT,
driven from a website, with **execution and Claude auth staying entirely local**
(the Raft model). Only task metadata crosses the wire.

## Pieces

| File | Role |
|------|------|
| `relay_server.py` | stdlib HTTP relay hub — long-poll down to the machine, metadata up from it, a command queue, and a demo dashboard at `/`. Moves messages only; executes nothing. |
| `dashboard.html` | minimal demo UI — connect with a machine token, watch the board sync up, send a task doc down. |
| `skills/manager/relay_client.py` | the local bridge (lives with the skill): local daemon HTTP ↔ relay, enforcing the metadata boundary and the local-only arm gate. |

## Run it (all local, for a demo)

```bash
# 1. the relay hub
python3 cloud/relay_server.py --port 8787          # dashboard at http://127.0.0.1:8787

# 2. the manager daemon for some project (as usual)
cd /your/repo && bash ~/.claude/skills/manager/panel-up.sh

# 3. the bridge, pairing this machine to the relay
MANAGER_RELAY_URL=http://127.0.0.1:8787 \
MANAGER_MACHINE_TOKEN=sk_machine_dev123 \
MANAGER_MACHINE_NAME="my-laptop" \
python3 ~/.claude/skills/manager/relay_client.py
```

Open the dashboard, paste `sk_machine_dev123`, and the project's board mirrors
up. Send a task doc from the page: it reaches the machine, but **only runs if
the machine is armed** (`remote_exec_armed: true` in that project's
`config.json` — off by default). Unarmed, it queues and says so.

## Security posture (Phase 0)

- The **machine is the trust boundary.** The relay only forwards; `relay_client.py`
  decides what runs. A compromised relay cannot execute anything on its own.
- **Arming is local-only and defaults off.** No relay message can set it.
- Metadata boundary enforced in `relay_client.py` — code, diffs, logs, worktree
  paths, full repo paths, and Claude credentials never leave the machine.

## Not in Phase 0 (see PLAN.md)

Real accounts, Postgres, hashed/revocable tokens, the production Next.js app,
billing, and the WebSocket transport upgrade. Dev tokens are accepted as-is and
state is in-memory here.
