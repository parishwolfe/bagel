create table public.experiments (
  id uuid primary key default gen_random_uuid(),
  request_hash text not null unique,
  dataset_id text not null references public.datasets,
  spec jsonb not null,
  total_trials bigint not null check(total_trials > 0),
  generated_trials bigint not null default 0 check(generated_trials >= 0),
  state text not null default 'active' check(state in ('active','canceled')),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check(generated_trials <= total_trials)
);
create table public.experiment_trials (
  experiment_id uuid not null references public.experiments,
  ordinal bigint not null check(ordinal >= 0),
  run_id uuid references public.research_runs,
  normalized_spec jsonb not null,
  error_code text,
  primary key(experiment_id,ordinal),
  check((run_id is null) = (error_code is not null))
);
create index experiment_trials_run on public.experiment_trials(run_id);
revoke all on public.experiments,public.experiment_trials from public;
grant select,insert,update on public.experiments to bagel_research;
grant select,insert on public.experiment_trials to bagel_research;
alter table public.experiments enable row level security;
alter table public.experiment_trials enable row level security;
create policy experiments_research on public.experiments to bagel_research using(true) with check(true);
create policy trials_research on public.experiment_trials to bagel_research using(true) with check(true);
