"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { GateReviewPanel } from "@/components/review/GateReviewPanel";
import { CreativeReviewGrid } from "@/components/review/CreativeReviewGrid";
import { ReviewDrawer } from "@/components/review/ReviewDrawer";
import { StageShell } from "@/components/pipeline/StageShell";
import type { GateAdvisory } from "@/lib/review/fetch";
import {
  buildGridRows,
  rollupCleared,
  type CreativeStage,
  type GridCreative,
  type StageStateRow,
} from "@/lib/review/grid";

/**
 * Mode-parameterized stage host for the four per-creative gate stages
 * (creative_qa / compliance_review / copy / spec_validation). Renders the
 * CreativeReviewGrid and the per-creative ReviewDrawer; for compliance + spec it
 * swaps in the unified GateReviewPanel (hard-block override + soft advisory
 * accept). The Continue button is gated on the per-creative rollup clearing
 * (matching the server gate); it POSTs to the generic advance route.
 */
export type StageCreativeReviewProps = {
  pipelineId: string;
  mode: CreativeStage;
  creatives: GridCreative[];
  states: StageStateRow[];
  signedUrls: Record<string, string | null>;
  /** Per-creative advisories for the soft-gate accept UI (compliance + spec). */
  advisories?: GateAdvisory[];
};

const STAGE_TITLE: Record<CreativeStage, string> = {
  creative_qa: "Creative QA",
  compliance_review: "Compliance",
  copy: "Copy",
  spec_validation: "Spec validation",
};

const STAGE_SUBTITLE: Record<CreativeStage, string> = {
  creative_qa:
    "Pass/fail each final per the QA rubric. One failed creative never blocks the others.",
  compliance_review:
    "Hard gate — every creative must pass or be overridden with a written justification.",
  copy: "Author ≥3 approved copy variants per creative.",
  spec_validation: "Validate placement specs + derived crops for each creative.",
};

export function StageCreativeReview({
  pipelineId,
  mode,
  creatives,
  states,
  signedUrls,
  advisories = [],
}: StageCreativeReviewProps) {
  const router = useRouter();
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const rows = buildGridRows(creatives, states);
  const cleared = rollupCleared(rows, mode);
  const activeCreative: GridCreative | null = openId
    ? (creatives.find((c) => c.id === openId) ?? null)
    : null;

  const advance = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/pipelines/${pipelineId}/advance`, { method: "POST" });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setError(data.error ?? `Advance failed (${res.status})`);
        return;
      }
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Network error");
    } finally {
      setBusy(false);
    }
  };

  const drawer = (
    <ReviewDrawer
      creative={activeCreative}
      states={states}
      signedUrl={activeCreative ? (signedUrls[activeCreative.id] ?? null) : null}
      open={openId !== null}
      onOpenChange={(o) => setOpenId(o ? openId : null)}
      initialTab={mode}
    />
  );

  // Compliance + spec get the unified gate panel (hard-block override + soft
  // advisory accept, posting to /gate/decision); creative_qa gets the grid.
  if (mode === "compliance_review" || mode === "spec_validation") {
    return (
      <>
        <StageShell
          title={STAGE_TITLE[mode]}
          subtitle={STAGE_SUBTITLE[mode]}
          canContinue={false}
          body={
            <GateReviewPanel
              pipelineId={pipelineId}
              stage={mode}
              creatives={creatives}
              states={states}
              advisories={advisories}
              onOpenCreative={setOpenId}
              onContinue={advance}
            />
          }
        />
        {drawer}
      </>
    );
  }

  return (
    <>
      <StageShell
        title={STAGE_TITLE[mode]}
        subtitle={STAGE_SUBTITLE[mode]}
        canContinue={cleared && !busy}
        onContinue={advance}
        continueLabel="Continue"
        body={
          <div className="space-y-3">
            <CreativeReviewGrid
              creatives={creatives}
              states={states}
              mode={mode}
              onOpenCreative={setOpenId}
            />
            {error ? (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            ) : null}
            {!cleared ? (
              <p className="text-xs text-muted-foreground">
                Continue unlocks once every in-scope creative clears this stage.
              </p>
            ) : null}
          </div>
        }
      />
      {drawer}
    </>
  );
}
