import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { mockSupabaseClient, type SupabaseClientMock } from "@/tests/unit/helpers/supabase-mock";

// The page imports the server-only review/monitor fetch helpers; neutralise the
// sentinel and stub the helpers so this jsdom test can import the page.
vi.mock("server-only", () => ({}));

const getReviewBundle = vi.fn(async () => ({
  creatives: [{ id: "a", concept: "A", status: "draft" }],
  states: [],
  copyVariants: [],
  signedUrls: {},
}));
const getCopyVariants = vi.fn(async () => []);
const getClientCplTarget = vi.fn(async () => null);
vi.mock("@/lib/review/fetch", () => ({
  getReviewBundle: (...a: unknown[]) => getReviewBundle(...(a as [])),
  getCopyVariants: (...a: unknown[]) => getCopyVariants(...(a as [])),
  getClientCplTarget: (...a: unknown[]) => getClientCplTarget(...(a as [])),
}));
const getVariantPlanEditorData = vi.fn(async () => ({
  plan: null,
  cells: [],
  creatives: [],
  copyVariants: [],
}));
vi.mock("@/lib/variant-plan/fetch", () => ({
  getVariantPlanEditorData: (...a: unknown[]) => getVariantPlanEditorData(...(a as [])),
}));
const getMonitorRows = vi.fn(async () => []);
vi.mock("@/lib/monitor/fetch", () => ({
  getMonitorRows: (...a: unknown[]) => getMonitorRows(...(a as [])),
}));

const getPipelineQuery = vi.fn();
vi.mock("@/lib/pipeline/queries", () => ({
  getPipelineQuery: (...args: unknown[]) => getPipelineQuery(...args),
}));

let currentSupabase: SupabaseClientMock;
vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => currentSupabase,
}));

// PR-5: the page SSR-seeds the active work_item from `v_pipeline_dispatch_state`
// via the admin (service-role) client for the ideation/review/generation
// stages. Mock the admin client so the jsdom test can render those branches
// without real env / network. `currentAdmin` lets a test control the seed.
let currentAdmin: SupabaseClientMock;
vi.mock("@/lib/supabase/admin", () => ({
  createAdminClient: () => currentAdmin,
}));

// Stub all stage components so we don't need their full mocks.
vi.mock("@/components/pipeline/PipelineDetailRealtime", () => ({
  PipelineDetailRealtime: () => <div data-testid="realtime" />,
}));
vi.mock("@/components/pipeline/CancelPipelineButton", () => ({
  CancelPipelineButton: () => <button data-testid="cancel">cancel</button>,
}));
vi.mock("@/components/pipeline/ArchivePipelineButton", () => ({
  ArchivePipelineButton: ({ archived }: { archived: boolean }) => (
    <button data-testid="archive" data-archived={archived}>
      {archived ? "restore" : "archive"}
    </button>
  ),
}));
vi.mock("@/components/pipeline/PhaseStepper", () => ({
  PhaseStepper: ({ current }: { current: string }) => (
    <div data-testid="stepper" data-current={current} />
  ),
}));
vi.mock("@/components/pipeline/StageCreativeReview", () => ({
  StageCreativeReview: ({ mode }: { mode: string }) => (
    <div data-testid="stage" data-stage={mode} />
  ),
}));
vi.mock("@/components/pipeline/StageCopy", () => ({
  StageCopy: () => <div data-testid="stage" data-stage="copy" />,
}));
vi.mock("@/components/pipeline/StageVariantPlan", () => ({
  StageVariantPlan: () => <div data-testid="stage" data-stage="variant_plan" />,
}));
vi.mock("@/components/pipeline/VariantPlanEditor", () => ({
  VariantPlanEditor: () => <div data-testid="variant-plan-editor" />,
}));
vi.mock("@/components/launch/LaunchGate", () => ({
  LaunchGate: () => <div data-testid="stage" data-stage="launch_handoff" />,
}));
vi.mock("@/components/monitor/MonitorDashboard", () => ({
  MonitorDashboard: () => <div data-testid="stage" data-stage="monitor" />,
}));
vi.mock("@/components/pipeline/StageConfiguration", () => ({
  StageConfiguration: ({ clients }: { clients: unknown[] }) => (
    <div data-testid="stage" data-stage="configuration" data-clients={clients.length} />
  ),
}));
vi.mock("@/components/pipeline/StageIdeation", () => ({
  StageIdeation: ({ initialWorkItem }: { initialWorkItem?: { id: string } | null }) => (
    <div data-testid="stage" data-stage="ideation" data-work-item={initialWorkItem?.id ?? "none"} />
  ),
}));
vi.mock("@/components/pipeline/StageReview", () => ({
  StageReview: ({ initialWorkItem }: { initialWorkItem?: { id: string } | null }) => (
    <div data-testid="stage" data-stage="review" data-work-item={initialWorkItem?.id ?? "none"} />
  ),
}));
vi.mock("@/components/pipeline/StageGeneration", () => ({
  StageGeneration: ({ initialWorkItem }: { initialWorkItem?: { id: string } | null }) => (
    <div
      data-testid="stage"
      data-stage="generation"
      data-work-item={initialWorkItem?.id ?? "none"}
    />
  ),
}));
vi.mock("@/components/pipeline/StageDone", () => ({
  StageDone: () => <div data-testid="stage" data-stage="done" />,
}));
vi.mock("@/components/pipeline/StagePlaceholder", () => ({
  StagePlaceholder: ({ stageLabel }: { stageLabel: string }) => (
    <div data-testid="placeholder" data-label={stageLabel} />
  ),
}));
// OperatorNarration uses the realtime hook; stub it so the server-component
// render doesn't open an SSE subscription.
vi.mock("@/components/pipeline/OperatorNarration", () => ({
  OperatorNarration: ({ pipelineId }: { pipelineId: string }) => (
    <div data-testid="operator-narration" data-pipeline={pipelineId} />
  ),
}));

