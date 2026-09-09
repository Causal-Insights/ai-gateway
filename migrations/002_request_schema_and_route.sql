alter table gateway_generation_jobs
  add column if not exists request_schema_version smallint not null default 1,
  add column if not exists provider_route text,
  add column if not exists adapter_revision text;

update gateway_generation_jobs set provider_route = case
  when provider = 'xai' then 'xai_videos_v1'
  when provider = 'byteplus' and model like 'seedance-2.5%' then 'byteplus_las_v1'
  when provider = 'byteplus' then 'byteplus_ark_v3'
  when provider = 'vertex' and (model like 'veo-%' or model like 'vertex_ai/veo-%') then 'vertex_litellm_video'
  when provider = 'vertex' then 'vertex_omni_interactions'
end where provider_route is null;

alter table gateway_generation_jobs alter column provider_route set not null;
create index if not exists gateway_generation_jobs_route_idx on gateway_generation_jobs (provider_route, status);
