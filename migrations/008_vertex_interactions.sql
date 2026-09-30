create table if not exists gateway_vertex_interactions (
 id text primary key,
 app_id text not null,
 owner_id text not null,
 request_id text not null,
 request_hash text not null,
 model text not null,
 provider_id text,
 state text not null default 'creating',
 accounting_id text,
 response jsonb,
 deleted_at timestamptz,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(app_id,owner_id,request_id)
);
create index if not exists gateway_vertex_interactions_owner_idx on gateway_vertex_interactions(app_id,owner_id,created_at desc);

alter table gateway_vertex_interactions add column if not exists receipt_projected boolean not null default false;
