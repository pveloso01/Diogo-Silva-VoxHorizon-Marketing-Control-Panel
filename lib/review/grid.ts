/**
 * Pure projection helpers for the CreativeReviewGrid (#357, P4.2) + the gates
 * (#360, #361). The grid is "the projection of the per-creative data model":
 * one row per creative, one column per per-creative stage, each cell carrying
 * the `creative_stage_state` verdict for that (creative, stage).
 *
 * This module owns the *logic* the UI keys off so it stays unit-testable and
 * agrees with the server `pipeline_rollup_cleared()`:
 *   - the canonical column order (the forced QA → copy → compliance → spec
 *     ordering),
 *   - the per-cell lock predicate (a downstream stage is locked until the
 *     upstream stage cleared for that creative),
 *   - the per-column rollup counts (drives `RollupChip` + the gate enable),
 *   - the launch preconditions (spec-pass ∧ compliance-clear ∧ ≥3 approved
 *     copy/creative).
 *
 * No React, no IO — pure data in / data out (the `node` vitest project).
 *
 * The cleared-verdict set + the killed-creative scope rule are NOT defined here:
 * they live in `lib/pipeline/rollup.ts`, the ONE source the advance route, this
 * grid, and (mirrored) the SQL `pipeline_rollup_cleared` (0039) all share, so the
 * UI gate and the server gate agree by construction (M2 / E2.3).
 */
import {
  MIN_APPROVED_COPY,
  copyGateCleared,
  isCreativeInScope,
  isStageStateCleared,
  rollupCleared as rollupClearedCore,
} from "@/lib/pipeline/rollup";

// Re-export the copy-gate minimum from the single-source rollup module. Many
// call sites import it from here historically; keep the public name stable.
export { MIN_APPROVED_COPY };

/** The four per-creative gate stages, in their forced execution order. */
export type CreativeStage = "creative_qa" | "compliance_review" | "copy" | "spec_validation";

/** Mirrors `stage_state_enum` in the DB (0017). */
export type SubState = "pending" | "in_progress" | "passed" | "failed" | "overridden" | "skipped";

/** Mirrors `image_creative_status`: a killed creative drops out of the rollup scope. */
export type CreativeLifecycle = "draft" | "approved" | "rejected" | "live" | "killed";

/**
 * The canonical left-to-right column order. The pipeline runs these stages in
 * this sequence; the grid renders them in the same order and locks downstream
 * cells until the upstream cell clears (the "forced ordering").
 */
export const CREATIVE_STAGE_ORDER: readonly CreativeStage[] = [
  "creative_qa",
  "copy",
  "compliance_review",
  "spec_validation",
] as const;

export const CREATIVE_STAGE_LABEL: Record<CreativeStage, string> = {
  creative_qa: "QA",
  compliance_review: "Compliance",
  copy: "Copy",
  spec_validation: "Spec",
};

/** A single creative_stage_state row, narrowed to what the grid needs. */
export type StageStateRow = {
  creative_id: string;
  stage: CreativeStage;
  status: SubState;
  override_note?: string | null;
  summary?: unknown;
};

/** A creative row, narrowed to what the grid needs. */
export type GridCreative = {
  id: string;
  concept: string | null;
  status: CreativeLifecycle;
};

/**
 * True when a verdict counts as cleared (matches `pipeline_rollup_cleared`).
 * Delegates to the single-source rollup module (`CLEARED_STAGE_STATES`) so the
 * grid, the advance route, and the SQL predicate (0039) cannot drift.
 */
export function isCleared(status: SubState): boolean {
  return isStageStateCleared(status);
}

/**
 * A creative is "in scope" for a stage's rollup unless it was killed (or
 * soft-deleted). Delegates to the single-source scope rule so the grid agrees
 * with the advance route + the SQL predicate (0039).
 */
export function isInScope(creative: GridCreative): boolean {
  return isCreativeInScope(creative);
}

/**
 * A single resolved grid cell: the verdict for one (creative, stage) plus
 * whether the UI should lock it (a downstream stage whose upstream is not yet
 * cleared for this creative).
 */
export type GridCell = {
  stage: CreativeStage;
  status: SubState;
  /** Locked = the operator may not act here yet (upstream not cleared). */
  locked: boolean;
  /** Override note / failure reason for the tooltip, when present. */
  note: string | null;
};

export type GridRow = {
  creative: GridCreative;
  cells: Record<CreativeStage, GridCell>;
};

/**
 * Build a lookup of the verdict per (creative, stage) from the flat
 * creative_stage_state rows. A missing row defaults to `pending`.
 */
function indexStates(rows: StageStateRow[]): Map<string, StageStateRow> {
  const map = new Map<string, StageStateRow>();
  for (const r of rows) {
    map.set(`${r.creative_id}::${r.stage}`, r);
  }
  return map;
}

/**
 * Project creatives + their stage-state rows into grid rows. Each cell is
 * locked when any earlier stage in {@link CREATIVE_STAGE_ORDER} is not cleared
 * for that creative — the dashboard cannot let the operator act on copy before
 * compliance cleared, for instance.
 */
