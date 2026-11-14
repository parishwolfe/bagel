-- Broker transport evidence is separate from financial accounting. Snapshots are
-- not represented as fills and may never silently overwrite a strategy ledger.
create table public.paper_accounts (
  account_id uuid primary key,
  status text not null,
  cash numeric(38,12) not null,
  equity numeric(38,12) not null,
  buying_power numeric(38,12) not null,
  trading_blocked boolean not null,
  positions jsonb not null,
  orders jsonb not null,
  snapshot_at timestamptz not null,
  reconciled boolean not null default false,
  reconciliation_reason text not null default 'ledger_not_reconciled'
);
create table public.paper_fill_evidence (
  account_id uuid not null references public.paper_accounts,
  activity_id text not null,
  broker_order_id uuid not null,
  symbol text not null,
  side text not null check(side in ('buy','sell')),
  quantity numeric(38,12) not null check(quantity > 0),
  price numeric(38,12) not null check(price > 0),
  transaction_at timestamptz not null,
  primary key(account_id,activity_id)
);
create table public.paper_stream_evidence (
  event_id text primary key,
  account_id uuid not null references public.paper_accounts,
  broker_order_id uuid not null,
  event_type text not null,
  broker_timestamp timestamptz,
  observed_at timestamptz not null default clock_timestamp()
);
revoke all on public.paper_accounts, public.paper_fill_evidence, public.paper_stream_evidence from public;
