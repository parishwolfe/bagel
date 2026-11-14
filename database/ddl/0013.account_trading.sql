create table public.trading_accounts (
  account_id uuid primary key,
  owner_token uuid not null,
  epoch bigint not null default 1,
  paused boolean not null default true,
  run_id uuid,
  armed_until timestamptz,
  policy jsonb not null,
  reconciled boolean not null default false,
  reason text not null default 'startup_reconciliation',
  opening_cash numeric(38,12),
  cash numeric(38,12) not null default 0,
  realized_pnl numeric(38,12) not null default 0,
  fees numeric(38,12) not null default 0,
  positions jsonb not null default '{}',
  attribution jsonb not null default '{}',
  equity_peak numeric(38,12),
  session_equity numeric(38,12),
  session_date date,
  reconciled_at timestamptz,
  snapshot jsonb not null default '{}',
  stream_connected boolean not null default false,
  last_activity_time timestamptz,
  last_activity_id text,
  updated_at timestamptz not null default clock_timestamp()
);
create table public.trading_commands (
  id uuid primary key,
  request_hash text not null,
  account_id uuid not null,
  action text not null check(action in ('arm','pause','order','cancel')),
  payload jsonb not null,
  state text not null default 'queued' check(state in ('queued','complete','rejected')),
  result jsonb,
  created_at timestamptz not null default clock_timestamp(),
  completed_at timestamptz
);
create index trading_commands_pending on public.trading_commands(account_id,created_at,id) where state='queued';
create table public.order_intents (
  id uuid primary key,
  account_id uuid not null references public.trading_accounts,
  decision_id uuid not null,
  strategy_id uuid,
  run_id uuid not null,
  client_order_id text not null unique,
  decision_at timestamptz not null,
  expires_at timestamptz not null,
  payload jsonb not null,
  inputs jsonb not null,
  state text not null check(state in ('admitted','rejected','dispatching','unresolved','accepted','terminal','expired')),
  reserved_notional numeric(38,12) not null default 0 check(reserved_notional>=0),
  dispatch_count integer not null default 0 check(dispatch_count between 0 and 1),
  dispatched_at timestamptz,
  broker_order_id uuid,
  reason text,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique(account_id,decision_id)
);
create index order_intents_active on public.order_intents(account_id,state)
  where state not in ('rejected','terminal','expired');
create table public.risk_decisions (
  intent_id uuid primary key references public.order_intents,
  approved boolean not null,
  policy_version text not null,
  policy jsonb not null,
  inputs jsonb not null,
  reason text not null,
  reservation numeric(38,12) not null,
  created_at timestamptz not null default clock_timestamp()
);
create table public.broker_orders (
  id uuid primary key,
  account_id uuid not null references public.trading_accounts,
  root_intent uuid references public.order_intents,
  client_order_id text not null,
  symbol text not null,
  state text not null,
  quantity numeric(38,12),
  filled_quantity numeric(38,12) not null,
  updated_at timestamptz not null,
  payload jsonb not null
);
create index broker_orders_account on public.broker_orders(account_id,state);
create table public.account_activities (
  account_id uuid not null references public.trading_accounts,
  activity_id text not null,
  activity_type text not null,
  effective_at timestamptz not null,
  payload jsonb not null,
  payload_sha256 text not null,
  applied boolean not null default false,
  primary key(account_id,activity_id)
);
create index account_activities_pending on public.account_activities(account_id,effective_at,activity_id)
  where not applied;
create table public.broker_sync_checkpoints (
  account_id uuid primary key references public.trading_accounts,
  activity_cursor text,
  activity_after timestamptz,
  initial_activities_complete boolean not null default false,
  order_cursor text,
  initial_orders_complete boolean not null default false
);
create table public.trading_audit (
  id bigint generated always as identity primary key,
  account_id uuid not null references public.trading_accounts,
  event_id uuid not null unique,
  event_type text not null,
  body jsonb not null,
  created_at timestamptz not null default clock_timestamp()
);
create table public.ledger_entries (
  account_id uuid not null references public.trading_accounts,
  activity_id text not null,
  attribution text not null,
  symbol text,
  quantity numeric(38,12) not null,
  price numeric(38,12) not null,
  cash_delta numeric(38,12) not null,
  realized_pnl numeric(38,12) not null,
  effective_at timestamptz not null,
  primary key(account_id,activity_id)
);
do $$ begin
  if not exists(select from pg_roles where rolname='bagel_trader') then
    create role bagel_trader nologin;
  end if;
end $$;
grant usage on schema public to bagel_trader;
grant select on public.series,public.sources,public.instruments,public.instrument_mappings,
  public.bars,public.bar_revisions to bagel_trader;
do $$ declare t text; begin
  foreach t in array array['trading_accounts','trading_commands','order_intents','risk_decisions',
    'broker_orders','broker_sync_checkpoints','account_activities','trading_audit','ledger_entries'] loop
    execute format('revoke all on public.%I from public',t);
    execute format('alter table public.%I enable row level security',t);
    execute format('create policy account_worker on public.%I to bagel_trader using(true) with check(true)',t);
    execute format('grant select,insert,update on public.%I to bagel_trader',t);
  end loop;
end $$;
grant usage,select on sequence public.trading_audit_id_seq to bagel_trader;
