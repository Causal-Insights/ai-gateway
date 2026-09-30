create table if not exists gateway_voice_resources (
    id text primary key,
    app_id text not null,
    owner_id text not null,
    request_id text not null,
    request_hash text not null,
    name text not null,
    consent jsonb not null,
    sample_hashes jsonb not null,
    provider_voice_id text unique,
    state text not null default 'creating'
      check (state in ('creating','ready','verification_required','outcome_unknown','failed','deleted')),
    requires_verification boolean not null default false,
    error_code text,
    deleted_at timestamptz,
    cleanup_completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(app_id, owner_id, request_id)
);
create index if not exists gateway_voice_owner_idx on gateway_voice_resources(app_id,owner_id,created_at desc);
create table if not exists gateway_voice_events (
    id bigserial primary key,
    voice_id text not null references gateway_voice_resources(id),
    action text not null,
    created_at timestamptz not null default now()
);
