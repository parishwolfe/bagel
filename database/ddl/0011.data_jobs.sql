create table public.data_jobs (
  id uuid primary key default gen_random_uuid(),
  request_hash text not null unique,
  provider text not null check(provider in ('alpaca','massive')),
  spec jsonb not null,
  state text not null default 'queued' check(state in ('queued','running','complete','failed','canceled')),
  attempts smallint not null default 0 check(attempts between 0 and 3),
  lease_token uuid,
  lease_until timestamptz,
  result jsonb,
  error_code text,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check((state='running')=(lease_token is not null and lease_until is not null))
);
create index data_jobs_claim on public.data_jobs(provider,created_at,id)
  where state in ('queued','running');
revoke all on public.data_jobs from public;
grant select,insert,update on public.data_jobs to bagel_ingester;
alter table public.data_jobs enable row level security;
create policy data_jobs_ingester on public.data_jobs to bagel_ingester using(true) with check(true);
