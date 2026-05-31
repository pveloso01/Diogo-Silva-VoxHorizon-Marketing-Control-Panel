import "server-only";

import type { CopyVariantView } from "@/components/copy/CopyComposer";
import { getSignedUrl, type Creative } from "@/lib/creatives";
import { activeTracksLocal } from "@/lib/pipeline/transitions";
import type { PipelineFormat } from "@/lib/pipeline/types";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";
import type { LaunchCopyVariant } from "@/lib/review/grid";
import { createAdminClient } from "@/lib/supabase/admin";
import { getSignedUrl as getVideoSignedUrl } from "@/lib/video-creatives";

/**
 * Server-side fetch helpers for the per-creative review surfaces (P4.2–P4.6).
 *
 * Frontend reads go through the service-role client (RLS is deny-all on the new
 * tables, per the 0011 lockdown), so these helpers run in Server Components /
 * route handlers only — never the browser. They project the raw rows into the
 * narrow shapes the grid + gates consume (`lib/review/grid.ts`).
 */

/**
 * One advisory the manager weighs when accepting a soft gate state (a compliance
 * `needs_review` finding or a spec `warn` placement). Surfaced in the
 * GateReviewPanel so the accept is informed, not a rubber-stamp.
 */
export type GateAdvisory = {
  creative_id: string;
  stage: "compliance_review" | "spec_validation";
  /** Short header, e.g. a rule id or "feed 4x5 (warn)". */
  label: string;
  /** The human note / required edit, when present. */
  detail: string | null;
  severity: string | null;
};

export type ReviewBundle = {
  creatives: GridCreative[];
  states: StageStateRow[];
  copyVariants: LaunchCopyVariant[];
  /** Per-creative advisories for the soft-gate accept UI (compliance + spec). */
  advisories: GateAdvisory[];
  /** Signed preview URL per creative id (null when unavailable). */
  signedUrls: Record<string, string | null>;
};

type CreativeRow = Pick<Creative, "id" | "concept" | "status" | "file_path_supabase">;

/** A video_creatives row, narrowed to what the review bundle needs. */
type VideoCreativeRow = {
  id: string;
  status: string;
  captioned_path: string | null;
};

/**
 * Load every creative for a pipeline + its per-creative gate state + copy
 * variants, ready for the CreativeReviewGrid / launch preconditions. Killed +
 * soft-deleted creatives are still returned (the grid greys killed ones and
 * drops them from the rollup scope); deleted rows are filtered out.
 *
 * Format-aware (Phase 2 / B4): a VIDEO creative's data lives in the parity
 * tables `video_creatives` + `video_copy_variants` (migration 0031), while its
 * per-creative gate state shares the neutral `creative_stage_state` table
 * (migration 0034/0046: a video creative id is a valid creative_stage_state
 * key). So we additively union video creatives + video copy variants into the
 * bundle (the states read already covers both tracks). Launch preconditions
 * (spec-pass + compliance-clear + >=3 approved copy) then see video creatives
 * exactly like image creatives. Image creatives may be `killed` (dropped from
 * scope); video creatives are in scope unless soft-deleted. The video tables are
 * read only when the video track is active, so an image-only pipeline behaves
 * byte-identically to before.
 */
