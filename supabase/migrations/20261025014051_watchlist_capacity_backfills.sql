-- Versioned watchlists are the persistent control plane. No frontend access is granted.
create table ingestion.watchlists (
  stream_key text primary key,
  symbols text[] not null check (cardinality(symbols) between 1 and 10000),
  revision bigint not null default 1,
  config_signature text,
  loaded_revision bigint,
  attempt_years integer check (attempt_years between 1 and 50),
  history_start timestamptz,
  history_end timestamptz,
  status text not null default 'pending' check (status in ('pending','loading','ready','failed')),
  updated_at timestamptz not null default clock_timestamp(),
  check (history_start is null or history_end is null or history_start < history_end)
);
create table ingestion.capacity_resets (
  id uuid primary key default gen_random_uuid(),
  stream_key text not null references ingestion.watchlists,
  revision bigint not null,
  from_years integer,
  to_years integer not null,
  database_bytes_before bigint not null,
  reset_at timestamptz not null default clock_timestamp()
);
create index capacity_resets_stream on ingestion.capacity_resets(stream_key);
alter table ingestion.watchlists enable row level security;
alter table ingestion.capacity_resets enable row level security;
create policy backend_access on ingestion.watchlists to bagel_ingester
  using (true) with check (true);
create policy backend_access on ingestion.capacity_resets to bagel_ingester
  using (true) with check (true);
grant select,insert,update on ingestion.watchlists,ingestion.capacity_resets to bagel_ingester;

-- This is the sole truncate capability granted to the writer. It is conditional on
-- measured capacity and refuses to remove another source/feed's market history.
create function ingestion.reset_history_for_capacity(
  p_stream_key text, p_revision bigint, p_next_years integer,
  p_start timestamptz, p_end timestamptz, p_budget_bytes bigint
) returns boolean language plpgsql security definer set search_path = '' as $$
declare
  current_row ingestion.watchlists%rowtype;
  database_bytes bigint;
begin
  if p_budget_bytes <= 0 or p_next_years not in (1,2,3)
    or p_start is null or p_end is null or p_start >= p_end then
    raise exception 'Invalid capacity reset request';
  end if;
  -- Writers take the shared lock, so no batch can straddle a reset/policy change.
  perform pg_advisory_xact_lock(74021803);
  select * into current_row from ingestion.watchlists
    where stream_key=p_stream_key for update;
  if not found or current_row.revision <> p_revision then
    return false;
  end if;
  database_bytes := pg_database_size(current_database());
  if database_bytes < p_budget_bytes then
    raise exception 'Capacity reset refused: database is below the configured budget';
  end if;
  if exists (
    select 1 from market_data.bars b
      join market_data.series s on s.id=b.series_id
      join market_data.sources src on src.id=s.source_id
      where src.code || ':' || s.feed <> p_stream_key
  ) then
    raise exception 'Capacity reset refused: other sources or feeds have stored bars';
  end if;
  truncate table market_data.bars;
  delete from ingestion.ranges r using market_data.series s, market_data.sources src
    where r.series_id=s.id and s.source_id=src.id and src.code || ':' || s.feed=p_stream_key;
  delete from ingestion.stream_state
    where starts_with(stream_key,p_stream_key || ':');
  insert into ingestion.capacity_resets
    (stream_key,revision,from_years,to_years,database_bytes_before)
    values (p_stream_key,p_revision,current_row.attempt_years,p_next_years,database_bytes);
  update ingestion.watchlists set attempt_years=p_next_years,
    history_start=p_start,history_end=p_end,loaded_revision=null,status='pending',
    updated_at=clock_timestamp() where stream_key=p_stream_key;
  return true;
end $$;
revoke all on function ingestion.reset_history_for_capacity(
  text,bigint,integer,timestamptz,timestamptz,bigint) from public;
grant execute on function ingestion.reset_history_for_capacity(
  text,bigint,integer,timestamptz,timestamptz,bigint) to bagel_ingester;

-- Mirror canonical-file checksums when migrations are applied via the Supabase
-- Management API. The Python migration runner uses the same metadata locally.
create schema if not exists bagel_meta;
revoke all on schema bagel_meta from public;
create table if not exists bagel_meta.migrations (
  version text primary key, checksum text not null,
  applied_at timestamptz not null default clock_timestamp()
);
