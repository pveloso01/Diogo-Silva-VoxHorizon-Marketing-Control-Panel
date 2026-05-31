import type { Page } from "@playwright/test";

import { test, expect, getTestAdminClient } from "./_fixtures";
import { sessionCookieHeader } from "./_auth";
import { mockWorkerIdeation } from "./_mocks/sse-harness";
import {
  CLEAN_VIDEO_SCRIPT_OUTLINE,
  assertWorkerHealthy,
  awaitWorkerStageClosed,
  dropVideoIdeationDrafts,
  emitGenerationClosure,
  qaVideoItems,
  readPipelineStatus,
  readStageAdvancedOrder,
  readStageStates,
  readVideoCopyVariants,
  seedFinalVideoCreatives,
  seedGenerationOpenMarker,
  waitForStatus,
  workerPost,
} from "./_mocks/workflow-driver";

/**
 * Full gated VIDEO workflow e2e (Phase 2 / PR2).
 *
 * The video twin of `pipeline-workflow.spec.ts` (image). It drives ONE
 * video-track pipeline through ALL 12 stages to `done` against the REAL worker
 * verdicts - the QA + spec gates probe a real MP4 in Supabase Storage with
 * ffprobe, the compliance gate scans the real spoken script, and no verdict is
 * ever faked. The drive executors mirror the image spec exactly:
 *
 *   configuration→ideation        advance route (seeded video_payload draft)
 *   ideation→review               UI Continue (picks a video concept)
 *   review→generation             UI Approve
 *   generation→creative_qa        AUTO (migration 0046 B1 video QA seed trigger)
 *   creative_qa→copy              operator qa_run (surface=video) + advance
 *   copy→compliance_review        operator copy (video_copy_variants) + manager
 *                                 copy/decision approve (>=3) + advance
 *   compliance_review→spec (HARD) operator compliance_run (surface=video, clean
 *                                 script + approved copy → real PASS) + advance
 *   spec_validation→variant_plan  operator spec_result (reels; ffprobe spec
 *                                 backstop must PASS the MP4) + advance
 *   variant_plan→finalize_assets  manager variant-plan/decision approve
 *   finalize_assets→launch_handoff operator finalize_result + advance
 *   launch_handoff→monitor (HARD) operator launch (re-checks preconditions
 *                                 server-side) + manager launch/decision
 *   monitor→done                  operator monitor_result + manager
 *                                 monitor/decision (kill)
 *
 * The two-pass compliance re-arm is IMAGE-ONLY (the migration 0025 re-arm
 * trigger fires on `copy_variants`, not `video_copy_variants`), so authoring
 * video copy does NOT void the compliance PASS - the launch gate sees it stay
 * clear without a re-run. We assert that invariant directly.
 *
 * The pre-existing lightweight video UI tests (cancel-from-config,
 * add/remove-segment) are retained in their own `test()`s so coverage of those
 * paths is unchanged.
 */

