# Deploy the relay to Vercel

The `cloud/relay_server.py` in the parent dir is a single long-running process —
great for local dev, **but it can't run on Vercel**: Vercel is serverless
(functions spin up per request, die in seconds, share no memory, can't hold a
connection open). This directory is the **Vercel-native rewrite of the exact
same API**: serverless functions backed by **Upstash Redis** (serverless Redis),
with the machine **short-polling** instead of holding a connection.

The daemon + bridge still run on the **user's machine** — only the relay is
hosted. Execution and Claude auth never touch Vercel.

```
api/relay/state.js   POST  machine → push metadata snapshot up
api/relay/poll.js    GET   machine → pull next queued command (short-poll)
api/relay/ack.js     POST  machine → report a command's outcome
api/dash/state.js    GET   dashboard → read mirrored metadata + online status
api/dash/command.js  POST  dashboard → queue a command for the machine
api/dash/result.js   GET   dashboard → read a command's outcome
public/index.html          the dashboard
lib/store.js               shared Upstash Redis handle
```

## Deploy (you run these — they need YOUR Vercel + Upstash accounts)

1. **Create an Upstash Redis database** at <https://console.upstash.com> →
   copy its **REST URL** and **REST token** (the two `UPSTASH_REDIS_REST_*`
   values). Free tier is plenty for Phase 0.

2. From this directory:
   ```bash
   cd cloud/vercel
   npm install
   npx vercel            # first run: log in + link/create the project
   npx vercel env add UPSTASH_REDIS_REST_URL      # paste the URL,   pick all envs
   npx vercel env add UPSTASH_REDIS_REST_TOKEN    # paste the token, pick all envs
   npx vercel --prod
   ```
   (Or add the two env vars in the Vercel dashboard → Settings → Environment
   Variables, then redeploy. Vercel's Upstash integration can also inject them
   automatically if you add the Upstash integration to the project.)

3. Vercel prints your relay URL, e.g. `https://your-relay.vercel.app`. That is
   both the **dashboard** (open it in a browser) and the **relay endpoint** the
   machine dials.

## Point a machine at it

On the machine (Claude Code installed + logged in, manager daemon running):

```bash
MANAGER_RELAY_URL=https://your-relay.vercel.app \
MANAGER_MACHINE_TOKEN=sk_machine_whatever \
MANAGER_MACHINE_NAME="my-laptop" \
python3 ~/.claude/skills/manager/relay_client.py
```

Open the Vercel URL, paste the same token, and the board mirrors up. Sending a
task doc still only *runs* if the machine is armed locally
(`remote_exec_armed: true`) — the arm gate lives on the machine, not here, so
hosting the relay changes nothing about that guarantee.

## Notes / limits (Phase 0)

- **Dev tokens are accepted as-is.** Real per-user, hashed, revocable tokens +
  accounts are Phase 2 (see `../../PLAN.md`). Until then, treat the token as the
  only credential and keep it secret.
- Short-poll ≈ 1.5s command latency and steady light request volume — fine for a
  solo user with a few machines; revisit (WebSocket / a realtime service) at
  scale.
- CORS is open (`*`) so you can host the dashboard separately later; lock it down
  when you add real auth.
