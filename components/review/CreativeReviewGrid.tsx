"use client";

import { ChevronRight, Lock } from "lucide-react";

import { SubStatePill } from "@/components/review/SubStatePill";
import { RollupChip } from "@/components/review/RollupChip";
import {
  buildGridRows,
  rollupForStage,
  CREATIVE_STAGE_ORDER,
  CREATIVE_STAGE_LABEL,
  type CreativeStage,
  type GridCreative,
  type StageStateRow,
} from "@/lib/review/grid";
import { cn } from "@/lib/utils";

/**
 * The CreativeReviewGrid (#357, P4.2): "the projection of the per-creative data
 * model." Rows = creatives; columns = the four per-creative gate stages
 * (QA / Compliance / Copy / Spec) as `SubStatePill`s; a `RollupChip` per column
 * header summarising the gate's rollup.
 *
 * Locked cells encode the forced ordering — a downstream cell is rendered with
 * a lock and is non-interactive until every upstream stage cleared for that
 * creative (mirrors the server `pipeline_rollup_cleared` gate, computed in
 * `lib/review/grid.ts`). A per-creative drill-in opens the tabbed ReviewDrawer.
 *
 * The grid is mode-parameterized: `mode` is the stage the page is sitting on so
 * the matching column header reads as the active gate. The whole grid still
 * renders all four columns (the manager sees the full per-creative picture),
 * but only the active column's actions are emphasised.
 */
export type CreativeReviewGridProps = {
  creatives: GridCreative[];
  /** Flat creative_stage_state rows for all (creative, stage) pairs. */
  states: StageStateRow[];
  /** The stage the page is currently on (emphasises that column). */
  mode: CreativeStage;
  /** Open the per-creative drawer. */
  onOpenCreative?: (creativeId: string) => void;
  className?: string;
};

export function CreativeReviewGrid({
  creatives,
  states,
  mode,
  onOpenCreative,
  className,
}: CreativeReviewGridProps) {
  const rows = buildGridRows(creatives, states);

  if (creatives.length === 0) {
    return (
      <div
        data-testid="review-grid-empty"
        className="rounded-md border border-dashed bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground"
      >
        No creatives to review yet. The worker hasn&apos;t produced any finals for this run.
      </div>
    );
  }

  return (
    <div className={cn("overflow-x-auto rounded-lg border border-border", className)}>
      <table className="w-full border-collapse text-sm" data-testid="creative-review-grid">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            <th scope="col" className="px-3 py-2 font-semibold">
              Creative
            </th>
            {CREATIVE_STAGE_ORDER.map((stage) => {
              const counts = rollupForStage(rows, stage);
              return (
                <th
                  key={stage}
                  scope="col"
                  data-testid={`grid-col-${stage}`}
                  data-active={stage === mode ? "true" : undefined}
                  className={cn(
                    "px-3 py-2 font-semibold",
                    stage === mode && "bg-sky-50 dark:bg-sky-950/30",
                  )}
                >
                  <div className="flex flex-col gap-1">
                    <span>{CREATIVE_STAGE_LABEL[stage]}</span>
                    <RollupChip
                      total={counts.total}
                      cleared={counts.cleared}
                      blocked={counts.blocked}
                      pending={counts.pending}
                    />
                  </div>
                </th>
              );
            })}
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isKilled = row.creative.status === "killed";
            return (
              <tr
                key={row.creative.id}
                data-testid={`grid-row-${row.creative.id}`}
                data-killed={isKilled ? "true" : undefined}
                tabIndex={0}
                aria-label={`Review ${row.creative.concept ?? "creative"}`}
                onClick={() => onOpenCreative?.(row.creative.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpenCreative?.(row.creative.id);
                  }
                }}
                className={cn(
                  "cursor-pointer border-b border-border transition-colors last:border-0 hover:bg-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                  isKilled && "opacity-50",
                )}
              >
                <th scope="row" className="px-3 py-2 text-left font-medium">
                  <span className="block max-w-[16rem] truncate">
                    {row.creative.concept ?? "Untitled concept"}
                  </span>
                  {isKilled ? (
                    <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                      killed — out of scope
                    </span>
                  ) : null}
                </th>
                {CREATIVE_STAGE_ORDER.map((stage) => {
                  const cell = row.cells[stage];
                  return (
                    <td
                      key={stage}
                      data-testid={`grid-cell-${row.creative.id}-${stage}`}
                      data-status={cell.status}
                      data-locked={cell.locked ? "true" : undefined}
                      className={cn(
                        "px-3 py-2",
                        stage === mode && "bg-sky-50/60 dark:bg-sky-950/20",
                      )}
                    >
                      {cell.locked ? (
                        <span
                          className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground ring-1 ring-inset ring-border"
                          title="Locked until the previous stage clears for this creative"
                        >
                          <Lock aria-hidden="true" className="size-3" />
                          Locked
                        </span>
                      ) : (
                        <SubStatePill status={cell.status} title={cell.note ?? undefined} />
                      )}
                    </td>
                  );
                })}
                {/* Affordance only — the whole row is the click target (mouse +
                    keyboard via the tr's onClick/onKeyDown). aria-hidden since the
                    row carries the accessible "Review ..." label. */}
                <td className="px-3 py-2 text-right">
                  <ChevronRight
                    aria-hidden="true"
                    data-testid={`grid-open-${row.creative.id}`}
                    className="inline size-4 text-muted-foreground"
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
