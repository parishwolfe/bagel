create table public.strategy_inputs (
  run_id uuid not null references public.strategy_runs,
  activation_at timestamptz not null,
  bar_start timestamptz not null,
  series_id bigint not null references public.series,
  revision_id uuid not null,
  purpose text not null check(purpose in ('warmup','frame')),
  primary key(run_id,activation_at,bar_start,series_id,purpose)
);
revoke all on public.strategy_inputs from public;
alter table public.strategy_inputs enable row level security;
grant select,insert on public.strategy_inputs to bagel_strategy;
create policy strategy_input_worker on public.strategy_inputs to bagel_strategy using(true) with check(true);
