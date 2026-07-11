create table if not exists gateway_generation_jobs (
  id text primary key,
  owner_key_hash text not null,
  owner_context jsonb not null default '{}'::jsonb,
  idempotency_key text not null,
  request_hash text not null,
  modality text not null check (modality in ('video')),
  model text not null,
  provider text not null check (provider in ('xai', 'byteplus', 'vertex')),
  status text not null check (
    status in ('submitting', 'queued', 'in_progress', 'completed', 'failed', 'expired', 'cancelled')
  ),
  provider_status text,
  provider_request_id text,
  progress numeric check (progress is null or (progress >= 0 and progress <= 100)),
  request_metadata jsonb not null default '{}'::jsonb,
  result_url text,
  result_mime_type text,
  error_code text,
  error_message text,
  error_retryable boolean not null default false,
  usage jsonb,
  response_cost_usd numeric,
  poll_attempts integer not null default 0,
  consecutive_poll_errors integer not null default 0,
  next_poll_at timestamptz default (now() + interval '1 minute'),
  last_polled_at timestamptz,
  deadline_at timestamptz not null,
  callback_token_hash text,
  callback_received_at timestamptz,
  spend_logged_at timestamptz,
  submitted_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_key_hash, idempotency_key)
);

create unique index if not exists gateway_generation_jobs_provider_request_uidx
  on gateway_generation_jobs (provider, provider_request_id)
  where provider_request_id is not null;

create index if not exists gateway_generation_jobs_due_idx
  on gateway_generation_jobs (next_poll_at)
  where status in ('queued', 'in_progress');

create index if not exists gateway_generation_jobs_terminal_retention_idx
  on gateway_generation_jobs (completed_at)
  where status in ('completed', 'failed', 'expired', 'cancelled');
