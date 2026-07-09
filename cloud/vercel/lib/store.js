// Shared Redis handle + helpers for the Vercel relay functions.
// Upstash Redis is serverless (REST-based), so it works from short-lived
// Vercel functions where a long-lived in-memory store (the local
// relay_server.py) cannot. Config via env: UPSTASH_REDIS_REST_URL / _TOKEN.
import { Redis } from '@upstash/redis';

export const redis = Redis.fromEnv();

export const ONLINE_MS = 60_000;   // machine considered offline after this w/o a poll
export const STATE_TTL = 120;      // seconds a pushed state / seen marker lives

export function cors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
}

// Vercel parses JSON bodies automatically, but be defensive for raw strings.
export function body(req) {
  if (req.body && typeof req.body === 'object') return req.body;
  try { return JSON.parse(req.body || '{}'); } catch { return {}; }
}