export async function getReviewBundle(pipelineId: string): Promise<ReviewBundle> {
  const supabase = createAdminClient();

  // The pipeline row drives the format branch + the video lineage join. A
  // missing pipeline (or unreadable row) degrades to image-only behaviour.
  const { data: pipeline } = await supabase
    .from("pipelines")
    .select("format_choice, image_brief_id, video_brief_id")
    .eq("id", pipelineId)
    .maybeSingle();
  const tracks = activeTracksLocal((pipeline?.format_choice ?? "image") as PipelineFormat);

  const creatives: GridCreative[] = [];
  const copyVariants: LaunchCopyVariant[] = [];
  const signedUrls: Record<string, string | null> = {};

  // ----- Image track -----
  // Fetch the FINAL image creatives (v1*) by `image_brief_id`, NOT `pipeline_id`.
  // The canonical render path (record_creative_stage) keys creatives on
  // `brief_id` and leaves `pipeline_id` NULL, so a pipeline_id filter returned
  // ZERO rows for operator-rendered finals -> creative_qa (and every per-creative
  // review stage) rendered EMPTY. Mirrors the video track's brief_id fetch + the
  // v1* scope the QA-gate seed (migration 0056) uses, so the grid matches the
  // seeded gates exactly.
  let imageRows: CreativeRow[] = [];
  if (tracks.image && pipeline?.image_brief_id) {
    const { data: creativeRows } = await supabase
      .from("creatives")
      .select("id, concept, status, file_path_supabase")
      .eq("brief_id", pipeline.image_brief_id)
      .like("version", "v1%")
      .is("deleted_at", null)
      .order("created_at", { ascending: true });
    imageRows = (creativeRows ?? []) as CreativeRow[];

    for (const c of imageRows) {
      creatives.push({ id: c.id, concept: c.concept, status: c.status });
    }

    const { data: copyRows } = await supabase
      .from("copy_variants")
      .select("creative_id, status")
      .eq("pipeline_id", pipelineId);
    for (const cv of (copyRows ?? []) as LaunchCopyVariant[]) {
      copyVariants.push(cv);
    }
  }

  // ----- Video track (additive, B4) -----
  let videoRows: VideoCreativeRow[] = [];
  if (tracks.video && pipeline?.video_brief_id) {
    const { data: videoCreativeRows } = await supabase
      .from("video_creatives")
      .select("id, status, captioned_path")
      .eq("brief_id", pipeline.video_brief_id)
      .is("deleted_at", null)
      .order("created_at", { ascending: true });
    videoRows = (videoCreativeRows ?? []) as VideoCreativeRow[];

    for (const c of videoRows) {
      // A video creative is always in scope (no `killed` lifecycle); its
      // video_creative_status is not part of CreativeLifecycle but the only
      // scope check is `!== "killed"`, which it never is.
      creatives.push({
        id: c.id,
        concept: null,
        status: c.status as GridCreative["status"],
      });
    }

    const videoIds = videoRows.map((c) => c.id);
    if (videoIds.length > 0) {
      const { data: videoCopyRows } = await supabase
        .from("video_copy_variants")
        .select("creative_id, status")
        .in("creative_id", videoIds);
      for (const cv of (videoCopyRows ?? []) as LaunchCopyVariant[]) {
        copyVariants.push(cv);
      }
    }
  }

  // Per-creative gate state is the shared neutral table; one read covers both
  // image and video creatives (keyed by the neutral creative id).
  const { data: stateRows } = await supabase
    .from("creative_stage_state")
    .select("creative_id, stage, status, override_note, summary")
    .eq("pipeline_id", pipelineId);
  const states: StageStateRow[] = (stateRows ?? []) as StageStateRow[];

  // Per-creative advisories the manager reviews when accepting a soft gate
  // (compliance needs_review findings + spec warn/fail placement notes). Scoped
  // to this pipeline; best-effort (a read failure degrades to no advisories, so
  // the panel still renders the accept action, just without the detail).
  const advisories: GateAdvisory[] = [];
  const { data: findingRows } = await supabase
    .from("compliance_finding")
    .select("creative_id, rule_id, severity, required_edit, overridden")
    .eq("pipeline_id", pipelineId);
  for (const f of (findingRows ?? []) as Array<{
    creative_id: string;
    rule_id: string | null;
    severity: string | null;
    required_edit: string | null;
    overridden: boolean | null;
  }>) {
    if (f.overridden) continue;
    advisories.push({
      creative_id: f.creative_id,
      stage: "compliance_review",
      label: f.rule_id ?? "compliance finding",
      detail: f.required_edit,
      severity: f.severity,
    });
  }
  const { data: specRows } = await supabase
    .from("spec_check")
    .select("creative_id, platform, placement, ratio, status, checks")
    .eq("pipeline_id", pipelineId)
    .in("status", ["warn", "fail", "exception"]);
  for (const s of (specRows ?? []) as Array<{
    creative_id: string;
    platform: string | null;
    placement: string | null;
    ratio: string | null;
    status: string | null;
    checks: { notes?: unknown } | null;
  }>) {
    const note = typeof s.checks?.notes === "string" ? s.checks.notes : null;
    advisories.push({
      creative_id: s.creative_id,
      stage: "spec_validation",
      label: `${s.platform ?? ""} ${s.placement ?? ""} ${s.ratio ?? ""} (${s.status ?? ""})`.trim(),
      detail: note,
      severity: s.status,
    });
  }

  // Resolve signed preview URLs (best-effort; null when missing). Image creatives
  // sign their file_path_supabase; video creatives sign their captioned MP4 path.
  await Promise.all([
    ...imageRows.map(async (c) => {
      signedUrls[c.id] = await getSignedUrl(supabase, c.file_path_supabase);
    }),
    ...videoRows.map(async (c) => {
      signedUrls[c.id] = await getVideoSignedUrl(supabase, c.captioned_path);
    }),
  ]);

  return { creatives, states, copyVariants, advisories, signedUrls };
}

/**
 * Load the full copy-variant rows for a pipeline as the CopyComposer's view
 * shape (#359). Ordered by creative then variant index for a stable editor.
 */
export async function getCopyVariants(pipelineId: string): Promise<CopyVariantView[]> {
  const supabase = createAdminClient();
  const { data } = await supabase
    .from("copy_variants")
    .select(
      "id, creative_id, platform, placement, variant_index, headline, body, description, cta, humanized, status",
    )
    .eq("pipeline_id", pipelineId)
    .order("creative_id", { ascending: true })
    .order("variant_index", { ascending: true });
  return (data ?? []) as CopyVariantView[];
}

/**
 * Load the latest variant_plan + its cells for a pipeline (variant_plan stage).
 * Returns null when no plan has been authored yet.
 */
export async function getVariantPlan(pipelineId: string): Promise<{
  test_variable: string | null;
  hypothesis: string | null;
  cells: Array<{
    id: string;
    cell_index: number;
    label: string | null;
    creative_id: string | null;
    copy_variant_id: string | null;
  }>;
} | null> {
  const supabase = createAdminClient();
  const { data: plan } = await supabase
    .from("variant_plan")
    .select("id, test_variable, hypothesis")
    .eq("pipeline_id", pipelineId)
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (!plan) return null;

  const { data: cells } = await supabase
    .from("variant_plan_cell")
    .select("id, cell_index, label, creative_id, copy_variant_id")
    .eq("variant_plan_id", plan.id)
    .order("cell_index", { ascending: true });

  return {
    test_variable: plan.test_variable,
    hypothesis: plan.hypothesis,
    cells: cells ?? [],
  };
}

/** The client's CPL target for the monitor thresholds, or null. */
export async function getClientCplTarget(clientId: string | null): Promise<number | null> {
  if (!clientId) return null;
  const supabase = createAdminClient();
  const { data } = await supabase
    .from("clients")
    .select("cpl_target")
    .eq("id", clientId)
    .maybeSingle();
  return data?.cpl_target ?? null;
}
