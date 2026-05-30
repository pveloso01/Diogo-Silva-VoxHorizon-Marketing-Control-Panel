-- 0059_generation_close_plan_floor.sql
-- ----------------------------------------------------------------------------
-- FIX: generation auto-advance fired after the FIRST rendered final, abandoning
-- the rest of generation.
--
-- THE BUG. ``pipeline_events_auto_advance_done()`` (0024 -> 0046 -> 0051 -> 0054)
-- closes ``generation`` with the heuristic
--     v_expected := greatest(v_queued, v_running);
--     advance when (v_done + v_error) >= v_expected and v_done >= 1.
-- It assumes a BATCH renderer that emits every task_queued/task_running up front
-- (Ekko's pattern), so v_running == N when the first task_done lands. But the
-- OPERATOR renders finals SERIALLY via codex -- running#1, done#1, running#2,
-- done#2, ... -- and emits NO up-front task_queued. So at the first done,
-- greatest(queued=0, running=1) == 1, (done=1) >= 1, and the pipeline advanced
-- to creative_qa after a SINGLE asset. The remaining picked concepts' finals
-- never entered generation (verified live on pipeline e087bbe1: 4 picks => 8
-- finals expected, advanced after rendering 1; only concept #1's 1x1+9x16 made
-- it, and the creative_qa gate seeded only those two).
--
-- THE FIX. Floor v_expected at the KNOWN plan size: each picked IMAGE concept
-- yields 2 finals (1x1 + 9x16 -- mirrors ``_KIND_PARAMS['final']['ratios']`` in
-- worker/src/routes/pipeline_tools.py + the operator helper). The floor only
-- ever RAISES v_expected, so a batch renderer (Ekko/Kie, which emit up-front)
-- reaches its queued/running count exactly as before, and video / manual
-- pipelines (no image picks -> floor 0) are unaffected. A serial operator render
-- now waits for all planned finals before the close fires, so every final exists
-- when the creative_qa gate is seeded.
--
-- Everything else in the function -- the cutoff, the all-failed guard, the
-- idempotent already-advanced check, the image+video QA-gate seeding, the
-- stage_advanced emission, and the operator_dispatch/worker_qa producer enqueue
-- -- is byte-identical to 0054. Forward-only create-or-replace; the existing
-- trigger keeps calling it. search_path re-pinned per the 0029 convention.
-- ----------------------------------------------------------------------------

create or replace function pipeline_events_auto_advance_done()
  returns trigger
  language plpgsql
  set search_path = public, pg_temp
as $$
declare
  v_cutoff_id uuid;
  v_cutoff_ts timestamptz;
  v_queued bigint;
  v_running bigint;
  v_done bigint;
  v_error bigint;
  v_expected bigint;
  v_pick_image bigint;
  v_pipeline_status pipeline_status_enum;
  v_already_advanced int;
  v_now timestamptz := now();
  v_operator_driven boolean;
begin
  if new.kind not in ('task_done', 'task_error') then
    return new;
  end if;
  if new.stage is distinct from 'generation' then
    return new;
  end if;

  v_pipeline_status := compute_pipeline_status(new.pipeline_id);
  if v_pipeline_status is null or v_pipeline_status <> 'generation' then
    return new;
  end if;

  select id, created_at into v_cutoff_id, v_cutoff_ts
    from pipeline_events
   where pipeline_id = new.pipeline_id
     and kind = 'stage_advanced'
     and stage = 'generation'
   order by created_at desc, id desc
   limit 1;
  if v_cutoff_id is null then
    return new;
  end if;

  select
      count(*) filter (where kind = 'task_queued'),
      count(*) filter (where kind = 'task_running'),
      count(*) filter (where kind = 'task_done'),
      count(*) filter (where kind = 'task_error')
    into v_queued, v_running, v_done, v_error
    from pipeline_events
   where pipeline_id = new.pipeline_id
     and stage = 'generation'
     and id <> v_cutoff_id
     and created_at >= v_cutoff_ts;

  -- Plan-size floor (0059): each picked image concept yields 2 finals
  -- (1x1 + 9x16). The serial operator render emits no up-front task_queued, so
  -- without this floor greatest(queued,running) == 1 at the first done and the
  -- close fired after a single asset. picks->'image' is null for video/manual
  -- pipelines -> coalesce -> 0 -> floor inert (falls back to queued/running).
  select coalesce(jsonb_array_length(picks->'image'), 0) into v_pick_image
    from pipelines
   where id = new.pipeline_id;

  -- Closure heuristic (Ekko emits queued+running+done; operator emits
  -- running+done serially -> floored at the picked-concept plan size).
  v_expected := greatest(
    coalesce(v_queued, 0),
    coalesce(v_running, 0),
    coalesce(v_pick_image, 0) * 2
  );
  -- Not closed yet, OR an all-failed batch (v_done = 0) which must NOT advance.
  if v_expected = 0 or (v_done + v_error) < v_expected or v_done < 1 then
    return new;
  end if;

  select count(*) into v_already_advanced
    from pipeline_events
   where pipeline_id = new.pipeline_id
     and kind = 'stage_advanced'
     and stage = 'creative_qa'
     and created_at >= v_cutoff_ts;
  if v_already_advanced > 0 then
    return new;
  end if;

  update pipelines
     set advanced_at = coalesce(advanced_at, '{}'::jsonb)
                       || jsonb_build_object('creative_qa', to_jsonb(v_now)),
         updated_at = v_now
   where id = new.pipeline_id;

  -- Seed the per-creative QA gate for each final IMAGE creative (idempotent).
  insert into creative_stage_state (pipeline_id, creative_id, stage, status)
  select p.id, c.id, 'creative_qa', 'pending'
    from pipelines p
    join creatives c
      on c.brief_id = p.image_brief_id
     and c.type = 'image'
     and c.version like 'v1%'
     and c.deleted_at is null
   where p.id = new.pipeline_id
  on conflict (creative_id, stage) do nothing;

  -- Seed the per-creative QA gate for each final VIDEO creative (idempotent).
  insert into creative_stage_state (pipeline_id, creative_id, stage, status)
  select p.id, vc.id, 'creative_qa', 'pending'
    from pipelines p
    join video_creatives vc
      on vc.brief_id = p.video_brief_id
     and vc.status = 'captioned'
     and vc.deleted_at is null
   where p.id = new.pipeline_id
  on conflict (creative_id, stage) do nothing;

  insert into pipeline_events (pipeline_id, kind, stage, payload)
  values (
    new.pipeline_id,
    'stage_advanced',
    'creative_qa',
    jsonb_build_object(
      'reason', 'auto_advance',
      'from', 'generation',
      'task_done_count', v_done,
      'task_error_count', v_error
    )
  );

  select (config_draft->>'operator_driven')::boolean
    into v_operator_driven
    from pipelines
   where id = new.pipeline_id;

  if v_operator_driven is true then
    insert into work_item (kind, pipeline_id, payload, idempotency_key, created_by, status)
    values (
      'operator_dispatch',
      new.pipeline_id,
      jsonb_build_object(
        'stage', 'creative_qa',
        'instruction',
        'Run the QA pass on each final for pipeline ' || new.pipeline_id::text
          || ': pass/fail with defects, flag re-renders, then stop for the manager''s QA sign-off.'
      ),
      'op-disp:' || new.pipeline_id::text || ':creative_qa:auto',
      'trigger:auto_advance',
      'queued'
    )
    on conflict (idempotency_key) do nothing;
  else
    insert into work_item (kind, pipeline_id, payload, idempotency_key, created_by, status)
    values (
      'worker_qa',
      new.pipeline_id,
      jsonb_build_object('stage', 'creative_qa'),
      'wi:' || new.pipeline_id::text || ':creative_qa',
      'trigger:auto_advance',
      'queued'
    )
    on conflict (idempotency_key) do nothing;
  end if;

  return new;
end;
$$;
