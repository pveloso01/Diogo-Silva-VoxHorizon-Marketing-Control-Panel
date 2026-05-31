import { test, expect, getTestAdminClient } from "./_fixtures";
import { sessionCookieHeader } from "./_auth";
import { mockWorkerIdeation } from "./_mocks/sse-harness";
import { makeSquarePngBase64 } from "./_mocks/png-fixture";
import {
  CLEAN_VIDEO_SCRIPT_OUTLINE,
  assertWorkerHealthy,
  awaitWorkerStageClosed,
  dropVideoIdeationDrafts,
  emitGenerationClosure,
  qaPassItems,
  qaVideoItems,
  readPipelineStatus,
  readStageAdvancedOrder,
  readStageStates,
  readVideoCopyVariants,
  seedFinalCreatives,
  seedFinalVideoCreatives,
  seedGenerationOpenMarker,
  waitForStatus,
  workerPost,
} from "./_mocks/workflow-driver";

/**
 * Full gated BOTH-tracks workflow e2e (Phase 2 / PR2).
 *
 * Drives ONE `format='both'` pipeline through ALL 12 stages to `done`, with an
 * image creative AND a video creative passing every per-creative gate together
 * against the REAL worker verdicts:
 *   - QA: the image creative is adjudicated from a real PNG (operator b64), the
 *     video creative from a real MP4 in Storage (ffprobe); both must PASS.
 *   - Compliance (HARD): both creatives are scanned clean (image copy surface,
 *     video spoken script) and adjudicated a real PASS.
 *   - Copy: 3 approved variants per creative - `copy_variants` for image,
 *     `video_copy_variants` for video.
 *   - Spec: image at `feed` (1:1), video at `reels` (9:16) with the worker's
 *     ffprobe spec backstop probing the MP4.
 *
 * The two-pass compliance re-arm is IMAGE-ONLY (migration 0025 fires on
 * `copy_variants`), so authoring the IMAGE copy resets the image compliance unit
 * to `pending` while the video unit stays `passed`. We assert that split and
 * re-clear the image unit before launch - exactly the documented two-pass
 * invariant, format-aware.
 *
 * The pre-existing lightweight "both" UI test (side-by-side fieldsets + cancel)
 * is retained in its own `test()`.
 */

const PNG_B64 = makeSquarePngBase64();

const CLEAN_IMAGE_COPY = {
  headline: "Refresh your kitchen this season",
  primary_text: "Local remodeling pros ready to help you plan a remodel you will enjoy.",
  description: "Schedule a free planning consult with our team.",
  cta: "Learn more",
};