export function buildGridRows(creatives: GridCreative[], states: StageStateRow[]): GridRow[] {
  const idx = indexStates(states);
  return creatives.map((creative) => {
    const cells = {} as Record<CreativeStage, GridCell>;
    let upstreamCleared = true;
    for (const stage of CREATIVE_STAGE_ORDER) {
      const row = idx.get(`${creative.id}::${stage}`);
      const status: SubState = row?.status ?? "pending";
      cells[stage] = {
        stage,
        status,
        // The first column is never locked; later columns lock until every
        // earlier column has cleared for this creative.
        locked: !upstreamCleared,
        note: row?.override_note ?? null,
      };
      // Carry the gate forward: once an upstream stage stops being cleared,
      // everything downstream of it is locked.
      upstreamCleared = upstreamCleared && isCleared(status);
    }
    return { creative, cells };
  });
}

export type RollupCounts = {
  total: number;
  cleared: number;
  blocked: number;
  pending: number;
};

/**
 * Count the per-column rollup for one stage across the in-scope creatives.
 * `blocked` = a `failed` unit (holds the gate); `pending` = pending/in_progress.
 * Mirrors the server predicate so `RollupChip` + the gate agree by construction.
 */
export function rollupForStage(rows: GridRow[], stage: CreativeStage): RollupCounts {
  let total = 0;
  let cleared = 0;
  let blocked = 0;
  let pending = 0;
  for (const row of rows) {
    if (!isInScope(row.creative)) continue;
    total += 1;
    const status = row.cells[stage].status;
    if (isCleared(status)) {
      cleared += 1;
    } else if (status === "failed") {
      blocked += 1;
    } else {
      pending += 1;
    }
  }
  return { total, cleared, blocked, pending };
}

/**
 * Whether a per-creative stage's rollup is cleared (the gate may open). Matches
 * `pipeline_rollup_cleared` (0039): ≥1 in-scope creative AND none uncleared.
 * Delegates the verdict to the single-source `rollupCleared` core over the
 * in-scope creatives' cell verdicts so the grid and the server agree.
 */
export function rollupCleared(rows: GridRow[], stage: CreativeStage): boolean {
  const statuses = rows.filter((r) => isInScope(r.creative)).map((r) => r.cells[stage].status);
  return rollupClearedCore(statuses).cleared;
}

// ---------------------------------------------------------------------------
// Launch preconditions
// ---------------------------------------------------------------------------

/** A copy_variant row narrowed to the launch precondition check. */
export type LaunchCopyVariant = {
  creative_id: string;
  status: string | null;
};

export type LaunchPreconditionId = "spec_pass" | "compliance_clear" | "copy_ge_3";

export type LaunchPrecondition = {
  id: LaunchPreconditionId;
  label: string;
  met: boolean;
  detail: string;
};

/**
 * Compute the launch preconditions checklist from the resolved grid + the copy
 * variants. Launch is gated on ALL of:
 *   - spec_validation cleared for every in-scope creative,
 *   - compliance_review cleared (no un-overridden failures),
 *   - ≥3 approved copy variants per in-scope creative.
 */
export function launchPreconditions(
  rows: GridRow[],
  copyVariants: LaunchCopyVariant[],
): LaunchPrecondition[] {
  const inScope = rows.filter((r) => isInScope(r.creative));

  const spec = rollupForStage(rows, "spec_validation");
  const compliance = rollupForStage(rows, "compliance_review");

  // Approved-copy count per in-scope creative — fed to the single-source copy
  // gate predicate so the launch checklist agrees with the copy stage gate + the
  // worker launch re-check.
  const approvedByCreative = new Map<string, number>();
  for (const cv of copyVariants) {
    if (cv.status === "approved") {
      approvedByCreative.set(cv.creative_id, (approvedByCreative.get(cv.creative_id) ?? 0) + 1);
    }
  }
  const copyGate = copyGateCleared(
    inScope.map((r) => r.creative.id),
    approvedByCreative,
  );
  const copyMet = copyGate.cleared;

  return [
    {
      id: "compliance_clear",
      label: "Compliance clear",
      met:
        compliance.total > 0 && compliance.blocked === 0 && compliance.cleared === compliance.total,
      detail:
        compliance.blocked > 0
          ? `${compliance.blocked} creative(s) blocked on compliance`
          : compliance.total === 0
            ? "no creatives screened"
            : `${compliance.cleared}/${compliance.total} cleared`,
    },
    {
      id: "spec_pass",
      label: "Spec validation passed",
      met: spec.total > 0 && spec.cleared === spec.total,
      detail:
        spec.total === 0 ? "no spec checks yet" : `${spec.cleared}/${spec.total} placements passed`,
    },
    {
      id: "copy_ge_3",
      label: `≥${MIN_APPROVED_COPY} approved copy variants per creative`,
      met: copyMet,
      detail:
        inScope.length === 0
          ? "no creatives in scope"
          : copyGate.short === 0
            ? "all creatives have enough approved copy"
            : `${copyGate.short} creative(s) short on approved copy`,
    },
  ];
}

/** True only when EVERY launch precondition is met. */
export function launchReady(preconditions: LaunchPrecondition[]): boolean {
  return preconditions.length > 0 && preconditions.every((p) => p.met);
}

/**
 * Surface the audited compliance overrides for re-display at the launch gate
 * (the architecture: "LaunchGate re-surfaces overrides"). Returns one entry per
 * creative whose compliance unit is `overridden`.
 */
export function overriddenCreatives(
  rows: GridRow[],
): Array<{ id: string; concept: string | null; note: string | null }> {
  return rows
    .filter((r) => isInScope(r.creative) && r.cells.compliance_review.status === "overridden")
    .map((r) => ({
      id: r.creative.id,
      concept: r.creative.concept,
      note: r.cells.compliance_review.note,
    }));
}
