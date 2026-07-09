// GET /api/dash/state?token=...
// What the dashboard renders — the metadata the machine last pushed up, plus
// whether it's currently online.
import { redis, cors, ONLINE_MS } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  const token = req.query.token;
  if (!token) return res.json({ online: false, name: null, state: {} });
  const rec = await redis.get(`state:${token}`);
  const seen = await redis.get(`seen:${token}`);
  const online = !!seen && (Date.now() - Number(seen) < ONLINE_MS);
  res.json({
    online,
    name: rec?.name || null,
    state: rec?.state || {},
    state_ts: rec?.ts || 0,
  });
}
