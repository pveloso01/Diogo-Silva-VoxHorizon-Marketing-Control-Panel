/**
 * ReviewDrawer (#358): tabbed per-creative drill-in. Covers tab switching,
 * per-stage verdict pills + evidence, locked-stage messaging, the preview tab,
 * the action slot, and the not-found state.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ReviewDrawer } from "./ReviewDrawer";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";

const creative: GridCreative = { id: "a", concept: "Concept A", status: "draft" };
const states: StageStateRow[] = [
  { creative_id: "a", stage: "creative_qa", status: "passed", override_note: null },
  { creative_id: "a", stage: "compliance_review", status: "failed", override_note: "policy hit" },
];

describe("ReviewDrawer", () => {
  it("renders the QA tab panel by default", () => {
    render(<ReviewDrawer creative={creative} states={states} open onOpenChange={() => {}} />);
    expect(screen.getByTestId("review-drawer")).toBeInTheDocument();
    expect(screen.getByTestId("drawer-panel-creative_qa")).toBeInTheDocument();
  });

  it("switches tabs on click", async () => {
    const user = userEvent.setup();
    render(<ReviewDrawer creative={creative} states={states} open onOpenChange={() => {}} />);
    await user.click(screen.getByTestId("drawer-tab-compliance_review"));
    expect(screen.getByTestId("drawer-panel-compliance_review")).toBeInTheDocument();
    // The failure note is surfaced.
    expect(screen.getByText("policy hit")).toBeInTheDocument();
  });

  it("shows the locked message for a locked stage", async () => {
    const user = userEvent.setup();
    render(<ReviewDrawer creative={creative} states={states} open onOpenChange={() => {}} />);
    // Reordered (QA → copy → compliance → spec): copy now unlocks after QA.
    // Compliance is failed (not cleared), so the downstream spec stage is locked.
    await user.click(screen.getByTestId("drawer-tab-spec_validation"));
    expect(screen.getByTestId("drawer-locked-spec_validation")).toBeInTheDocument();
  });

  it("renders the preview tab with a signed URL", async () => {
    const user = userEvent.setup();
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        signedUrl="https://signed.test/a.png"
        open
        onOpenChange={() => {}}
      />,
    );
    await user.click(screen.getByTestId("drawer-tab-preview"));
    expect(screen.getByRole("img")).toHaveAttribute("src", "https://signed.test/a.png");
  });

  it("renders evidence summary from the evidence prop", () => {
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        evidence={{ creative_qa: { summary: { defects: ["hands"] } } }}
        open
        onOpenChange={() => {}}
      />,
    );
    expect(screen.getByText(/defects/)).toBeInTheDocument();
  });

  it("renders the per-stage action slot", () => {
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        open
        onOpenChange={() => {}}
        renderStageActions={(stage) => <button>act-{stage}</button>}
      />,
    );
    expect(screen.getByTestId("drawer-actions-creative_qa")).toBeInTheDocument();
  });

  it("renders a string evidence summary verbatim", () => {
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        evidence={{ creative_qa: { summary: "all good" } }}
        open
        onOpenChange={() => {}}
      />,
    );
    expect(screen.getByText("all good")).toBeInTheDocument();
  });

  it("shows the no-evidence message for an empty-object summary", () => {
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        evidence={{ creative_qa: { summary: {} } }}
        open
        onOpenChange={() => {}}
      />,
    );
    expect(screen.getByText(/No evidence recorded/)).toBeInTheDocument();
  });

  it("tolerates a non-serializable (circular) summary", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    render(
      <ReviewDrawer
        creative={creative}
        states={states}
        evidence={{ creative_qa: { summary: circular } }}
        open
        onOpenChange={() => {}}
      />,
    );
    // String(circular) renders without throwing.
    expect(screen.getByTestId("drawer-panel-creative_qa")).toBeInTheDocument();
  });

  it("renders a not-found state when creative is null", () => {
    render(<ReviewDrawer creative={null} states={[]} open onOpenChange={() => {}} />);
    expect(screen.getByText("Creative not found")).toBeInTheDocument();
  });
});
