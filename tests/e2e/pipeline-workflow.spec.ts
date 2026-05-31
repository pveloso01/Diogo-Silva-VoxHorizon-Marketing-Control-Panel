import { test, expect, getTestAdminClient } from "./_fixtures";
import { sessionCookieHeader } from "./_auth";
import { mockWorkerIdeation } from "./_mocks/sse-harness";
import { makeSquarePngBase64 } from "./_mocks/png-fixture";
import {
  assertWorkerHealthy,
  awaitWorkerStageClosed,
  awaitWorkItemEnqueued,
  emitGenerationClosure,
  readPipelineStatus,
  readStageAdvancedOrder,
  readStageStates,
  seedFinalCreatives,
  seedGenerationOpenMarker,
  waitForStatus,
  workerPost,
} from "./_mocks/workflow-driver";

/**
 * No-stall workflow e2e (T.5 / #318) + FIX-A (post-generation dispatch).
 *
 * FIX-A closed the confirmed deadlock: the post-generation per-creative stages
 * (creative_qa / compliance_review / spec_validation) had NO dispatch PRODUCER
 * once the pipeline left generation -- the verdict-writers were reachable only
 * by the manual routes / this harness, so every real pipeline deadlocked at
 * creative_qa. The fix wires a per-stage dispatch on entry:
 *   - the auto-advance trigger enqueues the creative_qa producer
 *     (worker_qa deterministic / operator_dispatch operator);
 *   - the advance route enqueues compliance_review (worker_compliance) +
 *     spec_validation (worker_spec) on entry (operator -> operator_dispatch);
 *   - the variant-plan/decision approve enqueues finalize_assets via
 *     operator_dispatch in BOTH modes (finalize is inherently operator-held --
 *     Drive MCP -- so deterministic pipelines hand off to the operator too).
 *
 * This file proves BOTH:
 *
 *   (A) DETERMINISTIC back-half -- the REAL worker-stage consumer runs the
 *       deterministic verdict-writers end-to-end (it does NOT POST the verdict
 *       endpoints -- that was the old cheat). config -> done, asserting each
 *       worker_qa/worker_compliance/worker_spec work_item was ENQUEUED by the
 *       dispatch-on-entry seam and CLOSED by the real scheduler drain (claim ->
 *       run verdict-writer in-process -> close), the gate cleared, and the
 *       advance succeeded.
 *
 *   (B) OPERATOR dispatch-on-entry -- an operator-driven pipeline reaching
 *       creative_qa enqueues operator_dispatch(creative_qa), and each advance
 *       enqueues the next operator_dispatch(stage) incl. finalize_assets. The
 *       real Hermes chat cannot run in CI (no live agent), so the ENQUEUE-ON-
 *       ENTRY seam is the thing under test (the daemon's chat is unit-tested
 *       separately); the dispatch enqueue is exactly the seam that was missing.
 */

const PNG_B64 = makeSquarePngBase64();

