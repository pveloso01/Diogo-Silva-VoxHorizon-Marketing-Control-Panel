"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Film, ImageOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { CostBreakdownTable } from "@/components/pipeline/CostBreakdownTable";
import { StageShell } from "@/components/pipeline/StageShell";
import { WorkItemPanelSlot } from "@/components/pipeline/WorkItemPanel";
import { submitReviewDecision } from "@/lib/pipeline/client";
import { activeTracks, type PipelineTrack } from "@/lib/pipeline/tracks";
import type { Pipeline } from "@/lib/pipeline/types";
import type { WorkItem } from "@/lib/work-queue/types";
import { estimatePipelineCost } from "@/lib/cost-estimator";
import { CREATIVES_BUCKET } from "@/lib/creatives";
import {
  fetchCreativesByIds,
  fetchVideoCreativesByIdsWithOutline,
  signStoragePaths,
} from "@/lib/realtime/client-data";
import { cn } from "@/lib/utils";

type DecisionT = "approved" | "approved_with_changes" | "rejected";

export type StageReviewProps = {
  pipeline: Pipeline;
  /** Currently unused — accepted to keep the wiring point stable when Agent A
   *  threads the brief IDs from the page. The component reads everything it
   *  needs from `pipeline.picks` directly. */
  imageBriefId?: string | null;
  videoBriefId?: string | null;
  /**
   * SSR-seeded active work_item for this pipeline (silent-failure PR-5). Drives
   * the auto-hiding `WorkItemPanelSlot` at the bottom of the stage; null leaves
   * the layout unchanged and mounts no realtime channel.
   */
  initialWorkItem?: WorkItem | null;
};

/** Light projection of a `creatives` row — keep the preview card decoupled
 *  from the full DB-generated type so the row shape can evolve without
 *  breaking this file. */
type ImagePickRow = {
  id: string;
  concept: string | null;
  ratio: string | null;
  file_path_supabase: string | null;
};

/** Light projection of a `video_creatives` row + its denormalized script.
 *  We keep only what the compact review card needs to render. */
type VideoPickRow = {
  id: string;
  status: string | null;
  duration_actual_s: number | null;
  broll_clips: unknown;
  script_outline: unknown;
};

/**
 * Review-stage UI (PF-D-3).
 *
 * Renders a compact summary of the operator's picks across the active tracks,
 * a live-computed `<CostBreakdownTable />`, and the three-button approval
 * gate. Submitting a decision hits `POST /api/pipelines/[id]/review/decision`
 * via `submitReviewDecision()`; the server flips the pipeline forward to
 * `generation` (or `cancelled` on reject) and PipelineDetailRealtime picks
 * up the new state via Supabase Realtime.
 *
 * Notes-required rule mirrors the brief approval gate: `approved` is
 * note-optional; `approved_with_changes` and `rejected` require non-empty
 * notes (server enforces the same).
 *
 * The data fetch runs in the browser via the public client. Pick uuids come
 * straight from `pipeline.picks`; we then `IN`-fetch the picked rows from
 * `creatives` / `video_creatives`. Image thumbnails resolve via a signed URL
 * against the `creatives` bucket because images can be in private storage;
 * video tracks don't render a preview at the review stage (the captioned MP4
 * isn't built yet) — we show the script hook + b-roll plan summary instead.
 */
