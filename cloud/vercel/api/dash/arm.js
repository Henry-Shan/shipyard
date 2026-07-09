// POST /api/dash/arm  { token, armed }
// The dashboard's Arm/Disarm button. Cloud-controlled (the user chose a pure
// website button over a machine-local switch) — so the machine token now also
// grants the power to arm + run. That is the accepted trade.
import { redis, cors, body } from '../../lib/store.js';

export default async function handler(req, res) {
  cors(res);
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  const { token, armed } = body(req);
  if (!token) return res.status(400).json({ error: 'token required' });
  // Persistent (no TTL) — armed stays until explicitly toggled off, not until
  // the machine goes idle.
  await redis.set(`armed:${token}`, armed ? 1 : 0);
  res.json({ ok: true, armed: !!armed });
}