const notFoundSpy = vi.fn(() => {
  throw new Error("__NOT_FOUND__");
});
vi.mock("next/navigation", () => ({
  notFound: () => notFoundSpy(),
}));

import PipelineDetailPage, { generateMetadata } from "./page";

function pipeline(over: Record<string, unknown>) {
  return {
    id: "abcd1234-rest",
    status: "configuration",
    format_choice: "image",
    client_id: "c1",
    image_brief_id: null,
    video_brief_id: null,
    config_draft: null,
    picks: null,
    cost_estimate: null,
    cost_actual: null,
    approval: null,
    launch_package_id: null,
    created_at: "2026-05-17",
    updated_at: "2026-05-17",
    advanced_at: null,
    deleted_at: null,
    ...over,
  };
}

describe("PipelineDetailPage", () => {
  beforeEach(() => {
    // Default: no active work_item row, so the slot stays hidden. Tests that
    // need a seed override `currentAdmin`.
    currentAdmin = mockSupabaseClient({
      work_item: {
        select: { data: null, error: null, single: { data: null, error: null } },
      },
    });
  });

  it("renders configuration stage with clients list", async () => {
    getPipelineQuery.mockResolvedValueOnce({ pipeline: pipeline({}), events: [] });
    currentSupabase = mockSupabaseClient({
      clients: {
        select: {
          single: { data: { name: "Acme" }, error: null },
          data: [{ id: "c1", name: "Acme", slug: "acme", service_type: "roofing" }],
          error: null,
        },
      },
    });
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "abcd1234-rest" }) });
    render(el);
    // "Acme" now appears in the header AND the new run-context rail.
    expect(screen.getAllByText("Acme").length).toBeGreaterThan(0);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "configuration");
  });

  it("shows the operator narration + a scoped spend-approvals link on active runs", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "ideation", id: "abcd1234-rest" }),
      events: [{ id: "e1" }],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "abcd1234-rest" }) });
    render(el);
    expect(screen.getByTestId("operator-narration")).toHaveAttribute(
      "data-pipeline",
      "abcd1234-rest",
    );
    const approvalsLink = screen.getByRole("link", { name: /view spend approvals/i });
    expect(approvalsLink).toHaveAttribute("href", "/approvals?session=abcd1234-rest");
  });

  it("hides the supervision sidebar on a cancelled run", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "cancelled" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    // Cancelled pipelines render the placeholder branch but no narration sidebar.
    expect(screen.queryByTestId("operator-narration")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /view spend approvals/i })).not.toBeInTheDocument();
  });

  it("renders ideation stage", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "ideation" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient({
      clients: { select: { data: null, error: null, single: { data: null, error: null } } },
    });
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "ideation");
  });

  it("renders review stage", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "review" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "review");
  });

  it("renders generation stage with events", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "generation" }),
      events: [{ id: "e1" }],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "generation");
  });

  it("PR-5: SSR-seeds the active work_item into the ideation stage", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "ideation" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    currentAdmin = mockSupabaseClient({
      work_item: {
        select: {
          data: null,
          error: null,
          single: { data: { id: "wi-42" }, error: null },
        },
      },
    });
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    const stage = screen.getByTestId("stage");
    expect(stage).toHaveAttribute("data-stage", "ideation");
    expect(stage).toHaveAttribute("data-work-item", "wi-42");
    // The active work_item was read via the admin client.
    expect(currentAdmin._spies.from).toHaveBeenCalledWith("work_item");
  });

  it("PR-5: threads a null seed when the dispatcher is idle on review", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "review" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    const stage = screen.getByTestId("stage");
    expect(stage).toHaveAttribute("data-stage", "review");
    expect(stage).toHaveAttribute("data-work-item", "none");
  });

  it("PR-5: does NOT read the work_item seed on non-slot stages", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "monitor" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    // The slot only lives on ideation/review/generation -- monitor must not
    // pay for the admin seed read.
    expect(currentAdmin._spies.from).not.toHaveBeenCalledWith("work_item");
  });

  it("renders done stage and hides cancel button", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "done" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "done");
    expect(screen.queryByTestId("cancel")).not.toBeInTheDocument();
  });

  it("renders cancelled banner + placeholder", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "cancelled" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(
      screen.getByText(/This pipeline was cancelled\. No further actions/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId("placeholder")).toBeInTheDocument();
  });

  it("calls notFound when the pipeline is missing (null)", async () => {
    getPipelineQuery.mockResolvedValueOnce(null);
    await expect(PipelineDetailPage({ params: Promise.resolve({ id: "x" }) })).rejects.toThrow(
      "__NOT_FOUND__",
    );
  });

  it("propagates a DB error thrown by the query", async () => {
    getPipelineQuery.mockRejectedValueOnce(new Error("boom"));
    await expect(PipelineDetailPage({ params: Promise.resolve({ id: "x" }) })).rejects.toThrow(
      "boom",
    );
  });

  it("falls back to id slice when no client name and no client_id is null", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ client_id: null, status: "ideation" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByRole("heading", { name: /unassigned client/i })).toBeInTheDocument();
  });

  it("uses client id slice when client_id set but lookup returns no name", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "ideation", client_id: "abcd1234-deadbeef" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient({
      clients: { select: { data: null, error: null, single: { data: null, error: null } } },
    });
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByRole("heading", { name: /abcd1234/i })).toBeInTheDocument();
  });

  it("generateMetadata returns truncated id", async () => {
    const m = await generateMetadata({ params: Promise.resolve({ id: "abcdef12-rest" }) });
    expect(m.title).toBe("Pipeline abcdef12 — VoxHorizon");
  });

  it.each(["creative_qa", "compliance_review", "spec_validation"] as const)(
    "routes the %s status to the per-creative review host",
    async (status) => {
      getPipelineQuery.mockResolvedValueOnce({ pipeline: pipeline({ status }), events: [] });
      currentSupabase = mockSupabaseClient();
      const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
      render(el);
      expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", status);
      expect(getReviewBundle).toHaveBeenCalled();
    },
  );

  it("routes copy to StageCopy", async () => {
    getPipelineQuery.mockResolvedValueOnce({ pipeline: pipeline({ status: "copy" }), events: [] });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "copy");
    expect(getCopyVariants).toHaveBeenCalled();
  });

  it("routes variant_plan to StageVariantPlan", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "variant_plan" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "variant_plan");
  });

  it("routes launch_handoff to the LaunchGate", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "launch_handoff" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "launch_handoff");
  });

  it("routes monitor to the MonitorDashboard", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "monitor" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("stage")).toHaveAttribute("data-stage", "monitor");
    expect(getMonitorRows).toHaveBeenCalled();
  });

  it("falls through finalize_assets (auto stage) to the placeholder", async () => {
    getPipelineQuery.mockResolvedValueOnce({
      pipeline: pipeline({ status: "finalize_assets" }),
      events: [],
    });
    currentSupabase = mockSupabaseClient();
    const el = await PipelineDetailPage({ params: Promise.resolve({ id: "x" }) });
    render(el);
    expect(screen.getByTestId("placeholder")).toBeInTheDocument();
  });
});
