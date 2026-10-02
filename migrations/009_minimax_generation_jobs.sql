-- Add a provider without rewriting historical jobs or their route identities.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'gateway_generation_jobs'::regclass
      and conname = 'gateway_generation_jobs_provider_check'
      and pg_get_constraintdef(oid) like '%minimax%'
  ) then
    alter table gateway_generation_jobs drop constraint if exists gateway_generation_jobs_provider_check;
    alter table gateway_generation_jobs add constraint gateway_generation_jobs_provider_check
      check (provider in ('xai', 'byteplus', 'vertex', 'minimax'));
  end if;
end $$;
