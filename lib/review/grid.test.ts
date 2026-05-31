import { describe, expect, it } from "vitest";

import {
  buildGridRows,
  isCleared,
  isInScope,
  launchPreconditions,
  launchReady,
  overriddenCreatives,
  rollupCleared,
  rollupForStage,
  CREATIVE_STAGE_ORDER,
  MIN_APPROVED_COPY,
  type GridCreative,
  type StageStateRow,
} from "./grid";

const creative = (id: string, status: GridCreative["status"] = "draft"): GridCreative => ({
  id,
  concept: `concept-${id}`,
  status,
});

const st = (
  creative_id: string,
  stage: StageStateRow["stage"],
  status: StageStateRow["status"],
  override_note: string | null = null,
): StageStateRow => ({ creative_id, stage, status, override_note });

describe("isCleared", () => {
  it.each(["passed", "overridden", "skipped"] as const)("treats %s as cleared", (s) => {
    expect(isCleared(s)).toBe(true);
  });
  it.each(["pending", "in_progress", "failed"] as const)("treats %s as not cleared", (s) => {
    expect(isCleared(s)).toBe(false);
  });
});

describe("isInScope", () => {
  it("includes non-killed creatives", () => {
    expect(isInScope(creative("a", "approved"))).toBe(true);
  });
  it("drops killed creatives", () => {
    expect(isInScope(creative("a", "killed"))).toBe(false);
  });
});

describe("buildGridRows — forced ordering / locks", () => {
  it("defaults missing state rows to pending", () => {
    const [row] = buildGridRows([creative("a")], []);
    expect(row!.cells.creative_qa.status).toBe("pending");
  });

  it("locks downstream cells until upstream clears", () => {
    const [row] = buildGridRows([creative("a")], [st("a", "creative_qa", "pending")]);
    expect(row!.cells.creative_qa.locked).toBe(false);
    expect(row!.cells.compliance_review.locked).toBe(true);
    expect(row!.cells.copy.locked).toBe(true);
    expect(row!.cells.spec_validation.locked).toBe(true);
  });

  it("unlocks the next cell once the upstream cell clears", () => {
    const [row] = buildGridRows(
      [creative("a")],
      [st("a", "creative_qa", "passed"), st("a", "copy", "in_progress")],
    );
    expect(row!.cells.creative_qa.locked).toBe(false);
    // copy is the next column after QA in the reordered sequence.
    expect(row!.cells.copy.locked).toBe(false);
    // compliance is still locked (copy not cleared)
    expect(row!.cells.compliance_review.locked).toBe(true);
  });

  it("an overridden upstream still unlocks downstream", () => {
    const [row] = buildGridRows(
      [creative("a")],
      [
        st("a", "creative_qa", "passed"),
        st("a", "copy", "passed"),
        st("a", "compliance_review", "overridden", "ok"),
      ],
    );
    // compliance overridden (with QA + copy cleared) unlocks spec downstream.
    expect(row!.cells.spec_validation.locked).toBe(false);
    expect(row!.cells.compliance_review.note).toBe("ok");
  });

  it("re-locks downstream when an upstream is failed", () => {
    const [row] = buildGridRows([creative("a")], [st("a", "creative_qa", "failed")]);
    expect(row!.cells.compliance_review.locked).toBe(true);
  });
});

describe("rollupForStage / rollupCleared", () => {
  const creatives = [creative("a"), creative("b"), creative("c", "killed")];
  const states = [
    st("a", "creative_qa", "passed"),
    st("b", "creative_qa", "failed"),
    st("c", "creative_qa", "pending"),
  ];

  it("excludes killed creatives from the scope", () => {
    const rows = buildGridRows(creatives, states);
    const counts = rollupForStage(rows, "creative_qa");
    expect(counts.total).toBe(2);
    expect(counts.cleared).toBe(1);
    expect(counts.blocked).toBe(1);
    expect(counts.pending).toBe(0);
  });

  it("counts pending units", () => {
    const rows = buildGridRows([creative("a"), creative("b")], [st("a", "creative_qa", "passed")]);
    const counts = rollupForStage(rows, "creative_qa");
    expect(counts.pending).toBe(1);
  });

  it("rollupCleared is false with any blocked", () => {
    const rows = buildGridRows(creatives, states);
    expect(rollupCleared(rows, "creative_qa")).toBe(false);
  });

  it("rollupCleared is true when all in-scope cleared", () => {
    const rows = buildGridRows(
      [creative("a"), creative("b")],
      [st("a", "creative_qa", "passed"), st("b", "creative_qa", "skipped")],
    );
    expect(rollupCleared(rows, "creative_qa")).toBe(true);
  });

  it("rollupCleared is false with zero in-scope creatives", () => {
    const rows = buildGridRows([], []);
    expect(rollupCleared(rows, "creative_qa")).toBe(false);
  });
});

