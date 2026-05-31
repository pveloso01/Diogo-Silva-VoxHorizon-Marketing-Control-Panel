import Link from "next/link";
import type { Route } from "next";
import { notFound } from "next/navigation";
import { Bell } from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { ArchivePipelineButton } from "@/components/pipeline/ArchivePipelineButton";
import { CancelPipelineButton } from "@/components/pipeline/CancelPipelineButton";
import { MonitorDashboard } from "@/components/monitor/MonitorDashboard";
import { OperatorNarration } from "@/components/pipeline/OperatorNarration";
import { PhaseStepper } from "@/components/pipeline/PhaseStepper";
import { PipelineDetailRealtime } from "@/components/pipeline/PipelineDetailRealtime";
import { StageConfiguration } from "@/components/pipeline/StageConfiguration";
import { StageCopy } from "@/components/pipeline/StageCopy";
import { StageCreativeReview } from "@/components/pipeline/StageCreativeReview";
import { StageDone } from "@/components/pipeline/StageDone";
import { StageGeneration } from "@/components/pipeline/StageGeneration";
import { StageIdeation } from "@/components/pipeline/StageIdeation";
import { StagePlaceholder } from "@/components/pipeline/StagePlaceholder";
import { StageReview } from "@/components/pipeline/StageReview";
import { StageVariantPlan } from "@/components/pipeline/StageVariantPlan";
import { VariantPlanEditor } from "@/components/pipeline/VariantPlanEditor";
import { LaunchGate } from "@/components/launch/LaunchGate";
import type { Brief } from "@/lib/briefs";
import { getMonitorRows } from "@/lib/monitor/fetch";
import { getPipelineQuery } from "@/lib/pipeline/queries";
import { getClientCplTarget, getCopyVariants, getReviewBundle } from "@/lib/review/fetch";
import { getVariantPlanEditorData } from "@/lib/variant-plan/fetch";
import type { VariantTestVariable } from "@/lib/variant-plan/schemas";
import { type Pipeline, type PipelineEvent, type PipelineStatus } from "@/lib/pipeline/types";
import type { WorkItem } from "@/lib/work-queue/types";
import { createAdminClient } from "@/lib/supabase/admin";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return { title: `Pipeline ${id.slice(0, 8)} — VoxHorizon` };
}

const STAGE_PLACEHOLDER_LABEL: Record<PipelineStatus, { label: string; wave: string }> = {
  configuration: { label: "Configuration", wave: "PF-B-X" },
  ideation: { label: "Ideation", wave: "PF-C-X" },
  review: { label: "Review", wave: "PF-D-X" },
  generation: { label: "Generation", wave: "PF-E-X" },
  // New 12-stage flow — real stage components land in P4 (UX rebuild); until
  // then these render via the StagePlaceholder fall-through in the switch below.
  creative_qa: { label: "Creative QA", wave: "P4" },
  compliance_review: { label: "Compliance", wave: "P4" },
  copy: { label: "Copy", wave: "P4" },
  spec_validation: { label: "Spec Validation", wave: "P4" },
  variant_plan: { label: "Variant Plan", wave: "P4" },
  finalize_assets: { label: "Finalize", wave: "P4" },
  launch_handoff: { label: "Launch", wave: "P4" },
  monitor: { label: "Monitor", wave: "P4" },
  done: { label: "Done", wave: "PF-F-X" },
  cancelled: { label: "Cancelled", wave: "PF-X-X" },
};

function shortClientLabel(p: Pipeline, clientName: string | null): string {
  if (clientName) return clientName;
  if (p.client_id) return p.client_id.slice(0, 8);
  return "Unassigned client";
}