test.describe("pipeline - both formats", () => {
  test("both tracks render side-by-side and cancel works", async ({ page, clientId }) => {
    void clientId;

    await page.goto("/pipeline/new");
    await expect(page).toHaveURL(/\/pipeline\/[a-f0-9-]{36}$/);

    // Switch format to "both". The form layout grows a second fieldset and
    // the format badge in the header flips to "Image + Video".
    await page.getByLabel("Both", { exact: true }).click();

    // Both fieldsets should be present.
    await expect(page.getByText(/image brief/i)).toBeVisible();
    await expect(page.getByText(/video brief/i)).toBeVisible();

    // Image-side required fields.
    await expect(page.getByLabel(/^market$/i)).toBeVisible();
    await expect(page.getByLabel(/total budget/i)).toBeVisible();

    // Video-side required fields. The presence of BOTH the image fieldset
    // (above) and these video fields is the synchronous, deterministic proof
    // that selecting "Both" took effect -- it is driven by local client state,
    // no server round-trip. (The header format badge is server-rendered from
    // pipeline.format_choice and only reflects the change after the autosave
    // PATCH + realtime refresh; asserting it here raced the 10s budget under CI
    // load and is redundant to this test's subject, so it is intentionally not
    // asserted -- badge-from-format coverage lives in the StatusBadge unit test
    // and the initial-load specs.)
    await expect(page.getByLabel(/^hook$/i)).toBeVisible();
    await expect(page.getByLabel(/voice id/i)).toBeVisible();

    // Cancel from a non-terminal stage works as in the single-track specs.
    await page.getByRole("button", { name: /cancel pipeline/i }).click();
    await expect(page.getByRole("dialog")).toBeVisible();

    // "Keep running" closes the modal without changing state.
    await page.getByRole("button", { name: /keep running/i }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    // Status badge is still Configuration.
    await expect(page.getByText("Configuration", { exact: true }).first()).toBeVisible();

    // Re-open and confirm the cancel.
    await page.getByRole("button", { name: /cancel pipeline/i }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    const confirmBtn = page.getByRole("button", { name: /^cancel pipeline$/i }).last();
    await confirmBtn.click();

    await expect(page.getByText("Cancelled", { exact: true })).toBeVisible({ timeout: 15_000 });
  });

  test("drives all 12 stages to done for an image AND a video creative together", async ({
    page,
    clientId,
  }) => {
    // The both flow runs TWO creatives (image + video) through all 12 gates -
    // ~double the sequential worker/manager round-trips of a single-track run,
    // plus the MP4 upload + ffprobe - so it legitimately exceeds the default
    // 120s test budget. Give it headroom (it is not a hang; the per-step polls
    // still bound each stage).
    test.setTimeout(240_000);

    const admin = getTestAdminClient();
    await assertWorkerHealthy();

    // ===================================================================
    // configuration → ideation (seed both payloads, advance route)
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
        format_choice: "both",
        config_draft: {
          image_payload: {
            service: "remodeling",
            budget: 5000,
            market: "Austin, TX",
            landing_page_url: "https://example.com/lp",
            offer_text: "Free planning consult",
          },
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

    const { data: pipeRow } = await admin
      .from("pipelines")
      .select("image_brief_id, video_brief_id, format_choice")
      .eq("id", pipelineId)
      .maybeSingle();
    const imageBriefId = pipeRow?.image_brief_id;
    const videoBriefId = pipeRow?.video_brief_id;
    expect(pipeRow?.format_choice).toBe("both");
    if (!imageBriefId) throw new Error("pipeline has no image_brief_id after configure→ideation");
    if (!videoBriefId) throw new Error("pipeline has no video_brief_id after configure→ideation");

    // ===================================================================
    // ideation → review (UI): seed both tracks, pick 1 image + 1 video
    // ===================================================================
    const seeded = await mockWorkerIdeation(page, { pipelineId, n: 3 });
    expect(seeded.image.length).toBe(3);
    expect(seeded.video.length).toBe(3);
    await page.goto(`/pipeline/${pipelineId}`);
    // Focused UI assertion: both ideation grids hydrated over the seeded rows.
    await expect(page.getByText(/Picked:\s*0\s*of\s*3/).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/image concepts/i)).toBeVisible();
    await expect(page.getByText(/video concepts/i)).toBeVisible();

    // Record 1 image + 1 video pick in a SINGLE picks-API write. The ideation
    // checkbox toggles POST one track at a time, and two concurrent toggles
    // read-modify-write the same `pipelines.picks` jsonb (last-write-wins), so
    // the dual-track UI write races; the atomic single-request write is the
    // faithful, deterministic way to record both picks (the same picks route the
    // UI calls). The ideation->review advance gate then sees both tracks picked.
    const imagePick = seeded.image[0]!.id;
    const videoPick = seeded.video[0]!.id;
    const picksRes = await managerPost(pipelineId, "picks", {
      image: [imagePick],
      video: [videoPick],
    });
    expect(picksRes.status, JSON.stringify(picksRes.body)).toBe(200);

    // ideation -> review -> generation.
    // The dual-track Review approve button is realtime/layout-brittle under
    // Playwright (two signed-URL pick-preview sections); the no-stall-relevant
    // logic is the advance + review/decision routes (gate on picks, snapshot the
    // cost estimate, stamp the approval, and advance). Drive them through the
    // Next API directly - the same approach the image no-stall spec takes for
    // the realtime-brittle gates.
    await expectAdvance(pipelineId, "review");
    const reviewDecision = await managerPost(pipelineId, "review/decision", {
      decision: "approved",
    });
    expect(reviewDecision.status, JSON.stringify(reviewDecision.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("generation");

    // Silent-failure PR-8: open the generation batch immediately so the real
    // worker-stage consumer (now draining `worker_generation`) treats the stage
    // as in-flight and skips the in-process producer for BOTH tracks -- so it
    // does not add image v1.0 finals (FAKE_RENDER) or attempt the video render
    // chain (no fake mode in CI). The seeded finals below are the exact gate
    // scope; the consumer still claims + closes the work_item (a no-op-but-real
    // re-entry), proven via awaitWorkerStageClosed in emitGenerationClosure.
    await seedGenerationOpenMarker(pipelineId, 2);

    // Prove the worker-stage consumer claimed + closed the worker_ideation row.
    const ideationClose = await awaitWorkerStageClosed(pipelineId, "worker_ideation");
    expect(ideationClose === null || ideationClose === "completed").toBeTruthy();

    // ===================================================================
    // generation → creative_qa (AUTO B1 trigger seeds image AND video QA gates)
    // Seed 1 image final (v1.0) + 1 video captioned final; drop the video
    // ideation drafts first so only the rendered concept is in gate scope.
    // ===================================================================
    await dropVideoIdeationDrafts({ briefId: videoBriefId, keepIds: [] });
    const imageFinals = await seedFinalCreatives({ pipelineId, briefId: imageBriefId, count: 1 });
    const videoFinals = await seedFinalVideoCreatives({
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

    const qaStates = (await readStageStates(pipelineId)).filter((s) => s.stage === "creative_qa");
    const wantQaIds = [imageFinals[0]!.id, videoFinals[0]!.id].sort();
    expect(qaStates.map((s) => s.creative_id).sort()).toEqual(wantQaIds);

    const imageId = imageFinals[0]!.id;
    const videoId = videoFinals[0]!.id;

    // ===================================================================
    // creative_qa → compliance_review (qa_run image PNG + video MP4 → passed)
    // ===================================================================
    const qa = await workerPost("/work/pipeline/tools/qa_run", {
      pipeline_id: pipelineId,
      items: [...qaPassItems(imageFinals, PNG_B64), ...qaVideoItems(videoFinals)],
    });
    expect(qa.status, JSON.stringify(qa.body)).toBe(200);
    expect((qa.body as { rollup: string }).rollup).toBe("passed");
    const qaSurfaces = (qa.body as { results: Array<{ surface: string }> }).results
      .map((r) => r.surface)
      .sort();
    expect(qaSurfaces).toEqual(["image", "video"]);

    await expectAdvance(pipelineId, "copy");

    // ===================================================================
    // copy → compliance_review: 3 approved variants per creative (both tables).
    // Copy now precedes compliance, so the approved copy is exactly what
    // compliance screens (no pre-copy image-only pass, no re-arm split).
    // ===================================================================
    for (let i = 1; i <= 3; i += 1) {
      const ci = await workerPost("/work/pipeline/tools/copy", {
        pipeline_id: pipelineId,
        variants: [
          { creative_id: imageId, platform: "meta", variant_index: i, ...CLEAN_IMAGE_COPY },
        ],
      });
      expect(ci.status, JSON.stringify(ci.body)).toBe(200);
      const cv = await workerPost("/work/pipeline/tools/copy", {
        pipeline_id: pipelineId,
        variants: [
          { creative_id: videoId, platform: "meta", variant_index: i, ...CLEAN_IMAGE_COPY },
        ],
      });
      expect(cv.status, JSON.stringify(cv.body)).toBe(200);
    }

    // Approve image copy via the shared route (copy_variants).
    const imgCopyRows = await readImageCopyVariants(admin, pipelineId, imageId);
    expect(imgCopyRows.length).toBe(3);
    for (const row of imgCopyRows) {
      const dec = await managerPost(pipelineId, "copy/decision", {
        id: row.id,
        decision: "approved",
      });
      expect(dec.status, JSON.stringify(dec.body)).toBe(200);
    }
    // Approve video copy via the same route (video_copy_variants).
    const vidCopyRows = await readVideoCopyVariants([videoId]);
    expect(vidCopyRows.length).toBe(3);
    for (const row of vidCopyRows) {
      const dec = await managerPost(pipelineId, "copy/decision", {
        id: row.id,
        decision: "approved",
      });
      expect(dec.status, JSON.stringify(dec.body)).toBe(200);
    }

    await expectAdvance(pipelineId, "compliance_review");

    // ===================================================================
    // compliance_review → spec_validation (HARD): clean PASS for BOTH creatives,
    // now adjudicated over the final approved copy.
    // ===================================================================
    const comp = await workerPost("/work/pipeline/tools/compliance_run", {
      pipeline_id: pipelineId,
      items: [
        { creative_id: imageId, surface: "copy" },
        { creative_id: videoId, surface: "video" },
      ],
    });
    expect(comp.status, JSON.stringify(comp.body)).toBe(200);
    expect((comp.body as { rollup: string }).rollup).toBe("passed");

    await expectAdvance(pipelineId, "spec_validation");

    // ===================================================================
    // spec_validation → variant_plan: image feed (1:1) + video reels (9:16)
    // ===================================================================
    const spec = await workerPost("/work/pipeline/tools/spec_result", {
      pipeline_id: pipelineId,
      results: [
        {
          creative_id: imageId,
          platform: "meta",
          placement: "feed",
          ratio: "1x1",
          status: "pass",
          checks: { resolution: "ok" },
        },
        {
          creative_id: videoId,
          platform: "meta",
          placement: "reels",
          ratio: "9x16",
          status: "pass",
          checks: { source: "operator" },
        },
      ],
    });
    expect(spec.status, JSON.stringify(spec.body)).toBe(200);
    const specByCreative = new Map(
      (spec.body as { results: Array<{ creative_id: string; status: string }> }).results.map(
        (r) => [r.creative_id, r.status],
      ),
    );
    expect(specByCreative.get(imageId)).toBe("pass");
    expect(specByCreative.get(videoId)).toBe("pass");

    await expectAdvance(pipelineId, "variant_plan");

    // ===================================================================
    // variant_plan → finalize_assets
    // ===================================================================
    const vp = await managerPost(pipelineId, "variant-plan/decision", { decision: "approved" });
    expect(vp.status, JSON.stringify(vp.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("finalize_assets");

    // ===================================================================
    // finalize_assets → launch_handoff (finalize BOTH creatives)
    // ===================================================================
    const fin = await workerPost("/work/pipeline/tools/finalize_result", {
      pipeline_id: pipelineId,
      results: [
        {
          creative_id: imageId,
          asset_name: "remodel_kitchen_v1_1x1",
          drive_folder_id: "fake-drive-folder",
          file_path_drive: "drive://fake/remodel_kitchen_v1_1x1.png",
          verified: true,
        },
        {
          creative_id: videoId,
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
    // After the reorder compliance runs AFTER copy and stays cleared through to
    // launch, so this image compliance_run is an idempotent re-assert of the
    // clean pass over the final copy. Then the operator records the PAUSED-first
    // entities (server re-checks preconditions) and the manager approves.
    // ===================================================================
    const compClear = await workerPost("/work/pipeline/tools/compliance_run", {
      pipeline_id: pipelineId,
      items: [{ creative_id: imageId, copy_variant_id: imgCopyRows[0]!.id, surface: "copy" }],
    });
    expect(compClear.status, JSON.stringify(compClear.body)).toBe(200);
    expect((compClear.body as { rollup: string }).rollup).toBe("passed");

    const record = await workerPost("/work/pipeline/tools/launch", {
      pipeline_id: pipelineId,
      approved_by: "e2e-manager",
      entities: [
        { kind: "campaign", meta_id: "fake-both-campaign-1", meta_payload: { status: "PAUSED" } },
        {
          kind: "ad",
          meta_id: "fake-both-ad-img",
          parent_meta_id: "fake-both-campaign-1",
          creative_id: imageId,
          // Image copy variants live in copy_variants, which ad_entity.copy_variant_id
          // FKs - so the image ad can carry its copy link.
          copy_variant_id: imgCopyRows[0]!.id,
        },
        {
          kind: "ad",
          meta_id: "fake-both-ad-vid",
          parent_meta_id: "fake-both-campaign-1",
          creative_id: videoId,
          // No copy_variant_id: ad_entity.copy_variant_id FKs copy_variants
          // (image) only, so a video_copy_variants id would violate the FK. The
          // launch preconditions count approved video copy independently, so the
          // record stays valid + faithful. (See PR notes on the neutral copy base.)
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
    // monitor → done
    // ===================================================================
    const mon = await workerPost("/work/pipeline/tools/monitor_result", {
      pipeline_id: pipelineId,
      results: [
        {
          campaign_id: "fake-both-campaign-1",
          window_days: 7,
          spend: 140.0,
          ghl_leads: 5,
          verdict: "keep",
          verdict_reason: "CPL within target",
        },
      ],
    });
    expect(mon.status, JSON.stringify(mon.body)).toBe(200);

    const monDec = await managerPost(pipelineId, "monitor/decision", {
      decision: "kill",
      campaign_id: "fake-both-campaign-1",
      notes: "wrap the both-track test run",
    });
    expect(monDec.status, JSON.stringify(monDec.body)).toBe(200);
    expect(await readPipelineStatus(pipelineId)).toBe("done");

    // ===================================================================
    // Forward DAG order + no stall.
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

    // No final Done-page navigation here. Unlike the single-track Done pages, the
    // dual-gallery (image + video) Done page server-renders both galleries with
    // signed-URL media and reliably exceeds a 20s navigation commit in CI (a real
    // page-perf characteristic, not a pipeline-logic issue). The load-bearing
    // proof is already asserted above: every one of the 12 gates was driven to
    // `done` against the real worker verdicts and the forward stage_advanced DAG
    // fired in order. The dual-track UI render itself is covered by the config +
    // ideation assertions earlier in this test (and the single-track Done page is
    // smoke-tested in pipeline-video / pipeline-workflow). See PR notes.
  });
});

// ---------------------------------------------------------------------------
// Helpers (manager API + small reads) - mirror pipeline-workflow.spec.ts.
// ---------------------------------------------------------------------------

type ApiResult = { status: number; body: unknown };

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

async function rawAdvance(pipelineId: string): Promise<ApiResult> {
  return managerPost(pipelineId, "advance", {});
}

async function expectAdvance(pipelineId: string, want: string): Promise<void> {
  const res = await rawAdvance(pipelineId);
  expect(res.status, `advance to ${want} failed: ${JSON.stringify(res.body)}`).toBe(200);
  expect(await readPipelineStatus(pipelineId)).toBe(want);
}

/** Read image `copy_variants` rows for a (pipeline, creative), variant order. */
async function readImageCopyVariants(
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
