alter table public.experiments add column engine_id text;
create index experiment_engine_pending on public.experiments(engine_id,updated_at,id)
  where state='active' and generated_trials<total_trials;