test.describe("pipeline -- FIX-A post-generation dispatch (image track)", () => {
  test("(A) deterministic back-half: real consumer runs QA/compliance/spec to done", async ({
    page,
    clientId,
  }) => {
    const admin = getTestAdminClient();
    await assertWorkerHealthy();

    // ===================================================================
    // configuration -> ideation (real kickoff + advance route)
    // ===================================================================
    await page.goto("/pipeline/new");
    await expect(page).toHaveURL(/\/pipeline\/[a-f0-9-]{36}$/);
    const pipelineId = page.url().match(/\/pipeline\/([a-f0-9-]{36})$/)?.[1];
    if (!pipelineId) throw new Error(`could not extract pipeline id from ${page.url()}`);
    await expect(page.getByText("Configuration", { exact: true }).first()).toBeVisible({
      timeout: 15_000,
    });

    const cfgSeed = await admin
      .from("pipelines")
      .update({
        client_id: clientId,
        config_draft: {
          image_payload: {
            service: "remodeling",
            budget: 5000,
            market: "Austin, TX",
            landing_page_url: "https://example.com/lp",
            offer_text: "Free planning consult",
          },
        } as never,
      })
      .eq("id", pipelineId);
    expect(cfgSeed.error, JSON.stringify(cfgSeed.error)).toBeNull();

    await expectAdvance(pipelineId, "ideation");

    // ===================================================================
    // ideation -> review
    // ===================================================================
    const seeded = await mockWorkerIdeation(page, { pipelineId, n: 3 });
    expect(seeded.image.length).toBe(3);
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText(/Picked:\s*0\s*of\s*3/)).toBeVisible({ timeout: 15_000 });

    const picksRes = await managerPost(pipelineId, "picks", { image: [seeded.image[0]!.id] });
    expect(picksRes.status, JSON.stringify(picksRes.body)).toBe(200);
    await expectAdvance(pipelineId, "review");

    // ===================================================================
    // review -> generation (API approve, then open the batch atomically)
    // ===================================================================
    const approve = await managerPost(pipelineId, "review/decision", { decision: "approved" });
    expect(approve.status, JSON.stringify(approve.body)).toBe(200);
    await seedGenerationOpenMarker(pipelineId, 2);
    expect(await readPipelineStatus(pipelineId)).toBe("generation");

    const { data: pipeRow } = await admin
      .from("pipelines")
      .select("image_brief_id")
      .eq("id", pipelineId)
      .maybeSingle();
    const imageBriefId = pipeRow?.image_brief_id;
    if (!imageBriefId) throw new Error("pipeline has no image_brief_id after review approve");

    // The configuration->ideation advance enqueued worker_ideation; prove the
    // real consumer claimed + closed it (the symmetric half PR-8 left unbuilt).
    const ideationClose = await awaitWorkerStageClosed(pipelineId, "worker_ideation");
    expect(ideationClose === null || ideationClose === "completed").toBeTruthy();

    // ===================================================================
    // generation -> creative_qa (AUTO trigger)
    // The final creative is seeded WITH a real PNG in Storage so the
    // deterministic worker_qa engine can download + adjudicate a real PASS.
    // ===================================================================
    const finals = await seedFinalCreatives({
      pipelineId,
      briefId: imageBriefId,
      count: 1,
      imageB64: PNG_B64,
    });
    await emitGenerationClosure({
      pipelineId,
      taskCount: 2,
      outcome: "done",
      alreadyOpened: true,
    });
    await waitForStatus(pipelineId, "creative_qa");
    const qaStates = (await readStageStates(pipelineId)).filter((s) => s.stage === "creative_qa");
    expect(qaStates.length).toBe(finals.length);

    // ===================================================================
    // creative_qa: FIX-A -- the auto-advance trigger enqueued worker_qa
    // (deterministic). Prove the REAL consumer claimed + ran qa_run in-process
    // + closed the row, the gate cleared, then advance.
    // ===================================================================
    const qaWi = await awaitWorkItemEnqueued(pipelineId, "worker_qa", "creative_qa");
    expect(qaWi.id).toBeTruthy();
    const qaClose = await awaitWorkerStageClosed(pipelineId, "worker_qa");
    expect(qaClose).toBe("completed");
    await waitForStageCleared(pipelineId, "creative_qa");
    await expectAdvance(pipelineId, "copy");

    // ===================================================================
    // copy (reordered: now BEFORE compliance): copy stays MANUAL (no worker).
    // The operator authors drafts (we drive it directly as the operator); the
    // manager approves >=3 via the manager routes (NOT the verdict endpoint cheat
    // -- /copy + /copy/decision ARE the manager surfaces). Then advance to
    // compliance, which screens this approved copy.
    // ===================================================================
    const creativeId = finals[0]!.id;
    for (let i = 1; i <= 3; i += 1) {
      const c = await workerPost("/work/pipeline/tools/copy", {
        pipeline_id: pipelineId,
        variants: [{ creative_id: creativeId, platform: "meta", variant_index: i, ...CLEAN_COPY }],
      });
      expect(c.status, JSON.stringify(c.body)).toBe(200);
    }
    const copyRows = await readCopyVariants(admin, pipelineId, creativeId);
    expect(copyRows.length).toBe(3);
    for (const row of copyRows) {
      const dec = await managerPost(pipelineId, "copy/decision", {
        id: row.id,
        decision: "approved",
      });
      expect(dec.status, JSON.stringify(dec.body)).toBe(200);
    }
    await expectAdvance(pipelineId, "compliance_review");

    // ===================================================================
    // compliance_review: FIX-A -- the advance route enqueued worker_compliance
    // on entry. It now runs over the FINAL approved copy (copy precedes
    // compliance). The deterministic engine runs RULES ONLY (empty
    // llm_candidates) over the clean creative -> PASS. Prove the consumer ran +
    // the gate cleared.
    // ===================================================================
    const compWi = await awaitWorkItemEnqueued(
      pipelineId,
      "worker_compliance",
      "compliance_review",
    );
    expect(compWi.id).toBeTruthy();
    const compClose = await awaitWorkerStageClosed(pipelineId, "worker_compliance");
    expect(compClose).toBe("completed");
    await waitForStageCleared(pipelineId, "compliance_review");
    await expectAdvance(pipelineId, "spec_validation");

    // ===================================================================
    // spec_validation: FIX-A -- the advance route enqueued worker_spec. The
    // deterministic consumer submits a feed placement per creative (image keeps
    // the pass; video would be backstop-probed). Prove the consumer ran.
    // ===================================================================
    const specWi = await awaitWorkItemEnqueued(pipelineId, "worker_spec", "spec_validation");
    expect(specWi.id).toBeTruthy();
    const specClose = await awaitWorkerStageClosed(pipelineId, "worker_spec");
    expect(specClose).toBe("completed");
    await waitForStageCleared(pipelineId, "spec_validation");
    await expectAdvance(pipelineId, "variant_plan");

    // ===================================================================
    // variant_plan -> finalize_assets (manager variant-plan/decision approve).
    // finalize is inherently operator-held (Drive MCP) in BOTH modes, so the
    // approve hands off to the operator: it enqueues operator_dispatch(
    // finalize_assets) even for this deterministic pipeline. We assert the
    // hand-off fires, then stand in for the operator claiming+running the
    // finalize tool below (no live hermes in CI), exactly as finalize is
    // operator-held by design.
    // ===================================================================
    const vp = await managerPost(pipelineId, "variant-plan/decision", { decision: "approved" });
    expect(vp.status, JSON.stringify(vp.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("finalize_assets");
    // The operator hand-off dispatch must be enqueued for deterministic too.
    await awaitWorkItemEnqueued(pipelineId, "operator_dispatch", "finalize_assets");

    // ===================================================================
    // finalize_assets -> launch_handoff (stand in for the operator running the
    // finalize tool it was just dispatched for, then manager advance)
    // ===================================================================
    const fin = await workerPost("/work/pipeline/tools/finalize_result", {
      pipeline_id: pipelineId,
      results: [
        {
          creative_id: creativeId,
          asset_name: "remodel_kitchen_v1_1x1",
          drive_folder_id: "fake-drive-folder",
          file_path_drive: "drive://fake/remodel_kitchen_v1_1x1.png",
          verified: true,
        },
      ],
    });
    expect(fin.status, JSON.stringify(fin.body)).toBe(200);
    await expectAdvance(pipelineId, "launch_handoff");

    // ===================================================================
    // launch_handoff -> monitor (HARD gate). After the reorder compliance runs
    // AFTER copy and stays cleared through to launch (no copy edit voids it), so
    // this compliance_run is just an idempotent re-assert of the clean pass over
    // the final copy before we record PAUSED-first + approve.
    // ===================================================================
    const compClear = await workerPost("/work/pipeline/tools/compliance_run", {
      pipeline_id: pipelineId,
      items: [{ creative_id: creativeId, copy_variant_id: copyRows[0]!.id, surface: "copy" }],
    });
    expect(compClear.status, JSON.stringify(compClear.body)).toBe(200);
    expect((compClear.body as { rollup: string }).rollup).toBe("passed");

    const record = await workerPost("/work/pipeline/tools/launch", {
      pipeline_id: pipelineId,
      approved_by: "e2e-manager",
      entities: [
        { kind: "campaign", meta_id: "fake-campaign-1", meta_payload: { status: "PAUSED" } },
        {
          kind: "ad",
          meta_id: "fake-ad-1",
          parent_meta_id: "fake-campaign-1",
          creative_id: creativeId,
          copy_variant_id: copyRows[0]!.id,
        },
      ],
    });
    expect(record.status, JSON.stringify(record.body)).toBe(200);
    expect((record.body as { preconditions: { ok: boolean } }).preconditions.ok).toBe(true);

    const launchOk = await managerPost(pipelineId, "launch/decision", {
      decision: "approved",
      confirm_paused_first: true,
      acknowledge_preconditions: true,
    });
    expect(launchOk.status, JSON.stringify(launchOk.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("monitor");

    // ===================================================================
    // monitor -> done
    // ===================================================================
    const mon = await workerPost("/work/pipeline/tools/monitor_result", {
      pipeline_id: pipelineId,
      results: [
        {
          campaign_id: "fake-campaign-1",
          window_days: 7,
          spend: 120.0,
          ghl_leads: 4,
          verdict: "keep",
          verdict_reason: "CPL within target",
        },
      ],
    });
    expect(mon.status, JSON.stringify(mon.body)).toBe(200);

    const monDec = await managerPost(pipelineId, "monitor/decision", {
      decision: "kill",
      campaign_id: "fake-campaign-1",
      notes: "wrap the test run",
    });
    expect(monDec.status, JSON.stringify(monDec.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("done");

    // Every stage_advanced fired in DAG order; reaching done proves no stall.
    const order = await readStageAdvancedOrder(pipelineId);
    const expectedOrder = [
      "ideation",
      "review",
      "generation",
      "creative_qa",
      "copy",
      "compliance_review",
      "spec_validation",
      "variant_plan",
      "finalize_assets",
      "launch_handoff",
      "monitor",
    ];
    const seen = order.filter((s) => expectedOrder.includes(s));
    const firstOccurrence: string[] = [];
    for (const s of seen) {
      if (!firstOccurrence.includes(s)) firstOccurrence.push(s);
    }
    expect(firstOccurrence).toEqual(expectedOrder);
    expect(await readPipelineStatus(pipelineId)).toBe("done");
  });

  test("(B) operator dispatch-on-entry: each post-gen stage enqueues operator_dispatch", async ({
    clientId,
  }) => {
    const admin = getTestAdminClient();
    await assertWorkerHealthy();

    // Build an OPERATOR-DRIVEN pipeline directly in generation (the front half
    // is the same as test A; here we focus on the operator dispatch SEAM). A
    // brief id is set so the trigger's QA-gate seed join resolves, and
    // config_draft.operator_driven=true routes every dispatch to the operator.
    const { data: brief, error: bErr } = await admin
      .from("briefs")
      .insert({
        client_id: clientId,
        brief_id_human: `OPB-${Date.now()}`,
        status: "posted",
        payload: { service: "remodeling", budget: 5000 } as never,
        posted_at: new Date().toISOString(),
      } as never)
      .select("id")
      .single();
    if (bErr || !brief) throw new Error(`operator brief insert failed: ${bErr?.message}`);
    const briefId = (brief as { id: string }).id;

    const { data: created, error } = await admin
      .from("pipelines")
      .insert({
        client_id: clientId,
        format_choice: "image",
        image_brief_id: briefId,
        config_draft: { operator_driven: true } as never,
        advanced_at: { generation: new Date().toISOString() },
      } as never)
      .select("id")
      .single();
    if (error || !created) throw new Error(`operator pipeline insert failed: ${error?.message}`);
    const opId = (created as { id: string }).id;

    // Drop the pipeline into generation via a stage_advanced cutoff event.
    await admin.from("pipeline_events").insert({
      pipeline_id: opId,
      kind: "stage_advanced",
      stage: "generation",
      payload: { from: "review" },
    });
    expect(await readPipelineStatus(opId)).toBe("generation");

    // Seed a final creative (no PNG needed -- the operator dispatch path never
    // runs the verdict engine in CI; only the enqueue is under test). This
    // operator pipeline never enqueued a worker_generation work_item (it was
    // created directly in generation, not via the review approve), so there is
    // no consumer race to win -- emit a self-contained balanced closure (no
    // open marker) so the closure predicate (done >= greatest(queued, running))
    // is met and the trigger advances generation -> creative_qa.
    await seedFinalCreatives({ pipelineId: opId, briefId, count: 1 });
    await emitGenerationClosure({ pipelineId: opId, taskCount: 2, outcome: "done" });
    await waitForStatus(opId, "creative_qa");

    // creative_qa: the auto-advance trigger enqueued operator_dispatch (NOT
    // worker_qa) because the pipeline is operator-driven.
    const qaDisp = await awaitWorkItemEnqueued(opId, "operator_dispatch", "creative_qa");
    expect(qaDisp.id).toBeTruthy();
    // The deterministic worker_qa kind must NOT have been enqueued.
    const det = await admin
      .from("work_item")
      .select("id")
      .eq("pipeline_id", opId)
      .eq("kind", "worker_qa" as never);
    expect((det.data ?? []).length).toBe(0);

    // Force the creative_qa gate clear (the daemon's chat would do this; we
    // simulate the cleared gate so the advance proceeds) and advance into copy
    // (reordered: copy now follows QA): assert operator_dispatch(copy) on entry.
    await forceStageCleared(admin, opId, "creative_qa");
    await advanceVia(opId, "copy");
    const copyDisp = await awaitWorkItemEnqueued(opId, "operator_dispatch", "copy");
    expect(copyDisp.id).toBeTruthy();

    // copy is the manager-approval stage -- the operator authors drafts via its
    // dispatch; the manager approves >=3 via the manager routes. Then advance to
    // compliance, which screens the approved copy.
    const opCreative = (await readStageStates(opId)).find((s) => s.stage === "creative_qa");
    expect(opCreative).toBeTruthy();
    const opCreativeId = opCreative!.creative_id;
    for (let i = 1; i <= 3; i += 1) {
      const c = await workerPost("/work/pipeline/tools/copy", {
        pipeline_id: opId,
        variants: [
          { creative_id: opCreativeId, platform: "meta", variant_index: i, ...CLEAN_COPY },
        ],
      });
      expect(c.status, JSON.stringify(c.body)).toBe(200);
    }
    const opCopyRows = await readCopyVariants(admin, opId, opCreativeId);
    for (const row of opCopyRows) {
      const dec = await managerPost(opId, "copy/decision", { id: row.id, decision: "approved" });
      expect(dec.status, JSON.stringify(dec.body)).toBe(200);
    }
    await advanceVia(opId, "compliance_review");
    // compliance entry -> operator_dispatch(compliance_review) (screens copy).
    const compDisp = await awaitWorkItemEnqueued(opId, "operator_dispatch", "compliance_review");
    expect(compDisp.id).toBeTruthy();

    await forceStageCleared(admin, opId, "compliance_review");
    await advanceVia(opId, "spec_validation");
    // spec_validation entry -> operator_dispatch(spec_validation).
    const specDisp = await awaitWorkItemEnqueued(opId, "operator_dispatch", "spec_validation");
    expect(specDisp.id).toBeTruthy();

    await forceStageCleared(admin, opId, "spec_validation");
    await advanceVia(opId, "variant_plan");

    // variant_plan -> finalize_assets via the manager decision route. FIX-A: the
    // approve path enqueues operator_dispatch(finalize_assets) (operator holds
    // the Drive MCP). This is the finalize dispatch seam under test.
    const vp = await managerPost(opId, "variant-plan/decision", { decision: "approved" });
    expect(vp.status, JSON.stringify(vp.body)).toBe(200);
    expect(await readPipelineStatus(opId)).toBe("finalize_assets");
    const finDisp = await awaitWorkItemEnqueued(opId, "operator_dispatch", "finalize_assets");
    expect(finDisp.id).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Helpers (manager API + small reads)
// ---------------------------------------------------------------------------

/** Clean, compliance-safe copy (no superlatives / financing / guarantee). */
const CLEAN_COPY = {
  headline: "Refresh your kitchen this season",
  primary_text: "Local remodeling pros ready to help you plan a remodel you will enjoy.",
  description: "Schedule a free planning consult with our team.",
  cta: "Learn more",
};

type ApiResult = { status: number; body: unknown };

/** POST to a manager Next API route (server-side gate). Uses the dev server. */
async function managerPost(pipelineId: string, path: string, body: unknown): Promise<ApiResult> {
  const base = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";
  const res = await fetch(`${base}/api/pipelines/${pipelineId}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await sessionCookieHeader()) },
    body: JSON.stringify(body),
  });
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    parsed = null;
  }
  return { status: res.status, body: parsed };
}

/** POST the generic advance route (no body). */
async function rawAdvance(pipelineId: string): Promise<ApiResult> {
  return managerPost(pipelineId, "advance", {});
}

/** Advance and assert the pipeline reached `want`. */
async function expectAdvance(pipelineId: string, want: string): Promise<void> {
  const res = await rawAdvance(pipelineId);
  expect(res.status, `advance to ${want} failed: ${JSON.stringify(res.body)}`).toBe(200);
  expect(await readPipelineStatus(pipelineId)).toBe(want);
}

/** Advance once and assert the post-advance status (no body). */
async function advanceVia(pipelineId: string, want: string): Promise<void> {
  await expectAdvance(pipelineId, want);
}

/** Read the copy_variants rows for a (pipeline, creative). */
async function readCopyVariants(
  admin: ReturnType<typeof getTestAdminClient>,
  pipelineId: string,
  creativeId: string,
): Promise<Array<{ id: string; status: string | null }>> {
  const { data } = await admin
    .from("copy_variants")
    .select("id, status")
    .eq("pipeline_id", pipelineId)
    .eq("creative_id", creativeId)
    .order("variant_index", { ascending: true });
  return (data ?? []) as Array<{ id: string; status: string | null }>;
}

/**
 * Poll until every in-scope creative_stage_state row for `stage` is terminal-
 * good (passed | overridden | skipped) -- the gate-cleared condition the advance
 * route hard-checks. Proves the worker consumer's verdict landed.
 */
async function waitForStageCleared(
  pipelineId: string,
  stage: string,
  opts: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<void> {
  const timeoutMs = opts.timeoutMs ?? 20_000;
  const intervalMs = opts.intervalMs ?? 400;
  const deadline = Date.now() + timeoutMs;
  const good = new Set(["passed", "overridden", "skipped"]);
  let last = "";
  while (Date.now() < deadline) {
    const rows = (await readStageStates(pipelineId)).filter((s) => s.stage === stage);
    last = rows.map((r) => r.status).join(",");
    if (rows.length > 0 && rows.every((r) => good.has(r.status))) return;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(
    `waitForStageCleared: ${stage} for pipeline ${pipelineId} never cleared ` +
      `(last states: [${last}]) -- the deterministic worker consumer did not clear the gate.`,
  );
}

/**
 * Force every in-scope creative_stage_state row for `stage` to `passed` (the
 * operator daemon's chat would do this in production; the real Hermes chat
 * cannot run in CI, so test B simulates the cleared gate to exercise the
 * dispatch-on-entry seam at the NEXT advance). Inserts a row when none exists.
 */
async function forceStageCleared(
  admin: ReturnType<typeof getTestAdminClient>,
  pipelineId: string,
  stage: string,
): Promise<void> {
  const rows = (await readStageStates(pipelineId)).filter((s) => s.stage === stage);
  if (rows.length === 0) {
    // Seed from the creative_qa rows (the trigger seeds those first).
    const qa = (await readStageStates(pipelineId)).filter((s) => s.stage === "creative_qa");
    for (const r of qa) {
      await admin.from("creative_stage_state").insert({
        pipeline_id: pipelineId,
        creative_id: r.creative_id,
        stage: stage as never,
        status: "passed",
      } as never);
    }
    return;
  }
  await admin
    .from("creative_stage_state")
    .update({ status: "passed" } as never)
    .eq("pipeline_id", pipelineId)
    .eq("stage", stage as never);
}
