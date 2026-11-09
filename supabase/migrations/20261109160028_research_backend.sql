create schema research;
revoke all on schema research from public;

do $$ begin
  if not exists (select from pg_roles where rolname = 'bagel_research') then
    create role bagel_research nologin;
  end if;
end $$;

create table research.datasets (
  id text primary key check (id ~ '^[0-9a-f]{64}$'),
  manifest jsonb not null check (jsonb_typeof(manifest) = 'object'),
  created_at timestamptz not null default clock_timestamp()
);

create table research.runs (
  id uuid primary key default gen_random_uuid(),
  request_hash text not null unique check (request_hash ~ '^[0-9a-f]{64}$'),
  dataset_id text not null references research.datasets,
  engine_id text not null,
  spec jsonb not null check (jsonb_typeof(spec) = 'object'),
  actor uuid,
  state text not null default 'queued'
    check (state in ('queued', 'running', 'succeeded', 'failed', 'canceled')),
  attempts smallint not null default 0 check (attempts between 0 and 3),
  lease_token uuid,
  lease_until timestamptz,
  result_sha256 text check (result_sha256 ~ '^[0-9a-f]{64}$'),
  summary jsonb,
  error_code text,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check ((state = 'running') = (lease_token is not null and lease_until is not null)),
  check (state = 'running' or (lease_token is null and lease_until is null)),
  check ((state = 'succeeded') = (result_sha256 is not null and summary is not null))
);

create index research_claimable_runs on research.runs (engine_id, created_at, id)
  where state in ('queued', 'running');

create table research.attempts (
  run_id uuid not null references research.runs,
  attempt smallint not null check (attempt between 1 and 3),
  token uuid not null unique,
  state text not null check (state in ('running', 'succeeded', 'failed', 'canceled', 'expired')),
  started_at timestamptz not null default clock_timestamp(),
  ended_at timestamptz,
  error_code text,
  primary key (run_id, attempt),
  check ((state = 'running') = (ended_at is null))
);

create table research.batches (
  id uuid primary key default gen_random_uuid(),
  request_hash text not null unique check (request_hash ~ '^[0-9a-f]{64}$'),
  actor uuid,
  created_at timestamptz not null default clock_timestamp()
);

create table research.batch_runs (
  batch_id uuid not null references research.batches,
  ordinal integer not null check (ordinal between 0 and 999),
  run_id uuid not null references research.runs,
  primary key (batch_id, ordinal)
);

grant usage on schema research, market_data to bagel_research;
grant select, insert on research.datasets, research.batches, research.batch_runs to bagel_research;
grant select, insert, update on research.runs, research.attempts to bagel_research;
grant select on market_data.sources, market_data.instruments, market_data.series, market_data.bars
  to bagel_research;

do $$ declare t text; begin
  foreach t in array array['datasets', 'runs', 'attempts', 'batches', 'batch_runs'] loop
    execute format('alter table research.%I enable row level security', t);
    execute format('create policy research_access on research.%I to bagel_research '
      || 'using (true) with check (true)', t);
  end loop;
  foreach t in array array['sources', 'instruments', 'series', 'bars'] loop
    execute format('create policy research_read on market_data.%I for select '
      || 'to bagel_research using (true)', t);
  end loop;
end $$;
