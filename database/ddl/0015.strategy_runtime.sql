alter table public.trading_commands drop constraint trading_commands_action_check;
alter table public.trading_commands add constraint trading_commands_action_check
  check(action in ('arm','pause','order','cancel','target'));
alter table public.broker_orders add column parent_order_id uuid;
create index broker_order_parent on public.broker_orders(account_id,parent_order_id);

create table public.strategy_runs (
  id uuid primary key,
  request_hash text not null unique,
  spec jsonb not null,
  engine_id text not null,
  state text not null default 'paused' check(state in ('running','paused','failed')),
  reason text not null default 'created_paused',
  owner_token uuid,
  lease_until timestamptz,
  cursor bigint not null default 0,
  activated_at timestamptz,
  last_frame timestamptz,
  checkpoint jsonb not null default '{}',
  warmed boolean not null default false,
  frames bigint not null default 0,
  skipped_frames bigint not null default 0,
  late_revisions bigint not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp()
);
create index strategy_claim on public.strategy_runs(updated_at,id) where state='running';
create table public.strategy_members (
  run_id uuid not null references public.strategy_runs,
  series_id bigint not null references public.series,
  instrument_id uuid not null references public.instruments,
  symbol text not null,
  primary key(run_id,series_id),
  unique(run_id,instrument_id),
  unique(run_id,symbol)
);
create table public.strategy_frame_bars (
  run_id uuid not null references public.strategy_runs,
  bar_start timestamptz not null,
  series_id bigint not null,
  sequence bigint not null,
  envelope jsonb not null,
  primary key(run_id,bar_start,series_id)
);
create table public.strategy_signals (
  id uuid primary key,
  run_id uuid not null references public.strategy_runs,
  bar_start timestamptz not null,
  decision_at timestamptz not null,
  weights jsonb not null,
  inputs jsonb not null,
  missing_members integer not null,
  execution_eligible boolean not null,
  state_sha256 text not null,
  unique(run_id,bar_start)
);
do $$ begin
  if not exists(select from pg_roles where rolname='bagel_strategy') then
    create role bagel_strategy nologin;
  end if;
end $$;
grant usage on schema public to bagel_strategy;
grant select on public.series,public.sources,public.instruments,public.instrument_mappings,
  public.bars,public.bar_revisions,public.ingested_universe,public.publication_counter to bagel_strategy;
grant select,insert on public.event_journal,public.trading_commands to bagel_strategy;
grant select,insert,update on public.worker_heartbeats to bagel_strategy;
create policy journal_strategy on public.event_journal to bagel_strategy using(true) with check(true);
grant select,insert on public.event_journal to bagel_trader;
create policy journal_trader on public.event_journal to bagel_trader using(true) with check(true);
do $$ declare t text; begin
  foreach t in array array['strategy_runs','strategy_members','strategy_frame_bars','strategy_signals'] loop
    execute format('revoke all on public.%I from public',t);
    execute format('alter table public.%I enable row level security',t);
    execute format('create policy strategy_worker on public.%I to bagel_strategy using(true) with check(true)',t);
    execute format('grant select,insert,update on public.%I to bagel_strategy',t);
  end loop;
end $$;
grant delete on public.strategy_frame_bars to bagel_strategy;
grant select on public.strategy_runs to bagel_trader;
create policy trader_strategy_read on public.strategy_runs for select to bagel_trader using(true);
create policy strategy_command_insert on public.trading_commands for insert to bagel_strategy with check(action='target');
create policy strategy_command_read on public.trading_commands for select to bagel_strategy using(action='target');
