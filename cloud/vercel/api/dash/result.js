// GET /api/dash/result?token=...&cmd_id=...
// Fetch a command's reported outcome (set by the machine via /api/relay/ack).
import { redis, cors } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  const { token, cmd_id } = req.query;
  if (!token || !cmd_id) return res.json({ result: null });
  const r = await redis.hget(`results:${token}`, String(cmd_id));
  res.json({ result: r ?? null });
}
