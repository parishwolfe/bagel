alter table public.order_intents add column signal_id uuid references public.strategy_signals;
alter table public.strategy_signals add column activation_at timestamptz;
alter table public.strategy_signals drop constraint strategy_signals_run_id_bar_start_key;
create unique index strategy_signal_activation on public.strategy_signals(run_id,activation_at,bar_start);
create table public.strategy_target_state (
  account_id uuid not null references public.trading_accounts,
  strategy_id uuid not null references public.strategy_runs,
  symbol text not null,
  activation_at timestamptz not null,
  weight numeric not null,
  quantity numeric(38,12) not null check(quantity=trunc(quantity)),
  sizing_signal_id uuid references public.strategy_signals,
  sizing_decision_id uuid not null,
  sizing_price numeric(38,12) not null,
  sizing_equity numeric(38,12) not null,
  primary key(account_id,strategy_id,symbol)
);
revoke all on public.strategy_target_state from public;
alter table public.strategy_target_state enable row level security;
grant select,insert,update on public.strategy_target_state to bagel_trader;
create policy trader_target_state on public.strategy_target_state to bagel_trader using(true) with check(true);
grant select on public.strategy_signals to bagel_trader;
create policy trader_signal_read on public.strategy_signals for select to bagel_trader using(true);
grant select on public.strategy_members,public.strategy_inputs to bagel_trader;
create policy trader_member_read on public.strategy_members for select to bagel_trader using(true);
create policy trader_input_read on public.strategy_inputs for select to bagel_trader using(true);
revoke update on public.strategy_signals from bagel_strategy;
grant select(account_id,attribution) on public.trading_accounts to bagel_strategy;
create policy strategy_portfolio_read on public.trading_accounts for select to bagel_strategy using(true);
grant select(account_id,strategy_id,payload,reserved_notional) on public.order_intents to bagel_strategy;
create policy strategy_reservation_read on public.order_intents for select to bagel_strategy using(true);
grant select on public.strategy_target_state to bagel_strategy;
create policy strategy_target_read on public.strategy_target_state for select to bagel_strategy using(true);
grant update(state,result,completed_at) on public.trading_commands to bagel_strategy;
create policy strategy_target_supersede on public.trading_commands for update to bagel_strategy
  using(action='target') with check(action='target');
create index trading_queued_targets on public.trading_commands
  (account_id,((payload->>'strategy_id')),((payload->>'symbol'))) where action='target' and state='queued';
create index strategy_pending_exposure on public.order_intents
  (account_id,strategy_id,((payload->>'symbol'))) where reserved_notional>0;
do $$ declare t text; begin
  foreach t in array array['sources','instruments','instrument_mappings','series','bars','bar_revisions'] loop
    execute format('create policy execution_market_read on public.%I for select to bagel_trader,bagel_strategy using(true)',t);
  end loop;
end $$;
create policy strategy_universe_read on public.ingested_universe for select to bagel_strategy using(true);
