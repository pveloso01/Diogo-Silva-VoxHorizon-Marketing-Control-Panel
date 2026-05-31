/**
 * CreativeReviewGrid (#357): rows=creatives, cols=stage pills, rollup chips,
 * locked cells per the forced ordering, per-creative drill-in.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CreativeReviewGrid } from "./CreativeReviewGrid";
import type { GridCreative, StageStateRow } from "@/lib/review/grid";

const creatives: GridCreative[] = [
  { id: "a", concept: "Concept A", status: "draft" },
  { id: "b", concept: "Concept B", status: "killed" },
];

const states: StageStateRow[] = [
  { creative_id: "a", stage: "creative_qa", status: "passed", override_note: null },
  { creative_id: "a", stage: "compliance_review", status: "failed", override_note: null },
];

describe("CreativeReviewGrid", () => {
  it("renders a row per creative", () => {
    render(<CreativeReviewGrid creatives={creatives} states={states} mode="creative_qa" />);
    expect(screen.getByTestId("grid-row-a")).toBeInTheDocument();
    expect(screen.getByTestId("grid-row-b")).toBeInTheDocument();
  });

  it("marks killed creatives out of scope", () => {
    render(<CreativeReviewGrid creatives={creatives} states={states} mode="creative_qa" />);
    expect(screen.getByTestId("grid-row-b")).toHaveAttribute("data-killed", "true");
  });

  it("renders mixed per-creative states in cells", () => {
    render(<CreativeReviewGrid creatives={creatives} states={states} mode="creative_qa" />);
    expect(screen.getByTestId("grid-cell-a-creative_qa")).toHaveAttribute("data-status", "passed");
    expect(screen.getByTestId("grid-cell-a-compliance_review")).toHaveAttribute(
      "data-status",
      "failed",
    );
  });

  it("locks downstream cells until upstream clears (forced ordering)", () => {
    render(<CreativeReviewGrid creatives={creatives} states={states} mode="creative_qa" />);
    // Reordered: QA → copy → compliance → spec. QA passed → copy unlocked; copy
    // not cleared (no copy state) → compliance locked downstream.
    expect(screen.getByTestId("grid-cell-a-copy")).not.toHaveAttribute("data-locked");
    expect(screen.getByTestId("grid-cell-a-compliance_review")).toHaveAttribute(
      "data-locked",
      "true",
    );
  });

  it("emphasises the active mode column", () => {
    render(<CreativeReviewGrid creatives={creatives} states={states} mode="compliance_review" />);
    expect(screen.getByTestId("grid-col-compliance_review")).toHaveAttribute("data-active", "true");
  });

  it("opens the drill-in by clicking anywhere on the row", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(
      <CreativeReviewGrid
        creatives={creatives}
        states={states}
        mode="creative_qa"
        onOpenCreative={onOpen}
      />,
    );
    // Whole row is the click target (no Review button).
    await user.click(screen.getByTestId("grid-row-a"));
    expect(onOpen).toHaveBeenCalledWith("a");
  });

  it("opens the drill-in via keyboard (Enter on the focused row)", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(
      <CreativeReviewGrid
        creatives={creatives}
        states={states}
        mode="creative_qa"
        onOpenCreative={onOpen}
      />,
    );
    const row = screen.getByTestId("grid-row-a");
    row.focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledWith("a");
  });

  it("renders an empty state with no creatives", () => {
    render(<CreativeReviewGrid creatives={[]} states={[]} mode="creative_qa" />);
    expect(screen.getByTestId("review-grid-empty")).toBeInTheDocument();
  });
});
