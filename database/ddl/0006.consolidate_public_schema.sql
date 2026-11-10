-- Forward-only consolidation. Apply once, with application workers stopped.
begin;
set local lock_timeout = '10s';

-- Table names and primary-key indexes share a namespace after the move.
alter table ingestion.runs rename to ingestion_runs;
alter table ingestion.ingestion_runs rename constraint runs_pkey to ingestion_runs_pkey;
alter table research.runs rename to research_runs;
alter table research.research_runs rename constraint runs_pkey to research_runs_pkey;

-- SET SCHEMA preserves data, ownership, grants, policies and dependencies, and
-- moves owned sequences/indexes. Partitions must also be moved individually.
do $$ declare relation record; begin
  for relation in
    select n.nspname, c.relname
    from pg_catalog.pg_class c
    join pg_catalog.pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('market_data', 'ingestion', 'research', 'bagel_meta')
      and c.relkind in ('r', 'p')
    order by c.relispartition, n.nspname, c.relname
  loop
    execute format('alter table %I.%I set schema public', relation.nspname, relation.relname);
  end loop;
end $$;

alter function ingestion.ensure_bar_partitions(timestamptz, timestamptz) set schema public;
alter function ingestion.reset_history_for_capacity(
  text, bigint, integer, timestamptz, timestamptz, bigint
) set schema public;
alter function ingestion.watchlist_changed() set schema public;

-- PL/pgSQL string bodies are not rewritten by SET SCHEMA. Future partitions
-- and maintenance must use public too, with the original bounded capabilities.
create or replace function public.ensure_bar_partitions(p_start timestamptz, p_end timestamptz)
returns void language plpgsql security definer set search_path = '' as $$
declare
  month_start timestamptz;
  month_end timestamptz;
  partition_name text;
  client_role text;
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
    if to_regclass('public.' || partition_name) is null then
      execute format('create table public.%I partition of public.bars '
        || 'for values from (%L) to (%L)', partition_name, month_start, month_end);
      execute format('revoke all on public.%I from public', partition_name);
      -- Hosted public-schema defaults may grant client roles direct table access.
      foreach client_role in array array['anon', 'authenticated'] loop
        if exists (select from pg_roles where rolname = client_role) then
          execute format('revoke all on public.%I from %I', partition_name, client_role);
        end if;
      end loop;
      execute format('alter table public.%I enable row level security', partition_name);
    end if;
    month_start := month_end;
  end loop;
end $$;

create or replace function public.reset_history_for_capacity(
  p_stream_key text, p_revision bigint, p_next_years integer,
  p_start timestamptz, p_end timestamptz, p_budget_bytes bigint
) returns boolean language plpgsql security definer set search_path = '' as $$
declare
  current_row public.watchlists%rowtype;
  database_bytes bigint;
begin
  if p_budget_bytes <= 0 or p_next_years not in (1,2,3)
    or p_start is null or p_end is null or p_start >= p_end then
    raise exception 'Invalid capacity reset request';
  end if;
  perform pg_advisory_xact_lock(74021803);
  select * into current_row from public.watchlists
    where stream_key=p_stream_key for update;
  if not found or current_row.revision <> p_revision then
    return false;
  end if;
  database_bytes := pg_database_size(current_database());
  if database_bytes < p_budget_bytes then
    raise exception 'Capacity reset refused: database is below the configured budget';
  end if;
  if exists (
    select 1 from public.bars b
      join public.series s on s.id=b.series_id
      join public.sources src on src.id=s.source_id
      where src.code || ':' || s.feed <> p_stream_key
  ) then
    raise exception 'Capacity reset refused: other sources or feeds have stored bars';
  end if;
  truncate table public.bars;
  delete from public.ranges r using public.series s, public.sources src
    where r.series_id=s.id and s.source_id=src.id and src.code || ':' || s.feed=p_stream_key;
  delete from public.stream_state
    where starts_with(stream_key,p_stream_key || ':');
  insert into public.capacity_resets
    (stream_key,revision,from_years,to_years,database_bytes_before)
    values (p_stream_key,p_revision,current_row.attempt_years,p_next_years,database_bytes);
  update public.watchlists set attempt_years=p_next_years,
    history_start=p_start,history_end=p_end,loaded_revision=null,status='pending',
    updated_at=clock_timestamp() where stream_key=p_stream_key;
  return true;
end $$;

grant usage on schema public to bagel_ingester, bagel_research;
revoke all on function public.ensure_bar_partitions(timestamptz, timestamptz) from public;
revoke all on function public.reset_history_for_capacity(
  text, bigint, integer, timestamptz, timestamptz, bigint
) from public;
revoke all on function public.watchlist_changed() from public;
grant execute on function public.ensure_bar_partitions(timestamptz, timestamptz) to bagel_ingester;
grant execute on function public.reset_history_for_capacity(
  text, bigint, integer, timestamptz, timestamptz, bigint
) to bagel_ingester;

-- Refuse unexpected remaining objects rather than deleting them with CASCADE.
drop schema market_data restrict;
drop schema ingestion restrict;
drop schema research restrict;
drop schema bagel_meta restrict;
commit;
