// POST /api/dash/command  { token, type, payload }
// The dashboard (user side) queues a command for the machine to pull on its
// next poll. This NEVER executes anything itself — the machine's relay_client
// decides whether to act (and the arm gate can refuse). See PLAN.md.
import { redis, cors, body } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  const { token, type, payload } = body(req);
  if (!token) return res.status(400).json({ error: 'token required' });
  const id = await redis.incr('cmdseq');
  const cmd = { id: String(id), type: type || '', payload: payload || {}, ts: Date.now() };
  await redis.rpush(`queue:${token}`, cmd);
  res.json({ ok: true, id: String(id) });
}
