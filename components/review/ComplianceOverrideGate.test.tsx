/**
 * ComplianceOverrideGate (#360): hard block disables continue; override needs
 * justification + type-to-confirm; audit visible; POSTs to the override route.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, spyOnFetch } from "@/tests/unit/helpers/worker-mock";

const routerRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: routerRefresh, push: vi.fn(), replace: vi.fn() }),
}));

import { ComplianceOverrideGate } from "./ComplianceOverrideGate";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";

const creatives: GridCreative[] = [
  { id: "a", concept: "Concept A", status: "draft" },
  { id: "b", concept: "Concept B", status: "draft" },
];

const blockedStates: StageStateRow[] = [
  { creative_id: "a", stage: "compliance_review", status: "failed", override_note: null },
  { creative_id: "b", stage: "compliance_review", status: "passed", override_note: null },
];

const clearedStates: StageStateRow[] = [
  { creative_id: "a", stage: "compliance_review", status: "overridden", override_note: "legal ok" },
  { creative_id: "b", stage: "compliance_review", status: "passed", override_note: null },
];

// `needs_review` verdicts land as `pending`: no hard block, but the gate is not
// cleared and there is no `failed` row (so the blocked/override path never
// surfaces them). This is the case that stuck pipeline e087bbe1.
const needsReviewStates: StageStateRow[] = [
  { creative_id: "a", stage: "compliance_review", status: "pending", override_note: null },
  { creative_id: "b", stage: "compliance_review", status: "passed", override_note: null },
];

beforeEach(() => routerRefresh.mockReset());
afterEach(() => vi.restoreAllMocks());

describe("ComplianceOverrideGate", () => {
  it("shows the hard-block banner and disables Continue while blocked", () => {
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    expect(screen.getByTestId("hard-block-banner")).toBeInTheDocument();
    expect(screen.getByTestId("compliance-continue")).toBeDisabled();
  });

  it("enables Continue once the rollup clears", () => {
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={clearedStates} />);
    expect(screen.getByTestId("compliance-continue")).not.toBeDisabled();
  });

  it("shows the override audit for overridden creatives", () => {
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={clearedStates} />);
    expect(screen.getByTestId("override-audit")).toHaveTextContent("legal ok");
  });

  it("requires justification + type-to-confirm before submitting", async () => {
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    const submit = screen.getByTestId("override-submit");
    expect(submit).toBeDisabled();

    await user.type(screen.getByTestId("override-note"), "Reviewed by legal");
    expect(submit).toBeDisabled(); // still need the confirm word

    await user.type(screen.getByTestId("override-confirm"), "OVERRIDE");
    expect(submit).not.toBeDisabled();
  });

  it("POSTs the override and refreshes", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    await user.type(screen.getByTestId("override-note"), "Reviewed by legal");
    await user.type(screen.getByTestId("override-confirm"), "OVERRIDE");
    await user.click(screen.getByTestId("override-submit"));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/pipelines/p1/compliance/override",
        expect.objectContaining({ method: "POST" }),
      );
      expect(routerRefresh).toHaveBeenCalled();
    });
  });

  it("surfaces an override error inline", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ error: "denied" }, { status: 403 }));
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    await user.type(screen.getByTestId("override-note"), "x");
    await user.type(screen.getByTestId("override-confirm"), "OVERRIDE");
    await user.click(screen.getByTestId("override-submit"));
    await waitFor(() => expect(screen.getByText("denied")).toBeInTheDocument());
  });

  it("surfaces a network error inline", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockRejectedValue(new Error("offline"));
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    await user.type(screen.getByTestId("override-note"), "x");
    await user.type(screen.getByTestId("override-confirm"), "OVERRIDE");
    await user.click(screen.getByTestId("override-submit"));
    await waitFor(() => expect(screen.getByText("offline")).toBeInTheDocument());
  });

  it("falls back to a status message when the override error body is empty", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({}, { status: 502 }));
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    await user.type(screen.getByTestId("override-note"), "x");
    await user.type(screen.getByTestId("override-confirm"), "OVERRIDE");
    await user.click(screen.getByTestId("override-submit"));
    await waitFor(() => expect(screen.getByText(/502/)).toBeInTheDocument());
  });

  it("cancels the override form", async () => {
    const user = userEvent.setup();
    render(<ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={blockedStates} />);
    await user.click(screen.getByTestId("override-open-a"));
    expect(screen.getByTestId("override-form")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByTestId("override-form")).not.toBeInTheDocument();
  });

  it("holds the gate (disables Continue) while a unit is pending/needs_review", () => {
    render(
      <ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={needsReviewStates} />,
    );
    // No hard block, so no blocked banner, but Continue stays disabled because
    // a pending unit is not cleared (matches the server rollup).
    expect(screen.queryByTestId("hard-block-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId("compliance-continue")).toBeDisabled();
  });

  it("surfaces a 'Review & accept' action for pending units (not an override)", () => {
    render(
      <ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={needsReviewStates} />,
    );
    expect(screen.getByTestId("needs-review-list")).toBeInTheDocument();
    expect(screen.getByTestId("accept-open-a")).toBeInTheDocument();
    // It is NOT a hard-block override (no override button for the pending unit).
    expect(screen.queryByTestId("override-open-a")).not.toBeInTheDocument();
  });

  it("accept needs only a justification (no type-to-confirm) and POSTs the override route", async () => {
    const fetchSpy = spyOnFetch();
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));
    const user = userEvent.setup();
    render(
      <ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={needsReviewStates} />,
    );
    await user.click(screen.getByTestId("accept-open-a"));
    // No type-to-confirm field on the accept path; the note is prefilled + editable.
    expect(screen.queryByTestId("override-confirm")).not.toBeInTheDocument();
    expect(screen.getByTestId("override-submit")).not.toBeDisabled();
    await user.click(screen.getByTestId("override-submit"));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/pipelines/p1/compliance/override",
        expect.objectContaining({ method: "POST" }),
      );
      expect(routerRefresh).toHaveBeenCalled();
    });
  });

  it("accept is blocked when the justification is cleared", async () => {
    const user = userEvent.setup();
    render(
      <ComplianceOverrideGate pipelineId="p1" creatives={creatives} states={needsReviewStates} />,
    );
    await user.click(screen.getByTestId("accept-open-a"));
    await user.clear(screen.getByTestId("override-note"));
    expect(screen.getByTestId("override-submit")).toBeDisabled();
  });

  it("fires onContinue when the gate is clear", async () => {
    const onContinue = vi.fn();
    const user = userEvent.setup();
    render(
      <ComplianceOverrideGate
        pipelineId="p1"
        creatives={creatives}
        states={clearedStates}
        onContinue={onContinue}
      />,
    );
    await user.click(screen.getByTestId("compliance-continue"));
    expect(onContinue).toHaveBeenCalled();
  });
});
