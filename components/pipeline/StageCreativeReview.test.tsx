/**
 * StageCreativeReview: mode-parameterized host for the four per-creative stages.
 * Covers the grid render, the compliance hard-gate swap, the rollup-gated
 * Continue, the drawer drill-in, and the advance POST.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, spyOnFetch } from "@/tests/unit/helpers/worker-mock";

const routerRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: routerRefresh, push: vi.fn(), replace: vi.fn() }),
}));

import { StageCreativeReview } from "./StageCreativeReview";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";

const creatives: GridCreative[] = [{ id: "a", concept: "Concept A", status: "draft" }];
const passedQa: StageStateRow[] = [
  { creative_id: "a", stage: "creative_qa", status: "passed", override_note: null },
];
const pendingQa: StageStateRow[] = [
  { creative_id: "a", stage: "creative_qa", status: "pending", override_note: null },
];

beforeEach(() => routerRefresh.mockReset());
afterEach(() => vi.restoreAllMocks());

describe("StageCreativeReview", () => {
  it("renders the grid for a non-compliance stage", () => {
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    expect(screen.getByTestId("creative-review-grid")).toBeInTheDocument();
  });

  it("disables Continue until the rollup clears", () => {
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={pendingQa}
        signedUrls={{}}
      />,
    );
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
  });

  it("advances when the rollup is cleared", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ pipeline: { status: "compliance_review" } }));
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/pipelines/p1/advance",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("surfaces an advance error", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ error: "blocked" }, { status: 422 }));
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => expect(screen.getByText("blocked")).toBeInTheDocument());
  });

  it("renders the compliance hard gate for compliance_review mode", () => {
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="compliance_review"
        creatives={creatives}
        states={[
          { creative_id: "a", stage: "compliance_review", status: "failed", override_note: null },
        ]}
        signedUrls={{}}
      />,
    );
    expect(screen.getByTestId("gate-review-panel")).toBeInTheDocument();
  });

  it("opens the drawer when a creative is drilled into (with no signed url)", async () => {
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByTestId("grid-open-a"));
    expect(screen.getByTestId("review-drawer")).toBeInTheDocument();
  });

  it("surfaces a network error on advance", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockRejectedValue(new Error("offline"));
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => expect(screen.getByText("offline")).toBeInTheDocument());
  });

  it("falls back to a status message when the advance error body is empty", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({}, { status: 502 }));
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="creative_qa"
        creatives={creatives}
        states={passedQa}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => expect(screen.getByText(/502/)).toBeInTheDocument());
  });

  it("advances from the compliance gate's Continue", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ pipeline: { status: "copy" } }));
    const user = userEvent.setup();
    render(
      <StageCreativeReview
        pipelineId="p1"
        mode="compliance_review"
        creatives={creatives}
        states={[
          { creative_id: "a", stage: "compliance_review", status: "passed", override_note: null },
        ]}
        signedUrls={{}}
      />,
    );
    await user.click(screen.getByTestId("gate-continue"));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/pipelines/p1/advance",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
