create table if not exists gateway_audio_requests (
    id text primary key,
    app_id text not null,
    owner_id text not null,
    request_id text not null,
    request_hash text not null,
    operation text not null,
    model text not null,
    state text not null default 'creating' check(state in ('creating','ready','outcome_unknown','failed','deleted')),
    accounting_id text,
    response_metadata jsonb,
    error_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(app_id,owner_id,request_id)
);
create table if not exists gateway_audio_payloads (
    request_id text primary key references gateway_audio_requests(id),
    content_type text not null,
    audio bytea not null
);
create table if not exists gateway_song_resources (
    id text primary key,
    app_id text not null,
    owner_id text not null,
    request_id text not null references gateway_audio_requests(id),
    provider_song_id text,
    state text not null default 'creating' check(state in ('creating','ready','outcome_unknown','failed','deleted')),
    duration_ms integer,
    composition_plan jsonb,
    deleted_at timestamptz,
    created_at timestamptz not null default now(),
    unique(request_id)
);
create index if not exists gateway_song_owner_idx on gateway_song_resources(app_id,owner_id,created_at desc);
create table if not exists gateway_speech_sessions (
    id text primary key references gateway_audio_requests(id),
    token_hash text not null,
    expires_at timestamptz not null,
    claimed_at timestamptz,
    voices jsonb not null,
    output_format text not null,
    max_characters integer not null,
    profile jsonb not null,
    identity jsonb not null
);
