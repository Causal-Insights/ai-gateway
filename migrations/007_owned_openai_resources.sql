create table if not exists gateway_openai_resources (
    id text primary key,
    kind text not null,
    app_id text not null,
    owner_id text not null,
    request_id text,
    request_hash text,
    provider_id text,
    state text not null default 'creating',
    data jsonb not null default '{}',
    manifest jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(kind, app_id, owner_id, request_id),
    unique(kind, provider_id)
);
create index if not exists gateway_openai_resources_owner_idx
    on gateway_openai_resources(app_id,owner_id,kind,created_at desc);
create table if not exists gateway_openai_batch_items (
    batch_id text not null references gateway_openai_resources(id),
    custom_id text not null,
    model text not null,
    accounting_id text not null,
    attempt_id text not null,
    result_hash text,
    state text not null default 'pending',
    primary key(batch_id,custom_id)
);

create table if not exists gateway_openai_owner_cleanup (
    app_id text not null,
    owner_id text not null,
    revoked_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key(app_id, owner_id)
);
