"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CreativeReviewGrid } from "@/components/review/CreativeReviewGrid";
import { SubStatePill } from "@/components/review/SubStatePill";
import type { GateAdvisory } from "@/lib/review/fetch";
import {
  buildGridRows,
  rollupCleared,
  rollupForStage,
  type CreativeStage,
  type GridCreative,
  type StageStateRow,
} from "@/lib/review/grid";
import { cn } from "@/lib/utils";

/**
 * GateReviewPanel: ONE consistent manager-clear UI for the per-creative gate
 * stages whose operator run leaves a soft, non-terminal state.
 *
 * Operator-driven gates produce "a human should look" verdicts that hold the
 * gate but had no manager-clear affordance:
 *   - `compliance_review` -> `pending` (needs_review: 0 hard blocks, advisory
 *     findings),
 *   - `spec_validation`   -> `in_progress` (a `warn` placement: valid crop, the
 *     operator asks the manager to eyeball it).
 * Both were dead-ends (not `failed`, so the old compliance-only override never
 * surfaced them; not cleared, so the gate stayed shut). This panel surfaces the
 * per-creative advisories (so the accept is informed, not a rubber-stamp) and
 * the two manager actions, both posting to the unified
 * `POST /api/pipelines/[id]/gate/decision`:
 *   - "Review & accept" (soft pending/in_progress) -> status `passed`,
 *   - "Override"        (hard `failed`)            -> status `overridden`.
 * Continue is gated on the single-source `rollupCleared` (matching the server),
 * so a held unit keeps it disabled.
 */
export type GateReviewPanelProps = {
  pipelineId: string;
  /** Which per-creative gate this panel drives. */
  stage: Extract<CreativeStage, "compliance_review" | "spec_validation">;
  creatives: GridCreative[];
  states: StageStateRow[];
  /** Per-creative advisories (compliance findings / spec warn notes). */
  advisories?: GateAdvisory[];
  onOpenCreative?: (creativeId: string) => void;
  onContinue?: () => void;
};

const CONFIRM_WORD = "OVERRIDE";

const STAGE_NOUN: Record<GateReviewPanelProps["stage"], string> = {
  compliance_review: "Compliance",
  spec_validation: "Spec",
};

const ACCEPT_PREFILL: Record<GateReviewPanelProps["stage"], string> = {
  compliance_review: "Advisory findings reviewed; no hard compliance blocks. Accepted for release.",
  spec_validation: "Crop reviewed and confirmed acceptable for the intended placements.",
};

