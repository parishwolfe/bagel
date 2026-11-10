-- One owner-only marker proves the Supabase GitHub integration applied this migration.
-- Retained for inspection; remove it only with a subsequent migration.
create table public.bagel_cd_smoke_test (
  id smallint primary key check (id = 1),
  marker text not null,
  deployed_at timestamptz not null default clock_timestamp()
);
alter table public.bagel_cd_smoke_test enable row level security;
revoke all on public.bagel_cd_smoke_test from public;
-- Plain PostgreSQL Compose does not define Supabase's client roles.
do $$ declare client_role text; begin
  foreach client_role in array array['anon', 'authenticated'] loop
    if exists (select from pg_roles where rolname = client_role) then
      execute format('revoke all on public.bagel_cd_smoke_test from %I', client_role);
    end if;
  end loop;
end $$;
comment on table public.bagel_cd_smoke_test is
  'Supabase GitHub CD verification marker. RLS denies client access.';
insert into public.bagel_cd_smoke_test(id,marker)
values (1, 'supabase-github-cd:20261025031941');
