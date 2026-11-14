-- Entitlements are provider/account capabilities, not a permanent IEX universe cap.
create or replace function public.watchlist_changed()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  select array_agg(distinct upper(btrim(symbol)) order by upper(btrim(symbol)))
    into new.symbols from unnest(new.symbols) symbol where btrim(symbol) <> '';
  if new.symbols is null or (new.symbols <> array['*'] and exists (
    select 1 from unnest(new.symbols) symbol where symbol !~ '^[A-Z][A-Z0-9.-]{0,19}$'
  )) then
    raise exception 'Invalid US equity watchlist';
  end if;
  if tg_op='UPDATE' then
    if new.stream_key <> old.stream_key then
      raise exception 'Watchlist stream identity is immutable';
    end if;
    if new.symbols is distinct from old.symbols or (
      old.config_signature is not null and new.config_signature is distinct from old.config_signature
    ) then
      new.revision := old.revision + 1;
      new.attempt_years := null;
      new.status := 'pending';
      new.updated_at := clock_timestamp();
    else
      new.revision := old.revision;
    end if;
  end if;
  return new;
end $$;

create table public.ingested_universe (
  series_id bigint primary key references public.series,
  first_received_at timestamptz not null,
  last_received_at timestamptz not null
);
revoke all on public.ingested_universe from public;
grant select,insert,update on public.ingested_universe to bagel_ingester;
grant select on public.ingested_universe to bagel_research;
alter table public.ingested_universe enable row level security;
create policy universe_ingester on public.ingested_universe to bagel_ingester using(true) with check(true);
create policy universe_research on public.ingested_universe for select to bagel_research using(true);
