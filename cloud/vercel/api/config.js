// GET /api/config
// Hands the static dashboard its public Supabase values (URL + anon key) so it
// can init the supabase-js client without hardcoding them. Both are PUBLIC by
// design (the anon key is meant to ship in browser code; row-level security is
// what protects data). If they're unset, the dashboard falls back to the old
// browser-token mode — so the site keeps working until auth is configured.
export default function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 'no-store');
  res.json({
    supabaseUrl: process.env.SUPABASE_URL || '',
    supabaseAnonKey: process.env.SUPABASE_ANON_KEY || '',
  });
}