export function GateReviewPanel({
  pipelineId,
  stage,
  creatives,
  states,
  advisories = [],
  onOpenCreative,
  onContinue,
}: GateReviewPanelProps) {
  const router = useRouter();
  const rows = buildGridRows(creatives, states);
  const counts = rollupForStage(rows, stage);
  const blocked = rows.filter(
    (r) => r.creative.status !== "killed" && r.cells[stage].status === "failed",
  );
  // Soft "needs a human look" units: pending (compliance needs_review) or
  // in_progress (spec warn). Not failed, so they never showed in a blocked list.
  const needsReview = rows.filter(
    (r) =>
      r.creative.status !== "killed" &&
      (r.cells[stage].status === "pending" || r.cells[stage].status === "in_progress"),
  );
  const cleared = rollupCleared(rows, stage);

  const [activeCreative, setActiveCreative] = useState<string | null>(null);
  const [activeKind, setActiveKind] = useState<"override" | "accept">("accept");
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsConfirm = activeKind === "override";
  const canSubmit = note.trim().length > 0 && (!needsConfirm || confirm === CONFIRM_WORD) && !busy;

  const advisoriesFor = (creativeId: string) =>
    advisories.filter((a) => a.creative_id === creativeId && a.stage === stage);

  const submit = async () => {
    if (!activeCreative || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/pipelines/${pipelineId}/gate/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          creative_id: activeCreative,
          stage,
          decision: activeKind,
          note: note.trim(),
        }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setError(data.error ?? `Decision failed (${res.status})`);
        return;
      }
      setActiveCreative(null);
      setNote("");
      setConfirm("");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Network error");
    } finally {
      setBusy(false);
    }
  };

  const openAccept = (creativeId: string) => {
    setActiveCreative(creativeId);
    setActiveKind("accept");
    setNote(ACCEPT_PREFILL[stage]);
    setConfirm("");
    setError(null);
  };
  const openOverride = (creativeId: string) => {
    setActiveCreative(creativeId);
    setActiveKind("override");
    setNote("");
    setConfirm("");
    setError(null);
  };

  const AdvisoryList = ({ creativeId }: { creativeId: string }) => {
    const items = advisoriesFor(creativeId);
    if (items.length === 0) return null;
    return (
      <ul className="mt-1 space-y-0.5">
        {items.map((a, i) => (
          <li key={`${a.label}-${i}`} className="text-xs text-muted-foreground">
            <span className="font-medium">{a.label}</span>
            {a.detail ? <span>: {a.detail}</span> : null}
          </li>
        ))}
      </ul>
    );
  };

  return (
    <div className="space-y-4" data-testid="gate-review-panel" data-stage={stage}>
      {blocked.length > 0 ? (
        <div
          role="alert"
          data-testid="hard-block-banner"
          className="flex items-start gap-2 rounded-md border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900 dark:border-rose-800 dark:bg-rose-950/30 dark:text-rose-200"
        >
          <ShieldAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <div>
            <p className="font-semibold">
              {STAGE_NOUN[stage]} is a hard gate: {blocked.length} creative(s) blocked.
            </p>
            <p className="text-xs">
              Every blocked creative must pass or be overridden with a written justification before
              this run can continue.
            </p>
          </div>
        </div>
      ) : null}

      <CreativeReviewGrid
        creatives={creatives}
        states={states}
        mode={stage}
        onOpenCreative={onOpenCreative}
      />

      {blocked.length > 0 ? (
        <section className="space-y-2" data-testid="blocked-list">
          <h3 className="text-sm font-semibold">Blocked creatives</h3>
          <ul className="space-y-2">
            {blocked.map((r) => (
              <li
                key={r.creative.id}
                className="flex items-start justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm"
              >
                <div className="min-w-0">
                  <span className="truncate font-medium">
                    {r.creative.concept ?? "Untitled concept"}
                  </span>
                  <AdvisoryList creativeId={r.creative.id} />
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  data-testid={`override-open-${r.creative.id}`}
                  onClick={() => openOverride(r.creative.id)}
                >
                  Override
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {needsReview.length > 0 ? (
        <section className="space-y-2" data-testid="needs-review-list">
          <h3 className="text-sm font-semibold">Needs review: advisory ({needsReview.length})</h3>
          <p className="text-xs text-muted-foreground">
            No hard blocks. The checks flagged advisories below; accept each with a written note to
            clear the gate (recorded as an audited manager decision).
          </p>
          <ul className="space-y-2">
            {needsReview.map((r) => (
              <li
                key={r.creative.id}
                className="flex items-start justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm"
              >
                <div className="min-w-0">
                  <span className="truncate font-medium">
                    {r.creative.concept ?? "Untitled concept"}
                  </span>
                  <AdvisoryList creativeId={r.creative.id} />
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  data-testid={`accept-open-${r.creative.id}`}
                  onClick={() => openAccept(r.creative.id)}
                >
                  Review &amp; accept
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {activeCreative ? (
        <section
          data-testid="decision-form"
          className="space-y-3 rounded-md border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/20"
        >
          <h3 className="text-sm font-semibold text-amber-900 dark:text-amber-200">
            {needsConfirm ? `Override ${STAGE_NOUN[stage]} block` : "Accept advisory findings"}
          </h3>
          <label className="block text-xs font-medium" htmlFor="decision-note">
            Justification (required)
          </label>
          <textarea
            id="decision-note"
            data-testid="decision-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={
              needsConfirm
                ? "Why is releasing this block acceptable?"
                : "Why are these advisory findings acceptable for release?"
            }
            className="min-h-[64px] w-full rounded-md border border-input bg-background px-2 py-1 text-sm"
          />
          {needsConfirm ? (
            <>
              <label className="block text-xs font-medium" htmlFor="decision-confirm">
                Type <span className="font-mono font-semibold">{CONFIRM_WORD}</span> to confirm
              </label>
              <input
                id="decision-confirm"
                data-testid="decision-confirm"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-2 py-1 text-sm"
              />
            </>
          ) : null}
          {error ? (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant={needsConfirm ? "destructive" : "default"}
              data-testid="decision-submit"
              disabled={!canSubmit}
              onClick={submit}
            >
              {busy
                ? needsConfirm
                  ? "Overriding…"
                  : "Accepting…"
                : needsConfirm
                  ? "Confirm override"
                  : "Accept & clear"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setActiveCreative(null)}>
              Cancel
            </Button>
          </div>
        </section>
      ) : null}

      <div className="flex items-center justify-end gap-3 border-t border-border pt-3">
        <span className="text-xs text-muted-foreground">
          {cleared
            ? `${STAGE_NOUN[stage]} cleared for all creatives.`
            : `${counts.blocked} blocked · ${counts.pending} pending`}
        </span>
        <Button
          type="button"
          data-testid="gate-continue"
          disabled={!cleared}
          aria-disabled={!cleared}
          className={cn(!cleared && "cursor-not-allowed")}
          onClick={onContinue}
        >
          Continue
        </Button>
      </div>
    </div>
  );
}
