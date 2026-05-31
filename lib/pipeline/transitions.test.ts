import { describe, expect, it } from "vitest";

import { PIPELINE_STAGES, type PipelineStatus } from "./types";
import { activeTracksLocal, advanceMechanism, canAdvance, nextStage } from "./transitions";

/** Minimal pipeline shape canAdvance reads. */
function p(over: Partial<Parameters<typeof canAdvance>[0]> = {}) {
  return {
    status: "configuration" as PipelineStatus,
    format_choice: "image" as const,
    config_draft: {} as Record<string, unknown> | null,
    picks: null as { image?: string[]; video?: string[] } | null,
    ...over,
  };
}

describe("activeTracksLocal", () => {
  it.each([
    ["image", { image: true, video: false }],
    ["video", { image: false, video: true }],
    ["both", { image: true, video: true }],
  ] as const)("format=%s → %o", (fmt, expected) => {
    expect(activeTracksLocal(fmt)).toEqual(expected);
  });
});

describe("canAdvance: configuration→ideation", () => {
  it("OK when image_payload exists", () => {
    expect(canAdvance(p({ config_draft: { image_payload: { service: "roofing" } } }))).toEqual({
      ok: true,
      next: "ideation",
    });
  });

  it("OK when both payloads present for format=both", () => {
    expect(
      canAdvance(
        p({
          format_choice: "both",
          config_draft: { image_payload: { x: 1 }, video_payload: { y: 2 } },
        }),
      ),
    ).toEqual({ ok: true, next: "ideation" });
  });

  it("flags missing video_payload for format=video", () => {
    expect(canAdvance(p({ format_choice: "video", config_draft: {} }))).toMatchObject({
      ok: false,
      missing: ["video_payload"],
    });
  });

  it("flags both missing payloads for format=both", () => {
    const r = canAdvance(p({ format_choice: "both", config_draft: null }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.missing).toEqual(["image_payload", "video_payload"]);
  });

  it("rejects array values where a plain object is required", () => {
    const r = canAdvance(p({ config_draft: { image_payload: [] } }));
    expect(r.ok).toBe(false);
  });
});

describe("canAdvance: ideation→review picks gate", () => {
  it("blocks when no picks for the active track", () => {
    const r = canAdvance(p({ status: "ideation", picks: {} }));
    expect(r).toMatchObject({ ok: false, missing: ["image"] });
  });
  it("OK once a pick exists", () => {
    expect(canAdvance(p({ status: "ideation", picks: { image: ["c1"] } }))).toEqual({
      ok: true,
      next: "review",
    });
  });
  it("format=both needs both tracks picked", () => {
    const r = canAdvance(
      p({ status: "ideation", format_choice: "both", picks: { image: ["c1"] } }),
    );
    expect(r).toMatchObject({ ok: false, missing: ["video"] });
  });
});

describe("canAdvance: review + generation", () => {
  it("review → generation (manager decision route commits it)", () => {
    expect(canAdvance(p({ status: "review" }))).toEqual({ ok: true, next: "generation" });
  });
  it("generation does NOT manually advance (auto on render completion)", () => {
    const r = canAdvance(p({ status: "generation" }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/auto-advance/);
  });
});

describe("canAdvance: per-creative gates (rollup)", () => {
  it.each([
    ["creative_qa", "copy"],
    ["copy", "compliance_review"],
    ["compliance_review", "spec_validation"],
    ["spec_validation", "variant_plan"],
  ] as const)("%s blocks until rollup cleared, then → %s", (from, to) => {
    expect(canAdvance(p({ status: from }), { rollupCleared: false }).ok).toBe(false);
    expect(canAdvance(p({ status: from }), { rollupCleared: true })).toEqual({
      ok: true,
      next: to,
    });
  });

  it("compliance is a HARD gate — surfaces the hard-block reason when uncleared", () => {
    const r = canAdvance(p({ status: "compliance_review" }), { rollupCleared: false });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/HARD gate/);
  });
});

describe("canAdvance: pack + launch + monitor", () => {
  it("variant_plan → finalize_assets", () => {
    expect(canAdvance(p({ status: "variant_plan" }))).toEqual({
      ok: true,
      next: "finalize_assets",
    });
  });
  it("finalize_assets → launch_handoff (auto)", () => {
    expect(canAdvance(p({ status: "finalize_assets" }))).toEqual({
      ok: true,
      next: "launch_handoff",
    });
  });
  it("launch_handoff blocks without preconditions, opens with them", () => {
    expect(canAdvance(p({ status: "launch_handoff" })).ok).toBe(false);
    expect(canAdvance(p({ status: "launch_handoff" }), { launchPreconditionsMet: true })).toEqual({
      ok: true,
      next: "monitor",
    });
  });
  it("monitor → done", () => {
    expect(canAdvance(p({ status: "monitor" }))).toEqual({ ok: true, next: "done" });
  });
});

describe("canAdvance: terminal states", () => {
  it("done is rejected", () => {
    const r = canAdvance(p({ status: "done" }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/already done/);
  });
  it("cancelled is rejected", () => {
    const r = canAdvance(p({ status: "cancelled" }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/cancelled/);
  });
});

// ---------------------------------------------------------------------------
// No-stall guarantees: the workflow must always be able to progress (or be
// deliberately gated) — never silently stuck, and never advance past a gate.
// ---------------------------------------------------------------------------
describe("no-stall guarantees", () => {
  it("every non-terminal stage has a successor (nextStage never null mid-flow)", () => {
    for (const { key } of PIPELINE_STAGES) {
      if (key === "done") continue;
      expect(nextStage(key)).not.toBeNull();
    }
    expect(nextStage("done")).toBeNull();
    expect(nextStage("cancelled")).toBeNull();
  });

  it("a per-creative gate HOLDS until the last creative clears, then OPENS", () => {
    // partial / uncleared → held
    expect(canAdvance(p({ status: "creative_qa" }), { rollupCleared: false }).ok).toBe(false);
    // last creative clears → opens
    expect(canAdvance(p({ status: "creative_qa" }), { rollupCleared: true }).ok).toBe(true);
  });

  it("the compliance HARD gate never opens while uncleared (no override)", () => {
    expect(canAdvance(p({ status: "compliance_review" }), { rollupCleared: false }).ok).toBe(false);
  });

  it("launch never opens without preconditions met", () => {
    expect(canAdvance(p({ status: "launch_handoff" }), { launchPreconditionsMet: false }).ok).toBe(
      false,
    );
  });

  it("advanceMechanism is defined for every status (no unclassified stage)", () => {
    // The full status set comes from the stage registry (E2.1) via PIPELINE_STAGES
    // (happy path) + the terminal `cancelled` escape.
    const all: PipelineStatus[] = [...PIPELINE_STAGES.map((s) => s.key), "cancelled"];
    for (const s of all) expect(advanceMechanism(s)).toBeTruthy();
    expect(advanceMechanism("generation")).toBe("auto");
    expect(advanceMechanism("compliance_review")).toBe("gate");
    // E2.5: spec_validation is a manual per-creative gate (the advance route
    // commits it via StageCreativeReview's Continue), NOT an auto stage — nothing
    // auto-advances spec_validation→variant_plan.
    expect(advanceMechanism("spec_validation")).toBe("gate");
    expect(advanceMechanism("launch_handoff")).toBe("decision");
    expect(advanceMechanism("done")).toBe("terminal");
  });
});

describe("nextStage: full 12-stage DAG", () => {
  it.each([
    ["configuration", "ideation"],
    ["ideation", "review"],
    ["review", "generation"],
    ["generation", "creative_qa"],
    ["creative_qa", "copy"],
    ["copy", "compliance_review"],
    ["compliance_review", "spec_validation"],
    ["spec_validation", "variant_plan"],
    ["variant_plan", "finalize_assets"],
    ["finalize_assets", "launch_handoff"],
    ["launch_handoff", "monitor"],
    ["monitor", "done"],
    ["done", null],
    ["cancelled", null],
  ] as const)("%s → %s", (from, to) => {
    expect(nextStage(from)).toBe(to);
  });
});
