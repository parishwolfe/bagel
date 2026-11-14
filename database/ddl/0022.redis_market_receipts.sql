-- Redis IDs are delivery identities; immutable event IDs deduplicate market data.
create table public.market_stream_receipts (
  stream text not null,
  stream_id text not null,
  event_id uuid not null,
  persisted_at timestamptz not null default clock_timestamp(),
  primary key(stream,stream_id)
);
revoke all on public.market_stream_receipts from public;
alter table public.market_stream_receipts enable row level security;
grant select,insert on public.market_stream_receipts to bagel_ingester;
create policy ingester_stream_receipts on public.market_stream_receipts to bagel_ingester using(true) with check(true);