export default async function PipelineDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  const res = await getPipelineQuery(id);
  if (!res) {
    notFound();
  }
  const pipeline: Pipeline = res.pipeline;
  const initialEvents: PipelineEvent[] = res.events ?? [];
  // The canonical operator brief (briefs row) the configuration stage renders
  // for operator-driven pipelines. `getPipelineQuery` already embeds it.
  const imageBrief = (res.image_brief ?? null) as Brief | null;

  let clientName: string | null = null;
  let clients: {
    id: string;
    name: string;
    slug: string;
    service_type: "roofing" | "remodeling";
  }[] = [];
  const supabase = await createClient();
  // The header's client name and (in configuration) the StageConfiguration
  // picker's active-client list are independent reads, so fetch them in one
  // parallel batch rather than on two serial round-trips (~140ms each to
  // us-east). Each is conditional; an unused slot resolves instantly. Server-
  // fetching the list avoids the form's first-paint flicker and keeps the auth
  // path consistent with the rest of the app.
  const [clientNameRes, clientListRes] = await Promise.all([
    pipeline.client_id
      ? supabase.from("clients").select("name").eq("id", pipeline.client_id).maybeSingle()
      : Promise.resolve({ data: null, error: null }),
    pipeline.status === "configuration"
      ? supabase
          .from("clients")
          .select("id, name, slug, service_type")
          .eq("status", "active")
          .order("name")
      : Promise.resolve({ data: [] as typeof clients, error: null }),
  ]);
  if (pipeline.client_id) clientName = clientNameRes.data?.name ?? null;
  if (pipeline.status === "configuration") {
    clients = (clientListRes.data ?? []) as typeof clients;
  }

  // Per-creative review surfaces (P4.2–P4.6) read through the service-role
  // fetch helpers (RLS deny-all on the new tables). We pull only what the
  // current stage needs so legacy stages stay light.
  const PER_CREATIVE: PipelineStatus[] = [
    "creative_qa",
    "compliance_review",
    "copy",
    "spec_validation",
  ];
  const reviewBundle =
    PER_CREATIVE.includes(pipeline.status) || pipeline.status === "launch_handoff"
      ? await getReviewBundle(pipeline.id)
      : null;
  // The copy stage lists a CopyComposer per creative; reviewBundle (fetched for
  // the PER_CREATIVE set, which includes "copy") carries the creatives.
  const copyVariants = pipeline.status === "copy" ? await getCopyVariants(pipeline.id) : [];
  const variantPlanData =
    pipeline.status === "variant_plan" ? await getVariantPlanEditorData(pipeline.id) : null;
  const monitorRows = pipeline.status === "monitor" ? await getMonitorRows(pipeline.id) : [];
  const cplTarget =
    pipeline.status === "monitor" ? await getClientCplTarget(pipeline.client_id) : null;

  // Silent-failure PR-5: SSR-seed the active OPERATOR dispatch for the stages
  // that mount `WorkItemPanelSlot`. The WorkItemPanel is the "what is the
  // operator dispatcher doing right now?" surface, so the slot only ever shows
  // for an active `operator_dispatch` work_item (kickoff / recovery on an
  // operator-driven pipeline).
  //
  // It deliberately does NOT seed off deterministic `worker_*` queue rows
  // (worker_ideation / worker_generation): on the normal flow those are an
  // internal implementation detail that can linger queued/claimed across the
  // ideation/review/generation stages, and surfacing them would mount the panel
  // (opening a realtime channel + daemon-health fetch) on every normal-flow
  // stage -- the exact PR-3 stall. Filtering to `operator_dispatch` keeps the
  // slot hidden on the deterministic flow (no panel, no channel) and live on the
  // operator flow it is built for.
  //
  // Reads the active row DIRECTLY (not via `v_pipeline_dispatch_state`) so the
  // render never depends on the view's per-row `compute_pipeline_status()`.
  // Gated to the three slot-bearing stages; fully defensive so a seed failure
  // can never break the stage (the slot just stays hidden).
  const SLOT_STAGES: PipelineStatus[] = ["ideation", "review", "generation"];
  let initialWorkItem: WorkItem | null = null;
  if (SLOT_STAGES.includes(pipeline.status)) {
    try {
      const dispatch = await createAdminClient()
        .from("work_item")
        .select("*")
        .eq("pipeline_id", pipeline.id)
        .eq("kind", "operator_dispatch")
        .in("status", ["queued", "claimed", "running"])
        .order("created_at", { ascending: false })
        .limit(1)
        .maybeSingle();
      initialWorkItem = (dispatch.data as WorkItem | null) ?? null;
    } catch {
      // Seeding is best-effort: the slot falls back to hidden and the client
      // re-seeds on the next router.refresh(). Never block the stage on it.
      initialWorkItem = null;
    }
  }

  const placeholder = STAGE_PLACEHOLDER_LABEL[pipeline.status];
  const isArchived = pipeline.deleted_at !== null;
  // Cancel is the in-flight escape hatch; archive is the "remove from my view"
  // soft-delete. An archived run is read-only chrome, so suppress cancel.
  const isCancellable =
    pipeline.status !== "done" && pipeline.status !== "cancelled" && !isArchived;
  // Spend approvals for this run surface in the global ApprovalQueue (header
  // bell) via the plugin→worker→dashboard flow. We also link straight to the
  // audit page filtered to this pipeline's operator session (the operator runs
  // with session_id = pipeline.id) so a supervisor can review render-spend
  // decisions in context.
  const approvalsHref = `/approvals?session=${encodeURIComponent(pipeline.id)}` as Route;

  return (
    <main className="flex min-h-dvh w-full flex-col gap-6 px-4 py-6 sm:px-6 sm:py-12">
      <PipelineDetailRealtime pipelineId={pipeline.id} />

      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">
          <Link href="/pipeline" className="underline-offset-4 hover:underline">
            Pipeline
          </Link>{" "}
          / <span className="font-mono">{pipeline.id.slice(0, 8)}</span>
        </p>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-wrap items-center gap-2 sm:gap-3">
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl md:text-3xl">
              {shortClientLabel(pipeline, clientName)}
            </h1>
            <StatusBadge status={pipeline.status} />
            <StatusBadge status={pipeline.format_choice} />
          </div>
          <div className="flex flex-wrap items-center gap-2 self-start">
            {isCancellable ? <CancelPipelineButton pipelineId={pipeline.id} /> : null}
            <ArchivePipelineButton pipelineId={pipeline.id} archived={isArchived} />
          </div>
        </div>
      </header>

      {isArchived ? (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning">
          This pipeline is archived and hidden from the active list. Restore it to bring it back.
        </div>
      ) : null}

      {pipeline.status === "cancelled" ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          This pipeline was cancelled. No further actions are available.
        </div>
      ) : (
        <section
          aria-label="Pipeline phases"
          className="rounded-lg border border-border bg-card px-4 py-4 sm:px-6 sm:py-5"
        >
          <PhaseStepper current={pipeline.status} />
        </section>
      )}

      {pipeline.status === "cancelled" ? (
        // Terminal: no supervision sidebar — the run is done.
        <StagePlaceholder
          stageLabel={placeholder.label}
          upcoming={placeholder.wave}
          subtitle="Cancelled pipelines do not advance further."
        />
      ) : (
        // Supervision cockpit: stage UI on the main column, operator narration
        // + spend-approvals access alongside it. The narration view reuses the
        // realtime relay (pipeline_events), and the spend gate itself lands in
        // the global ApprovalQueue (header bell) via the approval plugin.
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] xl:grid-cols-[20rem_minmax(0,1fr)_22rem]">
          {/* Left context rail. Shown only at xl+ so the existing single-column
              (mobile) and 2-zone (lg laptop) layouts are unchanged; the freed
              width on wide screens carries the run context a supervisor keeps
              referencing while judging a stage. All data is already on the page. */}
          <aside className="hidden flex-col gap-4 xl:sticky xl:top-6 xl:flex xl:self-start">
            <div className="rounded-lg border border-border bg-card px-4 py-4 text-sm">
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Run context
              </h2>
              <dl className="space-y-2.5">
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-muted-foreground">Client</dt>
                  <dd className="text-right font-medium">
                    {shortClientLabel(pipeline, clientName)}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd>
                    <StatusBadge status={pipeline.status} />
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Format</dt>
                  <dd>
                    <StatusBadge status={pipeline.format_choice} />
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-muted-foreground">Pipeline</dt>
                  <dd className="font-mono text-xs">{pipeline.id.slice(0, 8)}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-muted-foreground">Created</dt>
                  <dd className="text-right">
                    {new Date(pipeline.created_at).toLocaleDateString()}
                  </dd>
                </div>
              </dl>
            </div>
          </aside>
          <div className="flex min-w-0 flex-col gap-6">
            {pipeline.status === "configuration" ? (
              <StageConfiguration pipeline={pipeline} clients={clients} imageBrief={imageBrief} />
            ) : pipeline.status === "ideation" ? (
              <StageIdeation
                pipeline={pipeline}
                imageBriefId={pipeline.image_brief_id}
                videoBriefId={pipeline.video_brief_id}
                initialWorkItem={initialWorkItem}
              />
            ) : pipeline.status === "review" ? (
              <StageReview
                pipeline={pipeline}
                imageBriefId={pipeline.image_brief_id}
                videoBriefId={pipeline.video_brief_id}
                initialWorkItem={initialWorkItem}
              />
            ) : pipeline.status === "generation" ? (
              <StageGeneration
                pipeline={pipeline}
                initialEvents={initialEvents}
                initialWorkItem={initialWorkItem}
              />
            ) : (pipeline.status === "creative_qa" ||
                pipeline.status === "spec_validation" ||
                pipeline.status === "compliance_review") &&
              reviewBundle ? (
              <StageCreativeReview
                pipelineId={pipeline.id}
                mode={pipeline.status}
                creatives={reviewBundle.creatives}
                states={reviewBundle.states}
                signedUrls={reviewBundle.signedUrls}
                advisories={reviewBundle.advisories}
              />
            ) : pipeline.status === "copy" && reviewBundle ? (
              <StageCopy
                pipelineId={pipeline.id}
                creatives={reviewBundle.creatives}
                variants={copyVariants}
              />
            ) : pipeline.status === "variant_plan" ? (
              <div className="flex flex-col gap-6">
                <VariantPlanEditor
                  pipelineId={pipeline.id}
                  planExists={variantPlanData?.plan !== null && variantPlanData?.plan !== undefined}
                  locked={variantPlanData?.plan?.status === "approved"}
                  testVariable={
                    (variantPlanData?.plan?.test_variable as VariantTestVariable | undefined) ??
                    null
                  }
                  hypothesis={variantPlanData?.plan?.hypothesis ?? null}
                  initialCells={variantPlanData?.cells ?? []}
                  creatives={variantPlanData?.creatives ?? []}
                  copyVariants={variantPlanData?.copyVariants ?? []}
                />
                <StageVariantPlan
                  pipelineId={pipeline.id}
                  testVariable={variantPlanData?.plan?.test_variable ?? null}
                  hypothesis={variantPlanData?.plan?.hypothesis ?? null}
                  cells={(variantPlanData?.cells ?? []).map((c) => ({
                    id: c.id,
                    cell_index: c.cell_index,
                    label: c.label,
                    creative_id: c.creative_id,
                    copy_variant_id: c.copy_variant_id,
                  }))}
                />
              </div>
            ) : pipeline.status === "launch_handoff" && reviewBundle ? (
              <LaunchGate
                pipelineId={pipeline.id}
                creatives={reviewBundle.creatives}
                states={reviewBundle.states}
                copyVariants={reviewBundle.copyVariants}
              />
            ) : pipeline.status === "monitor" ? (
              <MonitorDashboard pipelineId={pipeline.id} rows={monitorRows} cplTarget={cplTarget} />
            ) : pipeline.status === "done" ? (
              <StageDone
                pipeline={pipeline}
                imageBriefId={pipeline.image_brief_id}
                videoBriefId={pipeline.video_brief_id}
              />
            ) : (
              // finalize_assets (auto stage) + any legacy status fall through to
              // the placeholder — no live run breaks (strangler-fig).
              <StagePlaceholder stageLabel={placeholder.label} upcoming={placeholder.wave} />
            )}
          </div>

          <aside className="flex flex-col gap-4 lg:sticky lg:top-6 lg:self-start">
            <OperatorNarration pipelineId={pipeline.id} initialEvents={initialEvents} />
            <div className="rounded-lg border border-border bg-card px-4 py-4 text-sm">
              <div className="flex items-center gap-2">
                <Bell aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
                <h2 className="text-sm font-semibold tracking-tight">Spend approvals</h2>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Render-spend requests pop up in the header bell. Review this run&apos;s decisions
                below.
              </p>
              <Button asChild variant="outline" size="sm" className="mt-3 w-full">
                <Link href={approvalsHref}>View spend approvals</Link>
              </Button>
            </div>
          </aside>
        </div>
      )}
    </main>
  );
}
