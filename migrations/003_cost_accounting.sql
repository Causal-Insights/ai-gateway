-- Additive: independent of the 30-day media retention window. No foreign key
-- to jobs or keys: accounting survives media expiry and key deletion.
create table if not exists gateway_cost_requests (
  accounting_id text primary key,
  owner_key_hash text not null,
  identity jsonb not null,
  model text not null,
  route text not null,
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  historical boolean not null default false
);
create table if not exists gateway_cost_attempts (
  attempt_id text primary key,
  accounting_id text not null references gateway_cost_requests(accounting_id),
  provider_request_id text,
  profile jsonb not null,
  usage jsonb,
  raw_usage jsonb,
  served_model text,
  reported_charge numeric,
  outcome text,
  cost_status text not null default 'pending' check (cost_status in ('pending','priced','unresolved')),
  cost_usd numeric,
  calculated_cost_usd numeric,
  reported_cost_usd numeric,
  cost_source text,
  breakdown jsonb not null default '[]',
  zero_reason text,
  unresolved_reason text,
  created_at timestamptz not null default now(),
  observed_at timestamptz,
  committed_at timestamptz,
  check ((cost_status='priced' and cost_usd is not null and cost_usd >= 0 and committed_at is not null)
    or (cost_status <> 'priced' and cost_usd is null and committed_at is null)),
  check (cost_usd is null or cost_usd > 0 or zero_reason is not null)
);
create index if not exists gateway_cost_attempts_pending on gateway_cost_attempts(created_at) where cost_status <> 'priced';
create index if not exists gateway_cost_attempts_request on gateway_cost_attempts(accounting_id);
alter table gateway_cost_attempts add column if not exists daily_buckets jsonb not null default '{}';
alter table gateway_cost_attempts add column if not exists served_options jsonb;
create table if not exists gateway_cost_corrections (
  correction_id text primary key,
  accounting_id text not null,
  evidence jsonb not null,
  before_value jsonb not null,
  after_value jsonb not null,
  reason text not null,
  created_at timestamptz not null default now()
);
create table if not exists gateway_cost_invoice_checks (
  reconciliation_id text primary key,
  provider text not null,
  account_context text not null,
  period_start timestamptz not null,
  period_end timestamptz not null,
  dimensions jsonb not null,
  usage_cost_usd numeric not null,
  invoice_cost_usd numeric not null,
  evidence jsonb not null,
  created_at timestamptz not null default now(),
  check (period_end > period_start)
);
create table if not exists gateway_cost_cache_outbox (
  attempt_id text primary key references gateway_cost_attempts(attempt_id),
  identity jsonb not null,
  created_at timestamptz not null default now()
);
alter table gateway_generation_jobs add column if not exists accounting_id text;