describe("launchPreconditions / launchReady", () => {
  const creatives = [creative("a"), creative("b")];
  const allCleared: StageStateRow[] = [
    st("a", "compliance_review", "passed"),
    st("b", "compliance_review", "overridden", "ok"),
    st("a", "spec_validation", "passed"),
    st("b", "spec_validation", "passed"),
  ];
  const approvedCopy = [
    ...Array.from({ length: MIN_APPROVED_COPY }, () => ({ creative_id: "a", status: "approved" })),
    ...Array.from({ length: MIN_APPROVED_COPY }, () => ({ creative_id: "b", status: "approved" })),
  ];

  it("is ready when spec + compliance + ≥3 copy all met", () => {
    const rows = buildGridRows(creatives, allCleared);
    const pre = launchPreconditions(rows, approvedCopy);
    expect(launchReady(pre)).toBe(true);
  });

  it("blocks when compliance has a failed unit", () => {
    const rows = buildGridRows(creatives, [
      ...allCleared.filter((s) => !(s.creative_id === "a" && s.stage === "compliance_review")),
      st("a", "compliance_review", "failed"),
    ]);
    const pre = launchPreconditions(rows, approvedCopy);
    expect(pre.find((p) => p.id === "compliance_clear")!.met).toBe(false);
    expect(launchReady(pre)).toBe(false);
  });

  it("blocks when a creative has <3 approved copy", () => {
    const rows = buildGridRows(creatives, allCleared);
    const pre = launchPreconditions(rows, [
      { creative_id: "a", status: "approved" },
      { creative_id: "a", status: "approved" },
      { creative_id: "a", status: "approved" },
      { creative_id: "b", status: "approved" },
    ]);
    const copyCheck = pre.find((p) => p.id === "copy_ge_3")!;
    expect(copyCheck.met).toBe(false);
    expect(copyCheck.detail).toContain("1 creative");
  });

  it("blocks when spec has not run", () => {
    const rows = buildGridRows(creatives, [
      st("a", "compliance_review", "passed"),
      st("b", "compliance_review", "passed"),
    ]);
    const pre = launchPreconditions(rows, approvedCopy);
    expect(pre.find((p) => p.id === "spec_pass")!.met).toBe(false);
  });

  it("reports zero-scope details", () => {
    const pre = launchPreconditions([], []);
    expect(launchReady(pre)).toBe(false);
    expect(pre.find((p) => p.id === "copy_ge_3")!.detail).toContain("no creatives");
    expect(pre.find((p) => p.id === "compliance_clear")!.detail).toContain("no creatives screened");
  });

  it("launchReady is false on an empty checklist", () => {
    expect(launchReady([])).toBe(false);
  });
});

describe("overriddenCreatives", () => {
  it("returns one entry per overridden compliance unit", () => {
    const rows = buildGridRows(
      [creative("a"), creative("b")],
      [
        st("a", "compliance_review", "overridden", "legal ok"),
        st("b", "compliance_review", "passed"),
      ],
    );
    const out = overriddenCreatives(rows);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: "a", note: "legal ok" });
  });

  it("excludes killed creatives", () => {
    const rows = buildGridRows(
      [creative("a", "killed")],
      [st("a", "compliance_review", "overridden", "x")],
    );
    expect(overriddenCreatives(rows)).toHaveLength(0);
  });
});

describe("CREATIVE_STAGE_ORDER", () => {
  it("is the forced QA → copy → compliance → spec sequence", () => {
    expect(CREATIVE_STAGE_ORDER).toEqual([
      "creative_qa",
      "copy",
      "compliance_review",
      "spec_validation",
    ]);
  });
});
