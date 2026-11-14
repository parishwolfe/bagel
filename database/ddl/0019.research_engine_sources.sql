create table public.research_engines (
  id text primary key check(id ~ '^[a-f0-9]{64}$'),
  metadata jsonb not null,
  sources jsonb not null,
  source_sha256 text not null check(source_sha256 ~ '^[a-f0-9]{64}$'),
  created_at timestamptz not null default clock_timestamp()
);
revoke all on public.research_engines from public;
alter table public.research_engines enable row level security;
grant select,insert on public.research_engines to bagel_research;
create policy research_engine_worker on public.research_engines to bagel_research using(true) with check(true);
