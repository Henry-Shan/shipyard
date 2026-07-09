// GET /api/relay/poll?token=...
// The machine SHORT-polls this (every ~1.5s) instead of holding a long-poll —
// serverless functions can't hold a connection open, so the daemon pulls
// instead. Returns the next queued command (or null) and refreshes the
// online marker.
import { redis, cors, STATE_TTL } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  const token = req.query.token;
  if (!token) return res.status(400).json({ error: 'token required' });
  await redis.set(`seen:${token}`, Date.now(), { ex: STATE_TTL });
  const cmd = await redis.lpop(`queue:${token}`);   // FIFO; auto-deserialized
  const armed = await redis.get(`armed:${token}`);  // cloud-controlled arm state
  res.json({ command: cmd || null, armed: armed == 1 });
}
