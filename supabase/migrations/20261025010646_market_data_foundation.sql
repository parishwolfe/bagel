-- Canonical source/feed-aware series. Run as a database owner, never as a frontend role.
create schema market_data;
create schema ingestion;
revoke all on schema market_data, ingestion from public;

do $$ begin
  if not exists (select from pg_roles where rolname = 'bagel_ingester') then
    create role bagel_ingester nologin;
  end if;
end $$;

create table market_data.sources (
  id bigint generated always as identity primary key,
  code text not null unique
);
create table market_data.instruments (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  asset_class text not null,
  currency text not null check (length(currency) = 3)
);
create table market_data.instrument_mappings (
  source_id bigint not null references market_data.sources,
  provider_instrument_id text not null,
  instrument_id uuid not null references market_data.instruments,
  symbol text not null,
  valid_from timestamptz not null default '-infinity',
  valid_to timestamptz,
  primary key (source_id, provider_instrument_id, valid_from),
  check (valid_to is null or valid_to > valid_from)
);
create unique index instrument_mappings_current_symbol
  on market_data.instrument_mappings(source_id, symbol) where valid_to is null;
create unique index instrument_mappings_current_provider_id
  on market_data.instrument_mappings(source_id, provider_instrument_id) where valid_to is null;
create index instrument_mappings_instrument on market_data.instrument_mappings(instrument_id);

create table market_data.series (
  id bigint generated always as identity primary key,
  source_id bigint not null references market_data.sources,
  instrument_id uuid not null references market_data.instruments,
  feed text not null,
  interval_seconds integer not null check (interval_seconds > 0),
  currency text not null check (length(currency) = 3),
  adjustment text not null,
  unique (source_id, instrument_id, feed, interval_seconds, currency, adjustment)
);
create index series_instrument on market_data.series(instrument_id);

create table market_data.bars (
  series_id bigint not null references market_data.series,
  bar_start timestamptz not null,
  open numeric(28,10) not null check (open > 0),
  high numeric(28,10) not null check (high > 0),
  low numeric(28,10) not null check (low > 0),
  close numeric(28,10) not null check (close > 0),
  volume numeric(28,8) not null check (volume >= 0),
  vwap numeric(28,10) check (vwap > 0),
  trade_count bigint check (trade_count >= 0),
  session text not null check (session in ('pre', 'regular', 'post')),
  origin text not null check (origin in ('live', 'live_updated', 'historical')),
  quality_rank smallint generated always as (
    case origin when 'historical' then 30 when 'live_updated' then 20 else 10 end
  ) stored,
  observed_at timestamptz not null,
  received_at timestamptz not null,
  ingested_at timestamptz not null default clock_timestamp(),
  primary key (series_id, bar_start),
  check (bar_start = date_trunc('minute', bar_start)),
  check (low <= least(open, close) and high >= greatest(open, close)),
  check (open::text <> 'NaN' and high::text <> 'NaN' and low::text <> 'NaN'
    and close::text <> 'NaN' and volume::text <> 'NaN' and vwap::text <> 'NaN')
) partition by range (bar_start);

create table ingestion.runs (
  id uuid primary key,
  mode text not null check (mode in ('backfill', 'reconcile')),
  symbols text[] not null,
  requested_start timestamptz not null,
  requested_end timestamptz not null,
  started_at timestamptz not null default clock_timestamp(),
  ended_at timestamptz,
  status text not null default 'running' check (status in ('running', 'complete', 'failed')),
  error_code text,
  check (requested_start < requested_end)
);
create table ingestion.ranges (
  series_id bigint not null references market_data.series,
  range_start timestamptz not null,
  range_end timestamptz not null,
  run_id uuid not null references ingestion.runs,
  status text not null default 'pending' check (status in ('pending', 'complete')),
  bar_count integer not null default 0 check (bar_count >= 0),
  completed_at timestamptz,
  primary key (series_id, range_start, range_end),
  check (range_start < range_end)
);
create index ranges_pending on ingestion.ranges(series_id, range_start)
  where status = 'pending';
create index ranges_run on ingestion.ranges(run_id);
create table ingestion.stream_state (
  stream_key text primary key,
  repair_cursor timestamptz not null,
  heartbeat_at timestamptz not null default clock_timestamp()
);

grant usage on schema market_data, ingestion to bagel_ingester;
grant select, insert, update on all tables in schema market_data, ingestion to bagel_ingester;
grant usage, select on all sequences in schema market_data, ingestion to bagel_ingester;

-- A narrowly scoped DDL capability: fixed parent, generated identifiers, no arbitrary SQL.
-- Private backend roles use database authentication, so auth.uid() is intentionally not used.
create function ingestion.ensure_bar_partitions(p_start timestamptz, p_end timestamptz)
returns void language plpgsql security definer set search_path = '' as $$
declare
  month_start timestamptz;
  month_end timestamptz;
  partition_name text;
begin
  if p_start is null or p_end is null or not isfinite(p_start) or not isfinite(p_end)
     or p_start >= p_end or p_end - p_start > interval '62 days' then
    raise exception 'Partition request must cover at most 62 days';
  end if;
  perform pg_advisory_xact_lock(74021801);
  month_start := date_trunc('month', p_start at time zone 'UTC') at time zone 'UTC';
  while month_start < p_end loop
    month_end := (month_start at time zone 'UTC' + interval '1 month') at time zone 'UTC';
    partition_name := 'bars_' || to_char(month_start at time zone 'UTC', 'YYYY_MM');
    if to_regclass('market_data.' || partition_name) is null then
      execute format('create table market_data.%I partition of market_data.bars '
        || 'for values from (%L) to (%L)', partition_name, month_start, month_end);
      execute format('revoke all on market_data.%I from public', partition_name);
      execute format('alter table market_data.%I enable row level security', partition_name);
      -- No direct partition grants: all ingestion access goes through the RLS-protected parent.
    end if;
    month_start := month_end;
  end loop;
end $$;
revoke all on function ingestion.ensure_bar_partitions(timestamptz, timestamptz) from public;
grant execute on function ingestion.ensure_bar_partitions(timestamptz, timestamptz)
  to bagel_ingester;

-- RLS is defense in depth even though these schemas are not exposed to the Data API.
do $$ declare t record; begin
  for t in select schemaname, tablename from pg_tables
    where schemaname in ('market_data', 'ingestion')
  loop
    execute format('alter table %I.%I enable row level security', t.schemaname, t.tablename);
    execute format('create policy backend_access on %I.%I to bagel_ingester '
      || 'using (true) with check (true)', t.schemaname, t.tablename);
  end loop;
end $$;

-- Create the current and next month. Backfill and worker writes also ensure their own ranges.
select ingestion.ensure_bar_partitions(now(), now() + interval '32 days');
