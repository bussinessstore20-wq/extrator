-- Extrator tables in the existing Supabase project "SentinelChat".
-- Safe namespace: all table names start with extrator_.
create table if not exists public.extrator_products (
  id uuid primary key default gen_random_uuid(),
  site_id text not null default 'MLB',
  item_id text not null,
  category_id text,
  title text not null,
  price numeric(14,2),
  currency_id text,
  thumbnail text,
  permalink text not null,
  status text not null default 'pending_approval'
    check (status in ('pending_approval','approved','rejected','publishing','published','publish_failed')),
  metadata jsonb not null default '{}'::jsonb,
  discovered_at timestamptz not null default now(),
  last_checked_at timestamptz,
  updated_at timestamptz not null default now(),
  reviewed_by bigint,
  reviewed_at timestamptz,
  review_chat_id bigint,
  review_message_id bigint,
  channel_message_id bigint,
  published_at timestamptz,
  unique (site_id, item_id)
);
create index if not exists extrator_products_status_discovered_idx
  on public.extrator_products (status, discovered_at desc);
create index if not exists extrator_products_category_idx
  on public.extrator_products (category_id);

create table if not exists public.extrator_collection_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running'
    check (status in ('running','completed','partial','failed')),
  categories_seen integer not null default 0,
  items_seen integer not null default 0,
  new_items integer not null default 0,
  error_count integer not null default 0,
  details jsonb not null default '{}'::jsonb
);

create table if not exists public.extrator_settings (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists public.extrator_audit_logs (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  event_type text not null,
  product_id uuid references public.extrator_products(id) on delete set null,
  actor_telegram_id bigint,
  details jsonb not null default '{}'::jsonb
);

alter table public.extrator_products enable row level security;
alter table public.extrator_collection_runs enable row level security;
alter table public.extrator_settings enable row level security;
alter table public.extrator_audit_logs enable row level security;

revoke all on public.extrator_products from anon, authenticated;
revoke all on public.extrator_collection_runs from anon, authenticated;
revoke all on public.extrator_settings from anon, authenticated;
revoke all on public.extrator_audit_logs from anon, authenticated;
grant all on public.extrator_products to service_role;
grant all on public.extrator_collection_runs to service_role;
grant all on public.extrator_settings to service_role;
grant all on public.extrator_audit_logs to service_role;
grant usage, select on sequence public.extrator_audit_logs_id_seq to service_role;

insert into public.extrator_settings(key, value)
values
 ('collector', '{"enabled": false, "interval_seconds": 900, "categories_per_cycle": 5, "max_items_per_category": 50, "site_id": "MLB"}'::jsonb),
 ('publication', '{"approval_required": true}'::jsonb)
on conflict (key) do nothing;
