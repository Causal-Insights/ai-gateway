-- Execution visibility is independent of pricing, attribution and projections.
-- NULL means unknown: it must never be represented as a free execution.
alter table "LiteLLM_SpendLogs" alter column spend drop not null;
alter table gateway_cost_attempts add column if not exists log_recorded_at timestamptz;
alter table gateway_cost_attempts add column if not exists attribution_status text not null default 'pending';
alter table gateway_cost_attempts add column if not exists attribution_reason text;
alter table gateway_cost_attempts add column if not exists pricing_issue text;
alter table gateway_cost_attempts add column if not exists projection_status text not null default 'pending';
alter table gateway_cost_attempts add column if not exists projection_error text;
alter table gateway_cost_attempts add column if not exists projected_at timestamptz;
alter table gateway_cost_attempts add column if not exists evidence_conflict boolean not null default false;
-- Old committed rows already incremented aggregates/budgets. Never apply twice.
update gateway_cost_attempts set projected_at=committed_at,projection_status='applied',
  log_recorded_at=committed_at,attribution_status='complete'
where committed_at is not null and log_recorded_at is null;
create table if not exists gateway_cost_observations (
  observation_id text primary key,
  attempt_id text not null references gateway_cost_attempts(attempt_id) on delete cascade,
  evidence jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists gateway_cost_observations_attempt on gateway_cost_observations(attempt_id);