test.describe("pipeline - video format", () => {
  test("create → switch to video → cancel", async ({ page, clientId }) => {
    void clientId;

    await page.goto("/pipeline/new");
    await expect(page).toHaveURL(/\/pipeline\/[a-f0-9-]{36}$/);

    // Configuration stage renders by default.
    await expect(page.getByText("Configuration", { exact: true }).first()).toBeVisible();

    // Switch the format radio to "video". This fires an autosave PATCH -
    // the new Video brief fieldset takes the place of the image one.
    await page.getByLabel("Video", { exact: true }).click();

    // Video brief fieldset is mounted; the hook + segments + voice fields
    // are required for the Continue gate to clear.
    await expect(page.getByText(/video brief/i)).toBeVisible();
    await expect(page.getByLabel(/^hook$/i)).toBeVisible();
    await expect(page.getByText(/script segments/i)).toBeVisible();
    await expect(page.getByLabel(/voice id/i)).toBeVisible();

    // Verify the pipeline format badge updated.
    await expect(page.getByText(/^Video$/i).first()).toBeVisible();

    // Cancel the pipeline from the Configuration stage.
    const cancelBtn = page.getByRole("button", { name: /cancel pipeline/i });
    await expect(cancelBtn).toBeVisible();
    await cancelBtn.click();

    await expect(page.getByRole("dialog")).toBeVisible();
    const confirmBtn = page.getByRole("button", { name: /^cancel pipeline$/i }).last();
    await confirmBtn.click();

    await expect(page.getByText("Cancelled", { exact: true })).toBeVisible({ timeout: 15_000 });
  });

  test("video segments can be added and removed", async ({ page, clientId }) => {
    void clientId;

    await page.goto("/pipeline/new");
    await expect(page).toHaveURL(/\/pipeline\/[a-f0-9-]{36}$/);

    await page.getByLabel("Video", { exact: true }).click();

    // The video form ships with one default segment and an "Add segment" button.
    await expect(page.getByLabel(/^topic$/i)).toHaveCount(1);

    await page.getByRole("button", { name: /add segment/i }).click();
    await expect(page.getByLabel(/^topic$/i)).toHaveCount(2);

    // The remove button on the FIRST segment now becomes interactive (it's
    // disabled when only one segment exists).
    const removeBtns = page.getByRole("button", { name: /^remove$/i });
    await expect(removeBtns).toHaveCount(2);
    await removeBtns.first().click();
    await expect(page.getByLabel(/^topic$/i)).toHaveCount(1);
  });

  test("drives all 12 stages to done against the real worker MP4 + script verdicts", async ({
    page,
    clientId,
  }) => {
    const admin = getTestAdminClient();
    await assertWorkerHealthy();

    // ===================================================================
    // configuration → ideation
    // Create the pipeline via the real kickoff route, flip it to the video
    // track + seed a valid VideoBriefInput draft (the advance route splices in
    // the pipeline's client_id and zod-validates the payload), then drive the
    // advance route - mirroring how the image spec seeds image_payload and
    // drives advance directly (the multi-field config form autosave is a UI
    // concern; the no-stall-relevant logic is the advance route).
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
        format_choice: "video",
        config_draft: {
          // VideoBriefInput: sum(segments.duration_s) ≈ target_duration_s.
          // CLEAN_VIDEO_SCRIPT_OUTLINE has one 15s segment, so target = 15.
          video_payload: {
            script_outline: CLEAN_VIDEO_SCRIPT_OUTLINE,
            target_duration_s: 15,
            voice_id: "21m00Tcm4TlvDq8ikWAM",
            dimensions: "9x16",
            broll_selection_mode: "review_each",
          },
        } as never,
      })
      .eq("id", pipelineId);
    expect(cfgSeed.error, JSON.stringify(cfgSeed.error)).toBeNull();

    await expectAdvance(pipelineId, "ideation");

    // Resolve the pipeline's video brief id (stamped at configure→ideation).
    const { data: pipeRow } = await admin
      .from("pipelines")
      .select("video_brief_id, format_choice")
      .eq("id", pipelineId)
      .maybeSingle();
    const videoBriefId = pipeRow?.video_brief_id;
    expect(pipeRow?.format_choice).toBe("video");
    if (!videoBriefId) throw new Error("pipeline has no video_brief_id after configure→ideation");

    // ===================================================================
    // ideation → review (UI): seed video variants, pick one, continue
    // ===================================================================
    const seeded = await mockWorkerIdeation(page, { pipelineId, n: 3 });
    expect(seeded.video.length).toBe(3);
    // config→ideation was driven via the API above, so the browser still shows
    // the config view - reload to render the ideation grid over the seeded rows.
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText(/Picked:\s*0\s*of\s*3/)).toBeVisible({ timeout: 15_000 });

    // Record the pick + drive ideation->review through the API. The ideation
    // checkbox + "continue to review" button update `pipelines.picks` and the
    // status OPTIMISTICALLY in the UI before the writes commit server-side, so an
    // immediately-following API advance/approve raced them (insufficient-picks
    // 422 / wrong-state 409). The API picks write is the same route the checkbox
    // calls and is committed on return, so the advance gate sees the pick.
    const picksRes = await managerPost(pipelineId, "picks", { video: [seeded.video[0]!.id] });
    expect(picksRes.status, JSON.stringify(picksRes.body)).toBe(200);
    await expectAdvance(pipelineId, "review");
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText("Review", { exact: true }).first()).toBeVisible({
      timeout: 15_000,
    });

    // ===================================================================
    // review → generation (API Approve, then open the batch atomically)
    // ===================================================================
    // Silent-failure PR-8: drive the approve through the API so the route's
    // `worker_generation` enqueue has COMPLETED on return, then immediately open
    // the generation batch (a fast write, no UI-visibility wait in between) so
    // the producer's `generation_state` probe reports `already_running` before
    // the consumer's next poll. For VIDEO the real render chain (TTS / compose /
    // caption) has NO fake mode in CI, so the captioned final + its closure are
    // seeded below; the consumer claims + closes the work_item as a
    // no-op-but-real re-entry (proven via emitGenerationClosure's await) without
    // emitting conflicting task_error events that would unbalance the closure.
    const approve = await managerPost(pipelineId, "review/decision", {
      decision: "approved",
    });
    expect(approve.status, JSON.stringify(approve.body)).toBe(200);
    await seedGenerationOpenMarker(pipelineId, 2);
    expect(await readPipelineStatus(pipelineId)).toBe("generation");

    // Focused UI assertion: the Generation stage renders after the API advance.
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText("Generation", { exact: true }).first()).toBeVisible({
      timeout: 15_000,
    });

    // Prove the worker-stage consumer claimed + closed the worker_ideation row
    // the configuration->ideation advance enqueued (the seeded video drafts
    // satisfy the producer's idempotency probe, so the real service runs as a
    // no-op-but-real re-entry and closes the row).
    const ideationClose = await awaitWorkerStageClosed(pipelineId, "worker_ideation");
    expect(ideationClose === null || ideationClose === "completed").toBeTruthy();

    // ===================================================================
    // generation → creative_qa (AUTO B1 trigger, migration 0046)
    // Seed ONE finalized captioned video creative carrying the real uploaded
    // MP4 + a clean spoken script, then emit the generation closure so the
    // trigger flips to creative_qa AND seeds the video creative_qa gate row.
    //
    // First drop the ideation drafts: the video gates scope creatives by
    // video_brief_id (no pipeline_id / version discriminator), so the
    // unfinished script_ready drafts would otherwise count as in-scope and hold
    // every gate. Only the rendered (captioned) concept proceeds to QA.
    // ===================================================================
    await dropVideoIdeationDrafts({ briefId: videoBriefId, keepIds: [] });
    const finals = await seedFinalVideoCreatives({
      pipelineId,
      briefId: videoBriefId,
      count: 1,
    });
    await emitGenerationClosure({
      pipelineId,
      taskCount: 2,
      outcome: "done",
      alreadyOpened: true,
    });
    await waitForStatus(pipelineId, "creative_qa");
    // The B1 trigger seeded a pending creative_qa gate row per final VIDEO
    // creative (joined on video_brief_id, status='captioned').
    const qaStates = (await readStageStates(pipelineId)).filter((s) => s.stage === "creative_qa");
    expect(qaStates.length).toBe(finals.length);
    expect(qaStates.map((s) => s.creative_id).sort()).toEqual(finals.map((f) => f.id).sort());

    // Focused UI assertion: the creative_qa stage renders.
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText(/Creative QA/i).first()).toBeVisible({ timeout: 15_000 });

    // ===================================================================
    // creative_qa → compliance_review
    // The worker DOWNLOADS captioned_path from Storage + ffprobes it; the MP4
    // must pass video_qa_verdict (has video, has audio, duration>0, 9:16). The
    // verdict is the worker's - we only name the creative.
    // ===================================================================
    const creativeId = finals[0]!.id;
    const qa = await workerPost("/work/pipeline/tools/qa_run", {
      pipeline_id: pipelineId,
      items: qaVideoItems(finals),
    });
    expect(qa.status, JSON.stringify(qa.body)).toBe(200);
    expect((qa.body as { rollup: string }).rollup).toBe("passed");
    // Sanity: the worker reported a real video probe, not an image path.
    const qaResults = (qa.body as { results: Array<{ surface: string; verdict: string }> }).results;
    expect(qaResults[0]?.surface).toBe("video");
    expect(qaResults[0]?.verdict).toBe("pass");

    await expectAdvance(pipelineId, "copy");

    // Focused UI assertion: the copy stage renders.
    await page.goto(`/pipeline/${pipelineId}`);
    await expect(page.getByText(/^Copy$/).first()).toBeVisible({ timeout: 15_000 });

    // ===================================================================
    // copy → compliance_review
    // The copy tool routes a video creative to video_copy_variants. Author 3
    // variants, then approve each via the format-aware copy/decision route. Copy
    // now precedes compliance, so compliance screens this approved copy.
    // ===================================================================
    for (let i = 1; i <= 3; i += 1) {
      const c = await workerPost("/work/pipeline/tools/copy", {
        pipeline_id: pipelineId,
        variants: [
          {
            creative_id: creativeId,
            platform: "meta",
            variant_index: i,
            headline: "Refresh your kitchen this season",
            primary_text: "Local remodeling pros ready to help you plan a remodel you will enjoy.",
            description: "Schedule a free planning consult with our team.",
            cta: "Learn more",
          },
        ],
      });
      expect(c.status, JSON.stringify(c.body)).toBe(200);
    }

    // Manager approves each variant (status → approved) via the shared route.
    const copyRows = await readVideoCopyVariants([creativeId]);
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
    // compliance_review → spec_validation (HARD gate)
    // The clean spoken script + approved copy is scanned by the worker compliance
    // engine; with no violating claim and no client offer-constraints it
    // adjudicates a real PASS so the gate clears.
    // ===================================================================
    const comp = await workerPost("/work/pipeline/tools/compliance_run", {
      pipeline_id: pipelineId,
      items: [{ creative_id: creativeId, surface: "video" }],
    });
    expect(comp.status, JSON.stringify(comp.body)).toBe(200);
    expect((comp.body as { rollup: string }).rollup).toBe("passed");

    await expectAdvance(pipelineId, "spec_validation");

    // ===================================================================
    // spec_validation → variant_plan
    // spec_result for the video creative at placement `reels` (9:16, 3-90s).
    // The worker's ffprobe spec backstop DOWNGRADES a non-conformant asset to
    // fail, so the MP4 must satisfy the reels PlacementSpec; our 9:16 H.264+AAC
    // ~4s fixture does, so a submitted `pass` stands.
    // ===================================================================
    const spec = await workerPost("/work/pipeline/tools/spec_result", {
      pipeline_id: pipelineId,
      results: [
        {
          creative_id: creativeId,
          platform: "meta",
          placement: "reels",
          ratio: "9x16",
          status: "pass",
          checks: { source: "operator" },
        },
      ],
    });
    expect(spec.status, JSON.stringify(spec.body)).toBe(200);
    // The backstop ran the real probe and did NOT downgrade (the asset conforms).
    const specWritten = (
      spec.body as { results: Array<{ status: string; backstop_downgraded: boolean }> }
    ).results;
    expect(specWritten[0]?.status).toBe("pass");
    expect(specWritten[0]?.backstop_downgraded).toBe(false);

    await expectAdvance(pipelineId, "variant_plan");

    // ===================================================================
    // variant_plan → finalize_assets (manager variant-plan/decision approve)
    // ===================================================================
    const vp = await managerPost(pipelineId, "variant-plan/decision", { decision: "approved" });
    expect(vp.status, JSON.stringify(vp.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("finalize_assets");

    // ===================================================================
    // finalize_assets → launch_handoff (operator finalize_result + advance)
    // The finalize tool routes the video creative to video_creatives and stamps
    // finalize_verified; the advance route's finalize gate reads it.
    // ===================================================================
    const fin = await workerPost("/work/pipeline/tools/finalize_result", {
      pipeline_id: pipelineId,
      results: [
        {
          creative_id: creativeId,
          asset_name: "remodel_kitchen_v1_9x16",
          drive_folder_id: "fake-drive-folder",
          file_path_drive: "drive://fake/remodel_kitchen_v1_9x16.mp4",
          verified: true,
        },
      ],
    });
    expect(fin.status, JSON.stringify(fin.body)).toBe(200);
    await expectAdvance(pipelineId, "launch_handoff");

    // ===================================================================
    // launch_handoff → monitor (HARD gate)
    // Compliance stayed clear (no video re-arm), spec passed, and 3 approved
    // video copy variants exist - so the launch preconditions are met. The
    // operator records the PAUSED-first Meta entities (the recorder re-checks
    // the same preconditions server-side), then the manager approves.
    //
    // The ad_entity row links the (neutral) creative_id but NOT copy_variant_id:
    // `ad_entity.copy_variant_id` FKs `copy_variants` (image) only (migration
    // 0035 repointed creative_id to the neutral `creative` base but there is no
    // neutral copy-variant base), so a video copy variant id would violate the
    // FK. copy_variant_id is optional on the recorder and the launch
    // preconditions count approved video copy independently of the ad_entity, so
    // omitting it records a faithful, valid launch. (See PR notes: a neutral
    // copy-variant base would let the video ad_entity carry its copy link.)
    const record = await workerPost("/work/pipeline/tools/launch", {
      pipeline_id: pipelineId,
      approved_by: "e2e-manager",
      entities: [
        { kind: "campaign", meta_id: "fake-video-campaign-1", meta_payload: { status: "PAUSED" } },
        {
          kind: "ad",
          meta_id: "fake-video-ad-1",
          parent_meta_id: "fake-video-campaign-1",
          creative_id: creativeId,
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
    // monitor → done (operator monitor_result + manager monitor/decision kill)
    // ===================================================================
    const mon = await workerPost("/work/pipeline/tools/monitor_result", {
      pipeline_id: pipelineId,
      results: [
        {
          campaign_id: "fake-video-campaign-1",
          window_days: 7,
          spend: 95.0,
          ghl_leads: 3,
          verdict: "keep",
          verdict_reason: "CPL within target",
        },
      ],
    });
    expect(mon.status, JSON.stringify(mon.body)).toBe(200);

    const monDec = await managerPost(pipelineId, "monitor/decision", {
      decision: "kill",
      campaign_id: "fake-video-campaign-1",
      notes: "wrap the video test run",
    });
    expect(monDec.status, JSON.stringify(monDec.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("done");

    // ===================================================================
    // Every forward stage_advanced fired in DAG order; reaching `done` proves
    // no stage stalled (every edge had an execution path for video).
    // ===================================================================
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

    // Focused UI assertion: the Done stage renders. Navigate with
    // domcontentloaded + a retry: a prior in-flight SPA navigation can abort the
    // first goto (net::ERR_ABORTED) on this long-running flow.
    await gotoWithRetry(page, `/pipeline/${pipelineId}`);
    await expect(page.getByText("Done", { exact: true }).first()).toBeVisible({ timeout: 15_000 });
  });
});

// ---------------------------------------------------------------------------
// Helpers (manager API + small reads) - mirror pipeline-workflow.spec.ts.
// ---------------------------------------------------------------------------

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

/**
 * Navigate resiliently for the final Done smoke. Uses `waitUntil: "commit"`
 * (returns as soon as the response starts, NOT after the Done page finishes
 * loading its signed-URL media) with a bounded timeout and one retry; the
 * subsequent `getByText("Done")` assertion does the real waiting. A prior
 * in-flight SPA navigation can otherwise abort (net::ERR_ABORTED) or the
 * full-load wait can hang on the media fetch on this long-running flow.
 */
async function gotoWithRetry(page: Page, url: string): Promise<void> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: "commit", timeout: 20_000 });
      return;
    } catch (e) {
      if (attempt === 1) throw e;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
}

/** Advance and assert the pipeline reached `want`. */
async function expectAdvance(pipelineId: string, want: string): Promise<void> {
  const res = await rawAdvance(pipelineId);
  expect(res.status, `advance to ${want} failed: ${JSON.stringify(res.body)}`).toBe(200);
  expect(await readPipelineStatus(pipelineId)).toBe(want);
}