export function StageReview({ pipeline, initialWorkItem = null }: StageReviewProps) {
  const router = useRouter();

  const tracks = useMemo(() => activeTracks(pipeline.format_choice), [pipeline.format_choice]);
  const imageActive = tracks.includes("image");
  const videoActive = tracks.includes("video");

  const imagePickIds = useMemo(
    () => (pipeline.picks?.image ?? []).filter((id): id is string => typeof id === "string"),
    [pipeline.picks],
  );
  const videoPickIds = useMemo(
    () => (pipeline.picks?.video ?? []).filter((id): id is string => typeof id === "string"),
    [pipeline.picks],
  );

  // Operator-driven pipelines render on the free Codex subscription (image +
  // operator both $0), so the forecast must reflect that — not the paid
  // Kie/Anthropic defaults used by the deterministic (Ekko) flow.
  const operatorDriven = Boolean(
    (pipeline.config_draft as { operator_driven?: boolean } | null | undefined)?.operator_driven,
  );

  // Estimate is pure & cheap — re-derive on each render rather than memoize
  // (the inputs are stable for any given pipeline snapshot).
  const estimate = estimatePipelineCost({
    format: pipeline.format_choice,
    picked_image_count: imagePickIds.length,
    picked_video_count: videoPickIds.length,
    estimated_chat_iterations: 1,
    operator_driven: operatorDriven,
  });

  return (
    <StageShell
      title="Review"
      subtitle="Confirm picks, review costs, then approve to start generation."
      canContinue={false}
      // The approval gate buttons (Approve / Approve with changes / Reject)
      // drive the decision; suppress the generic footer Continue CTA so there's
      // no dead, permanently-disabled button beneath them.
      hideContinue
      body={
        <div className="flex flex-col gap-6">
          {imagePickIds.length === 0 && videoPickIds.length === 0 ? (
            <div className="rounded-md border border-dashed bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground">
              No picks recorded. Go back to the ideation stage and select at least one variant per
              active track before approving.
            </div>
          ) : null}

          {imageActive && imagePickIds.length > 0 ? (
            <PickPreviewSection
              track="image"
              pickIds={imagePickIds}
              renderEmpty="No image picks yet."
            />
          ) : null}

          {videoActive && videoPickIds.length > 0 ? (
            <PickPreviewSection
              track="video"
              pickIds={videoPickIds}
              renderEmpty="No video picks yet."
            />
          ) : null}

          <section className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold">Cost forecast</h3>
            <CostBreakdownTable
              estimate={estimate}
              emptyMessage="No costs to forecast — make at least one pick to see an estimate."
            />
          </section>

          <PipelineApprovalGate
            pipelineId={pipeline.id}
            disabled={imagePickIds.length === 0 && videoPickIds.length === 0}
            onDecided={() => {
              // PipelineDetailRealtime listens for the row update and will
              // re-render; we proactively refresh so the operator sees the
              // new stage immediately even on a flaky realtime channel.
              router.refresh();
            }}
          />

          <WorkItemPanelSlot pipelineId={pipeline.id} initialWorkItem={initialWorkItem} />
        </div>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Pick previews — one section per active track.
// ---------------------------------------------------------------------------

function PickPreviewSection({
  track,
  pickIds,
  renderEmpty,
}: {
  track: PipelineTrack;
  pickIds: string[];
  renderEmpty: string;
}) {
  const label = track === "image" ? "Image picks" : "Video picks";

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold">{label}</h3>
        <span className="text-xs text-muted-foreground">
          {pickIds.length} {pickIds.length === 1 ? "pick" : "picks"}
        </span>
      </div>
      {track === "image" ? (
        <ImagePicksGrid pickIds={pickIds} emptyMessage={renderEmpty} />
      ) : (
        <VideoPicksGrid pickIds={pickIds} emptyMessage={renderEmpty} />
      )}
    </section>
  );
}

function ImagePicksGrid({ pickIds, emptyMessage }: { pickIds: string[]; emptyMessage: string }) {
  const [rows, setRows] = useState<ImagePickRow[] | null>(null);
  const [signedUrls, setSignedUrls] = useState<Record<string, string | null>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (pickIds.length === 0) {
      setRows([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const fetched = await fetchCreativesByIds<ImagePickRow>(pickIds);
        if (cancelled) return;
        // Preserve the operator's pick order from `pipeline.picks.image[]`.
        const byId = new Map(fetched.map((r) => [r.id, r]));
        const ordered = pickIds.map((id) => byId.get(id)).filter((r): r is ImagePickRow => !!r);
        setRows(ordered);

        // Resolve thumbnails for any row that has a stored file. One batched
        // server round-trip; failures leave the entry as `null` so the
        // placeholder tile renders.
        const paths = ordered
          .map((r) => r.file_path_supabase)
          .filter((p): p is string => typeof p === "string" && p.length > 0);
        if (paths.length > 0) {
          const urls = await signStoragePaths(CREATIVES_BUCKET, paths, 3600);
          if (cancelled) return;
          setSignedUrls((prev) => {
            const next = { ...prev };
            for (const row of ordered) {
              if (row.file_path_supabase) {
                next[row.id] = urls[row.file_path_supabase] ?? null;
              }
            }
            return next;
          });
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load image picks");
        setRows([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pickIds]);

  if (rows === null) {
    return <SkeletonCardRow count={Math.max(pickIds.length, 2)} />;
  }
  if (error) {
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
      >
        Failed to load image picks: {error}
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed bg-muted/20 px-3 py-4 text-center text-xs text-muted-foreground">
        {emptyMessage}
      </div>
    );
  }

  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
      {rows.map((row) => (
        <li key={row.id} className="flex flex-col gap-2 overflow-hidden rounded-md border bg-card">
          <div className="relative aspect-square w-full overflow-hidden bg-muted/40">
            {signedUrls[row.id] ? (
              // eslint-disable-next-line @next/next/no-img-element -- signed URLs from Supabase Storage need a plain <img>
              <img
                src={signedUrls[row.id] ?? undefined}
                alt={row.concept?.trim() || "Picked variant"}
                loading="lazy"
                className="h-full w-full object-cover"
              />
            ) : (
              <div className="flex h-full w-full flex-col items-center justify-center gap-1 text-muted-foreground">
                <ImageOff aria-hidden="true" className="h-5 w-5" />
                <span className="text-[10px]">No render</span>
              </div>
            )}
          </div>
          <div className="flex flex-col gap-0.5 px-2.5 pb-2.5">
            <p
              className="truncate text-xs font-medium text-foreground"
              title={row.concept ?? undefined}
            >
              {row.concept?.trim() || "Untitled"}
            </p>
            <p className="font-mono text-[11px] text-muted-foreground">{row.ratio ?? "—"}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function VideoPicksGrid({ pickIds, emptyMessage }: { pickIds: string[]; emptyMessage: string }) {
  const [rows, setRows] = useState<VideoPickRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (pickIds.length === 0) {
      setRows([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        // Pull the picked video creatives plus their associated brief's
        // `script_outline` (via the service-role API) so we can render the
        // hook excerpt while preserving the operator's pick order.
        const { creatives, outlines } = await fetchVideoCreativesByIdsWithOutline<{
          id: string;
          status?: string | null;
          duration_actual_s?: number | null;
          broll_clips?: unknown;
          brief_id?: string;
        }>(pickIds);
        if (cancelled) return;

        const byId = new Map<string, VideoPickRow>(
          creatives.map((c) => {
            const row: VideoPickRow = {
              id: c.id,
              status: typeof c.status === "string" ? c.status : null,
              duration_actual_s: c.duration_actual_s ?? null,
              broll_clips: c.broll_clips ?? null,
              script_outline: outlines[c.brief_id ?? ""] ?? null,
            };
            return [c.id, row];
          }),
        );
        const ordered: VideoPickRow[] = [];
        for (const id of pickIds) {
          const row = byId.get(id);
          if (row) ordered.push(row);
        }
        setRows(ordered);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load video picks");
        setRows([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pickIds]);

  if (rows === null) {
    return <SkeletonCardRow count={Math.max(pickIds.length, 1)} />;
  }
  if (error) {
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
      >
        Failed to load video picks: {error}
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed bg-muted/20 px-3 py-4 text-center text-xs text-muted-foreground">
        {emptyMessage}
      </div>
    );
  }

  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {rows.map((row) => {
        const outline = row.script_outline as
          | { hook?: unknown; segments?: unknown[] }
          | null
          | undefined;
        const hook =
          outline && typeof outline.hook === "string"
            ? outline.hook
            : "No hook recorded for this brief.";
        const segmentCount = Array.isArray(outline?.segments)
          ? (outline?.segments?.length ?? 0)
          : 0;
        const brollClips = Array.isArray(row.broll_clips) ? row.broll_clips.length : 0;
        const duration =
          typeof row.duration_actual_s === "number" && row.duration_actual_s > 0
            ? `${row.duration_actual_s}s`
            : "duration TBD";

        return (
          <li key={row.id} className="flex flex-col gap-2 rounded-md border bg-card p-3 shadow-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                <Film className="h-3.5 w-3.5" aria-hidden="true" />
                {row.status ?? "draft"}
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">{duration}</span>
            </div>
            <p className="line-clamp-3 text-sm text-foreground" title={hook}>
              {hook}
            </p>
            <p className="text-[11px] text-muted-foreground">
              {segmentCount} {segmentCount === 1 ? "segment" : "segments"} ·{" "}
              {brollClips > 0 ? `${brollClips} b-roll clips planned` : "b-roll plan pending"}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

function SkeletonCardRow({ count }: { count: number }) {
  return (
    <ul
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4"
      aria-hidden="true"
    >
      {Array.from({ length: count }).map((_, idx) => (
        <li
          key={idx}
          className="flex animate-pulse flex-col gap-2 overflow-hidden rounded-md border bg-card"
        >
          <div className="aspect-square w-full bg-muted/50" />
          <div className="flex flex-col gap-1 px-2.5 pb-2.5">
            <div className="h-3 w-3/4 rounded bg-muted/60" />
            <div className="h-2.5 w-1/3 rounded bg-muted/40" />
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Approval gate — pipeline-flavoured. Mirrors `<ApprovalGate />` from
// `components/brief/ApprovalGate.tsx`; we keep a thin local copy because the
// brief gate is bolted to `/api/briefs/:id/approve` and the brief's
// `DecisionInput` schema. Wrapping it would invert that coupling.
// ---------------------------------------------------------------------------

function PipelineApprovalGate({
  pipelineId,
  disabled,
  onDecided,
}: {
  pipelineId: string;
  disabled: boolean;
  onDecided: () => void;
}) {
  const [decision, setDecision] = useState<DecisionT>("approved");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const lastSubmittedRef = useRef<DecisionT | null>(null);

  const requiresNotes = decision !== "approved";

  const submit = useCallback(
    (next: DecisionT) => {
      setError(null);
      setDecision(next);

      // Client-side validation mirrors the server: notes required on
      // approved_with_changes / rejected.
      if (next !== "approved" && notes.trim().length === 0) {
        setError("Notes are required for approve-with-changes and reject.");
        return;
      }

      lastSubmittedRef.current = next;
      startTransition(async () => {
        try {
          await submitReviewDecision(pipelineId, {
            decision: next,
            notes: notes.trim() ? notes.trim() : undefined,
          });
          setNotes("");
          onDecided();
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          lastSubmittedRef.current = null;
        }
      });
    },
    [notes, onDecided, pipelineId],
  );

  return (
    <div className="space-y-4 rounded-md border bg-card p-4 shadow-sm">
      <div className="space-y-1">
        <h3 className="text-lg font-semibold">Decide on this pipeline</h3>
        <p className="text-sm text-muted-foreground">
          Approve to start generation. Notes are required when approving with changes or rejecting.
        </p>
      </div>

      <div className="grid gap-2">
        <Label htmlFor="pipeline-review-notes">
          Notes{requiresNotes ? <span className="text-destructive"> *</span> : null}
        </Label>
        <Textarea
          id="pipeline-review-notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          onFocus={() => setError(null)}
          rows={3}
          placeholder={
            requiresNotes
              ? "Explain what needs to change or why this is rejected."
              : "Optional — anything you want to record."
          }
          aria-invalid={requiresNotes && notes.trim().length === 0}
        />
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:gap-3">
        <Button
          type="button"
          disabled={isPending || disabled}
          onClick={() => submit("approved")}
          className={cn("min-h-11")}
        >
          {isPending && decision === "approved" ? "Approving…" : "Approve"}
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={isPending || disabled}
          onClick={() => submit("approved_with_changes")}
          className={cn("min-h-11")}
        >
          {isPending && decision === "approved_with_changes"
            ? "Submitting…"
            : "Approve with changes"}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={isPending || disabled}
          onClick={() => submit("rejected")}
          className={cn("min-h-11")}
        >
          {isPending && decision === "rejected" ? "Rejecting…" : "Reject"}
        </Button>
      </div>
    </div>
  );
}
