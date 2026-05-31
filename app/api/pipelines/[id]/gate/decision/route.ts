import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";

import { createAdminClient } from "@/lib/supabase/admin";
import type { Json } from "@/lib/supabase/types.gen";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ id: string }> };

type SupabaseClient = ReturnType<typeof createAdminClient>;

/**
 * Unified per-creative gate decision (the manager-clear path for soft gate
 * states).
 *
 * Operator-driven per-creative gates produce non-terminal "a human should look"
 * states that hold the gate but have no manager-clear affordance:
 *   - `compliance_review` -> `pending` (verdict `needs_review`: no hard block,
 *     advisory `compliance_finding` rows),
 *   - `spec_validation`   -> `in_progress` (a `warn` placement: the crop is
 *     valid but the operator asks the manager to eyeball it).
 * Before this route those states were dead-ends: not `failed` (so the bespoke
 * compliance override never surfaced them) yet not cleared (so the gate stayed
 * shut and the pipeline could not advance). This is ONE consistent route the
 * grid uses for BOTH stages:
 *   - `decision: "accept"`  (soft state) -> status `passed`  (advisory accepted)
 *   - `decision: "override"`(hard `failed`) -> status `overridden` (block released)
 * Both require a non-empty `note` (the audited manager justification) and stamp
 * `decided_by`/`decided_at`. A `compliance_review` decision also marks the
 * matching `compliance_finding` rows overridden (append-only audit), mirroring
 * the dedicated `compliance/override` route it generalises.
 */
const GateDecisionInput = z.object({
  creative_id: z.string().uuid(),
  stage: z.enum(["compliance_review", "spec_validation"]),
  decision: z.enum(["accept", "override"]),
  note: z.string().trim().min(1, "note is required"),
  decided_by: z.string().trim().min(1).default("manager"),
});

type GateDecision = z.infer<typeof GateDecisionInput>;

/** accept -> the soft advisory is cleared; override -> the hard block released. */
const DECISION_TO_STATUS = {
  accept: "passed",
  override: "overridden",
} as const;

export async function POST(req: NextRequest, ctx: RouteContext) {
  const { id } = await ctx.params;

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  const parsed = GateDecisionInput.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "validation_failed", issues: parsed.error.issues },
      { status: 422 },
    );
  }
  const { creative_id, stage, decision, note, decided_by } = parsed.data;

  const supabase = createAdminClient();

  // The pipeline must exist (a bad id 404s instead of silently no-op'ing).
  const { data: pipeline, error: readErr } = await supabase
    .from("pipelines")
    .select("id")
    .eq("id", id)
    .maybeSingle();
  if (readErr) {
    return NextResponse.json({ error: readErr.message }, { status: 500 });
  }
  if (!pipeline) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  // The gate row for this creative must exist (you can only decide a unit the
  // worker/operator has seeded/adjudicated).
  const { data: stateRow, error: stateErr } = await supabase
    .from("creative_stage_state" as never)
    .select("id, status")
    .eq("pipeline_id" as never, id as never)
    .eq("creative_id" as never, creative_id as never)
    .eq("stage" as never, stage as never)
    .maybeSingle();
  if (stateErr) {
    return NextResponse.json({ error: stateErr.message }, { status: 500 });
  }
  if (!stateRow) {
    return NextResponse.json(
      { error: "gate row not found for creative", creative_id, stage },
      { status: 404 },
    );
  }

  const now = new Date().toISOString();
  const status = DECISION_TO_STATUS[decision];

  const { data: updated, error: updateErr } = await supabase
    .from("creative_stage_state" as never)
    .update({
      status,
      override_note: note,
      decided_by,
      decided_at: now,
    } as never)
    .eq("pipeline_id" as never, id as never)
    .eq("creative_id" as never, creative_id as never)
    .eq("stage" as never, stage as never)
    .select()
    .single();
  if (updateErr || !updated) {
    return NextResponse.json(
      { error: updateErr?.message ?? "gate decision update failed" },
      { status: 500 },
    );
  }

  // Compliance keeps its finding-level audit: stamp the matching findings
  // overridden (append-only; the original verdict rows are retained).
  if (stage === "compliance_review") {
    await markComplianceFindingsOverridden(supabase, {
      pipelineId: id,
      creativeId: creative_id,
      decidedBy: decided_by,
      note,
      decidedAt: now,
    });
  }

  // Permanent audit event (the dashboard audit trail + the launch gate re-surface
  // overrides). Non-fatal: the gate row is the load-bearing change.
  const { error: evErr } = await supabase.from("pipeline_events").insert({
    pipeline_id: id,
    kind: decision === "override" ? "gate_overridden" : "gate_accepted",
    stage,
    payload: {
      creative_id,
      stage,
      decision,
      status,
      note,
      decided_by,
      decided_at: now,
    } as Json,
  });
  if (evErr) {
    console.warn(`[pipelines.gate.decision] event insert failed: ${evErr.message}`);
  }

  return NextResponse.json({ ok: true, creative_id, stage, decision, status, decided_by });
}

/**
 * Stamp the override audit columns on the creative's compliance findings
 * (append-only: rows are not deleted, the original verdict is retained). Best
 * effort: the `creative_stage_state` write above is the source of truth the
 * rollup predicate reads.
 */
async function markComplianceFindingsOverridden(
  supabase: SupabaseClient,
  args: {
    pipelineId: string;
    creativeId: string;
    decidedBy: string;
    note: string;
    decidedAt: string;
  },
): Promise<void> {
  const { error } = await supabase
    .from("compliance_finding" as never)
    .update({
      overridden: true,
      overridden_by: args.decidedBy,
      override_reason: args.note,
      overridden_at: args.decidedAt,
    } as never)
    .eq("pipeline_id" as never, args.pipelineId as never)
    .eq("creative_id" as never, args.creativeId as never)
    .eq("overridden" as never, false as never);
  if (error) {
    console.warn(`[pipelines.gate.decision] finding audit update failed: ${error.message}`);
  }
}
