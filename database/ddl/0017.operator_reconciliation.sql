alter table public.trading_commands drop constraint trading_commands_action_check;
alter table public.trading_commands add constraint trading_commands_action_check
  check(action in ('arm','pause','order','cancel','target','reconcile'));
