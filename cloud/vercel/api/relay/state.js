// POST /api/relay/state  { token, name, state }
// The machine's bridge pushes its filtered metadata snapshot up. Executes
// nothing, stores nothing but metadata (the boundary is enforced machine-side
// in relay_client.py — code/diffs/logs never reach here because they're never
// put on the wire).
import { redis, cors, body, STATE_TTL } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  const { token, name, state } = body(req);
  if (!token) return res.status(400).json({ error: 'token required' });
  await redis.set(`state:${token}`, { name, state, ts: Date.now() }, { ex: STATE_TTL });
  await redis.set(`seen:${token}`, Date.now(), { ex: STATE_TTL });
  res.json({ ok: true });
}
