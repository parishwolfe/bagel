-- Direct SQL/Studio/API edits receive the same revision semantics as the CLI.
create function ingestion.watchlist_changed()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  select array_agg(distinct upper(btrim(symbol)) order by upper(btrim(symbol)))
    into new.symbols from unnest(new.symbols) symbol where btrim(symbol) <> '';
  if new.symbols is null or exists (
    select 1 from unnest(new.symbols) symbol where symbol !~ '^[A-Z][A-Z0-9.-]{0,19}$'
  ) or (new.stream_key='alpaca:iex' and cardinality(new.symbols)>30) then
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
revoke all on function ingestion.watchlist_changed() from public;
create trigger watchlist_changed before insert or update on ingestion.watchlists
  for each row execute function ingestion.watchlist_changed();
