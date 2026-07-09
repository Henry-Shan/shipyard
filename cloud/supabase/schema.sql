-- manager cloud — auth schema (Phase 2)
-- Run this once in your Supabase project's SQL editor
-- (Dashboard → SQL Editor → paste → Run).
--
-- It adds ONE table: the machines a logged-in user owns. Login itself is
-- handled by Supabase Auth (no table needed — auth.users is built in). The
-- relay traffic (state/commands/arm) stays on the existing Vercel+Upstash
-- path, keyed by each machine's token; this table just issues those tokens
-- to accounts and enforces that a user only ever sees their own.

create extension if not exists pgcrypto;

create table if not exists public.machines (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name       text not null default 'my computer',
  -- the pairing secret the daemon uses; generated server-side so the user
  -- never invents or types one.
  token      text not null unique default ('sk_machine_' || encode(gen_random_bytes(16), 'hex')),
  created_at timestamptz not null default now()
);

-- Row-level security: every row is scoped to its owner. A logged-in user can
-- only ever select/insert/update/delete THEIR OWN machines.
alter table public.machines enable row level security;

create policy "machines: select own" on public.machines
  for select to authenticated using (auth.uid() = user_id);
create policy "machines: insert own" on public.machines
  for insert to authenticated with check (auth.uid() = user_id);
create policy "machines: update own" on public.machines
  for update to authenticated using (auth.uid() = user_id);
create policy "machines: delete own" on public.machines
  for delete to authenticated using (auth.uid() = user_id);
