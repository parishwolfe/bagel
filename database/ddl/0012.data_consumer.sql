create table public.data_consumer_state (
  consumer text primary key,
  event_count bigint not null default 0 check(event_count >= 0),
  last_sequence bigint not null default 0,
  updated_at timestamptz not null default clock_timestamp()
);
revoke all on public.data_consumer_state from public;
