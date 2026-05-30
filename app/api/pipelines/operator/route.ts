import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";

import { operatorInstruction } from "@/lib/operator/dispatch";
import { hydratePipelineStatus } from "@/lib/pipeline/derived-status";
import { type PipelineInsert } from "@/lib/pipeline/schemas";
import { createAdminClient } from "@/lib/supabase/admin";
import type { Json } from "@/lib/supabase/types.gen";
import { enqueueWorkItem } from "@/lib/work-queue/enqueue";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/pipelines/operator
 *
 * Operator-driven kickoff. This is the supervision cockpit's "hire the
 * operator for a new run" entry point: it creates a fresh pipeline in the
 * `configuration` stage (the same shape `POST /api/pipelines` produces) and
 * then nudges the Hermes operator to start authoring the brief.
 *
 * Body: `{ instruction: string, format_choice?, client_id? }`
 *   - `instruction` — the manager's free-text brief, e.g.
 *     "4 roofing ads, Austin, $99 inspection". Stored on
 *     `config_draft.operator_instruction` so the StageConfiguration form and
 *     the operator both see it, and threaded into the dispatch instruction.
 *   - `format_choice` — defaults to `image` (the operator pipeline is the
 *     image-ad flow).
 *   - `client_id` — optional; the operator/manager can assign one later.
 *
 * Flow (silent-failure PR-3 cutover):
 *   1. Insert the pipelines row (status defaults to `configuration`; we seed
 *      `advanced_at.configuration` and store the instruction in config_draft).
 *      The pipeline row creation is NOT a stage transition -- the bootstrap
 *      `stage_advanced` event the auto-emit trigger writes on the matching
 *      work_item enqueue covers the timeline.
 *   2. Synchronously enqueue the operator_dispatch work_item. The auto-emit
 *      trigger then writes the canonical `stage_advanced` + `operator_dispatched`
 *      `pipeline_events` rows; the operator-daemon claims the queued row and
 *      docker-execs into the operator container. Enqueue failure rolls the
 *      pipeline back so silent-failure cannot leak a half-state.
 *
 * Returns the created pipeline with status 201 (same envelope as the create
 * route) so the kickoff UI can redirect straight to the detail page.
 */
/**
 * The manager's "Finals model" choice → (backend, model) for the FINALS
 * (generation) stage. The FREE codex/gpt-image-2 is the default; the paid Kie
 * models are nano-banana-2 / Flux / Seedream. IDEATION is NOT configurable — it
 * always renders on the free codex model server-side, never one of these. This
 * registry mirrors `FINALS_MODELS` in
 * `ekko-skills/pipeline-operator/helper.py` (the operator routes renders by the
 * persisted backend+model). Keep the two in lockstep.
 */
const FINALS_MODELS = {
  "gpt-image-2 (free)": { backend: "openai-codex", model: "gpt-image-2" },
  "nano-banana-2": { backend: "kie", model: "nano-banana-2" },
  Flux: { backend: "kie", model: "flux-2/pro-text-to-image" },
  Seedream: { backend: "kie", model: "bytedance/seedream-v4-text-to-image" },
} as const;

const FINALS_MODEL_LABELS = Object.keys(FINALS_MODELS) as [
  keyof typeof FINALS_MODELS,
  ...(keyof typeof FINALS_MODELS)[],
];
// Default to the fast paid Kie model for FINALS. The free codex model renders
// finals at ~3-5 min/image (HIGH quality), so a multi-concept batch cannot
// finish inside the 600s render-tool window -- the call times out after ~one
// concept and generation stalls. nano-banana-2 renders the whole batch in
// seconds, reliably. IDEATION stays free codex (it tolerates multiple resumes).
const DEFAULT_FINALS_LABEL: keyof typeof FINALS_MODELS = "nano-banana-2";

const OperatorKickoffBody = z.object({
  instruction: z.string().trim().min(1, "instruction is required").max(5000),
  format_choice: z.enum(["image", "video", "both"]).default("image"),
  client_id: z.string().uuid().optional(),
  // The finals (generation) image model. Defaults to the FREE codex model;
  // ideation always stays free regardless of this choice.
  finals_model: z.enum(FINALS_MODEL_LABELS).default(DEFAULT_FINALS_LABEL),
});

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const parsed = OperatorKickoffBody.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "validation_failed", issues: parsed.error.issues },
      { status: 422 },
    );
  }
  const { instruction, format_choice, client_id, finals_model } = parsed.data;

  // Resolve the finals model label → (backend, model) and persist it on the
  // pipeline so the operator renders FINALS with the manager's choice. Ideation
  // stays on the free codex model server-side regardless of this.
  const finals = FINALS_MODELS[finals_model];

  const supabase = createAdminClient();
  const now = new Date().toISOString();
  const insert: PipelineInsert = {
    format_choice,
    client_id: client_id ?? null,
    advanced_at: { configuration: now } as unknown as Json,
    config_draft: {
      operator_driven: true,
      operator_instruction: instruction,
      finals_render_label: finals_model,
      finals_render_backend: finals.backend,
      finals_render_model: finals.model,
    } as unknown as Json,
  };

  const { data: pipeline, error: insertErr } = await supabase
    .from("pipelines")
    .insert(insert)
    .select()
    .single();
  if (insertErr || !pipeline) {
    return NextResponse.json({ error: insertErr?.message ?? "insert failed" }, { status: 500 });
  }

  // Silent-failure PR-3 cutover: the auto-emit trigger on the work_item
  // enqueue below writes the canonical `stage_advanced` + `operator_dispatched`
  // `pipeline_events` rows -- the route no longer races the trigger by writing
  // them itself. The operator-daemon claims the queued row from the worker's
  // REST surface and docker-execs into the operator container; the legacy
  // fire-and-forget `dispatchOperator` is gone.
  try {
    await enqueueWorkItem({
      kind: "operator_dispatch",
      pipelineId: pipeline.id,
      payload: {
        instruction: operatorInstruction("configuration", pipeline.id, instruction),
        stage: "configuration",
      },
      idempotencyKey: `op-disp:${pipeline.id}:configuration:kickoff`,
      createdBy: "api/pipelines/operator",
    });
  } catch (e) {
    // Roll back the pipeline insert so silent-failure cannot leak a half-state.
    await supabase.from("pipelines").delete().eq("id", pipeline.id);
    return NextResponse.json({ error: `work_item enqueue failed: ${String(e)}` }, { status: 500 });
  }

  // Silent-failure PR-4: hydrate `status` from the reducer. The work_item
  // enqueue above fires the auto-emit trigger that writes the seed
  // `stage_advanced`/`operator_dispatched` events; the reducer resolves to
  // 'configuration' as the most recent stage_advanced target.
  const enriched = await hydratePipelineStatus(supabase, pipeline);
  return NextResponse.json({ pipeline: enriched }, { status: 201 });
}
