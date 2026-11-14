create index account_activity_order on public.account_activities(account_id,((payload->>'order_id')))
  where activity_type='FILL';
create index trading_audit_account on public.trading_audit(account_id,id);
revoke update on public.trading_audit,public.risk_decisions,public.ledger_entries from bagel_trader;
