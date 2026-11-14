-- Additive: the existing native-partitioned bars and all historical data remain intact.
create extension if not exists timescaledb;

create table public.bar_revisions (
  bar_start timestamptz not null,
  series_id bigint not null references public.series,
  event_id uuid not null,
  available_at timestamptz not null,
  observed_at timestamptz not null,
  envelope jsonb not null,
  primary key (bar_start, series_id, event_id),
  check (jsonb_typeof(envelope) = 'object')
);
select create_hypertable('public.bar_revisions', by_range('bar_start', interval '7 days'));
select add_dimension('public.bar_revisions', by_hash('series_id', 8));
create index bar_revisions_available on public.bar_revisions (series_id, available_at, event_id);
alter table public.bars add column revision_id uuid;

-- Writers do NOT assign a sequence. The publisher assigns it under a transaction
-- lock only after observing the committed row. This prevents commit-order holes.
create table public.event_journal (
  event_id uuid primary key,
  topic text not null,
  partition_key text not null,
  body jsonb not null,
  sequence bigint unique,
  created_at timestamptz not null default clock_timestamp(),
  published_at timestamptz,
  check (sequence is null or sequence > 0)
);
create index event_journal_unsequenced on public.event_journal(created_at, event_id)
  where sequence is null;
create index event_journal_unpublished on public.event_journal(sequence)
  where published_at is null and sequence is not null;
create table public.publication_counter (
  singleton boolean primary key default true check (singleton),
  last_sequence bigint not null default 0 check (last_sequence >= 0)
);
insert into public.publication_counter default values;
create table public.consumer_checkpoints (
  consumer text primary key,
  last_sequence bigint not null default 0 check (last_sequence >= 0),
  updated_at timestamptz not null default clock_timestamp()
);
create table public.worker_heartbeats (
  worker text primary key,
  process_id uuid not null,
  status text not null check (status in ('ready','disabled','blocked','stopping')),
  details jsonb not null default '{}',
  updated_at timestamptz not null default clock_timestamp()
);

-- No automatic journal/revision retention: consumer recovery and pinned research
-- inputs must be accounted for before any future pruning policy is authorized.
revoke all on public.bar_revisions, public.event_journal, public.publication_counter,
  public.consumer_checkpoints, public.worker_heartbeats from public;
grant select, insert on public.bar_revisions, public.event_journal to bagel_ingester;
grant select on public.bar_revisions, public.event_journal to bagel_research;
alter table public.bar_revisions enable row level security;
create policy revision_ingester on public.bar_revisions to bagel_ingester using (true) with check (true);
create policy revision_research on public.bar_revisions for select to bagel_research using (true);
alter table public.event_journal enable row level security;
create policy journal_ingester on public.event_journal to bagel_ingester using (true) with check (true);
create policy journal_research on public.event_journal for select to bagel_research using (true);
