// POST /api/relay/ack  { token, cmd_id, result }
// The machine reports a command's outcome so the dashboard can show it
// (e.g. "started" vs "refused: not armed").
import { redis, cors, body, STATE_TTL } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  const { token, cmd_id, result } = body(req);
  if (!token || cmd_id == null) return res.status(400).json({ error: 'token + cmd_id required' });
  await redis.hset(`results:${token}`, { [String(cmd_id)]: result });
  await redis.expire(`results:${token}`, STATE_TTL);
  res.json({ ok: true });
}
