/**
 * GateReviewPanel: the unified manager-clear UI for soft per-creative gate
 * states. Drives both stages (compliance_review + spec_validation): hard-block
 * override (type-to-confirm) + soft advisory accept (note only), the audited
 * Continue gate, advisory display, and the unified /gate/decision POST.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, spyOnFetch } from "@/tests/unit/helpers/worker-mock";

const routerRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: routerRefresh, push: vi.fn(), replace: vi.fn() }),
}));

import { GateReviewPanel } from "./GateReviewPanel";
import type { GateAdvisory } from "@/lib/review/fetch";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";

const creatives: GridCreative[] = [
  { id: "a", concept: "Concept A", status: "draft" },
  { id: "b", concept: "Concept B", status: "draft" },
];

const complianceBlocked: StageStateRow[] = [
  { creative_id: "a", stage: "compliance_review", status: "failed", override_note: null },
  { creative_id: "b", stage: "compliance_review", status: "passed", override_note: null },
];

const complianceCleared: StageStateRow[] = [
  { creative_id: "a", stage: "compliance_review", status: "overridden", override_note: "ok" },
  { creative_id: "b", stage: "compliance_review", status: "passed", override_note: null },
];

// Spec warn lands as in_progress: not failed, not cleared. The case that stuck.
const specWarn: StageStateRow[] = [
  { creative_id: "a", stage: "spec_validation", status: "in_progress", override_note: null },
  { creative_id: "b", stage: "spec_validation", status: "passed", override_note: null },
];

const specAdvisories: GateAdvisory[] = [
  {
    creative_id: "a",
    stage: "spec_validation",
    label: "meta feed 4x5 (warn)",
    detail: "Confirm the before/after crop does not cut either side.",
    severity: "warn",
  },
];

beforeEach(() => routerRefresh.mockReset());
afterEach(() => vi.restoreAllMocks());

describe("GateReviewPanel", () => {
  it("compliance: hard block disables Continue + shows the banner", () => {
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="compliance_review"
        creatives={creatives}
        states={complianceBlocked}
      />,
    );
    expect(screen.getByTestId("hard-block-banner")).toBeInTheDocument();
    expect(screen.getByTestId("gate-continue")).toBeDisabled();
  });

  it("compliance: enables Continue once the rollup clears", () => {
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="compliance_review"
        creatives={creatives}
        states={complianceCleared}
      />,
    );
    expect(screen.getByTestId("gate-continue")).not.toBeDisabled();
  });

  it("override needs justification + type-to-confirm", async () => {
    const user = userEvent.setup();
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="compliance_review"
        creatives={creatives}
        states={complianceBlocked}
      />,
    );
    await user.click(screen.getByTestId("override-open-a"));
    const submit = screen.getByTestId("decision-submit");
    expect(submit).toBeDisabled();
    await user.type(screen.getByTestId("decision-note"), "Reviewed by legal");
    expect(submit).toBeDisabled();
    await user.type(screen.getByTestId("decision-confirm"), "OVERRIDE");
    expect(submit).not.toBeDisabled();
  });

  it("override POSTs /gate/decision with decision=override", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));
    const user = userEvent.setup();
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="compliance_review"
        creatives={creatives}
        states={complianceBlocked}
      />,
    );
    await user.click(screen.getByTestId("override-open-a"));
    await user.type(screen.getByTestId("decision-note"), "Reviewed by legal");
    await user.type(screen.getByTestId("decision-confirm"), "OVERRIDE");
    await user.click(screen.getByTestId("decision-submit"));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/pipelines/p1/gate/decision",
        expect.objectContaining({ method: "POST" }),
      );
      const sentBody = JSON.parse((fetchSpy.mock.calls[0]![1] as { body: string }).body);
      expect(sentBody).toMatchObject({ stage: "compliance_review", decision: "override" });
      expect(routerRefresh).toHaveBeenCalled();
    });
  });

  it("spec: a warn (in_progress) holds Continue + offers Review & accept (not override)", () => {
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="spec_validation"
        creatives={creatives}
        states={specWarn}
        advisories={specAdvisories}
      />,
    );
    expect(screen.queryByTestId("hard-block-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId("gate-continue")).toBeDisabled();
    expect(screen.getByTestId("needs-review-list")).toBeInTheDocument();
    expect(screen.getByTestId("accept-open-a")).toBeInTheDocument();
    expect(screen.queryByTestId("override-open-a")).not.toBeInTheDocument();
    // The advisory detail is surfaced so the accept is informed.
    expect(screen.getByText(/before\/after crop/)).toBeInTheDocument();
  });

  it("spec accept needs only a note (no confirm) and POSTs decision=accept", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));
    const user = userEvent.setup();
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="spec_validation"
        creatives={creatives}
        states={specWarn}
        advisories={specAdvisories}
      />,
    );
    await user.click(screen.getByTestId("accept-open-a"));
    expect(screen.queryByTestId("decision-confirm")).not.toBeInTheDocument();
    expect(screen.getByTestId("decision-submit")).not.toBeDisabled();
    await user.click(screen.getByTestId("decision-submit"));
    await waitFor(() => {
      const sentBody = JSON.parse((fetchSpy.mock.calls[0]![1] as { body: string }).body);
      expect(sentBody).toMatchObject({ stage: "spec_validation", decision: "accept" });
      expect(routerRefresh).toHaveBeenCalled();
    });
  });

  it("surfaces a decision error inline", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ error: "denied" }, { status: 403 }));
    const user = userEvent.setup();
    render(
      <GateReviewPanel
        pipelineId="p1"
        stage="spec_validation"
        creatives={creatives}
        states={specWarn}
      />,
    );
    await user.click(screen.getByTestId("accept-open-a"));
    await user.click(screen.getByTestId("decision-submit"));
    await waitFor(() => expect(screen.getByText("denied")).toBeInTheDocument());
  });
});
