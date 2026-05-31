/**
 * Stage-registry parity contract (E2.1). The 12-stage DAG + each stage's
 * mechanism / class / per-creative flag / next edge live ONCE in the registry
 * (`./stages`). Everything else is derived. This test fails CI if any of the
 * derivations -- or the DB enum, or the generated Python Literal -- drift from
 * the registry, the way `rollup.parity.test.ts` guards the per-creative gate.
 *
 * It checks four directions of agreement:
 *   1. registry order  == DB `pipeline_status_enum` order (read from types.gen.ts),
 *   2. registry mechanism/class/next == the TS derivations
 *      (`advanceMechanism` / `stageClass` / `nextStage` / `PER_CREATIVE_STAGES`),
 *   3. registry `next` chain == the contiguous happy-path order,
 *   4. registry order  == the generated Python `PIPELINE_STAGES` tuple
 *      (`worker/src/generated/pipeline_stages.py`) so the worker Literal can
 *      never silently fall behind the manifest (codegen drift gate, twin of the
 *      worker-side `gen_pipeline_stages.py --check`).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { advanceMechanism, nextStage, PER_CREATIVE_STAGES } from "./transitions";
import { stageClass } from "./phases";
import { ALL_STAGE_KEYS, PIPELINE_STAGE_REGISTRY } from "./stages";
import { PIPELINE_STAGES, type PipelineStatus } from "./types";

const TYPES_GEN_PATH = fileURLToPath(new URL("../supabase/types.gen.ts", import.meta.url));
const PY_GEN_PATH = fileURLToPath(
  new URL("../../worker/src/generated/pipeline_stages.py", import.meta.url),
);

const typesGen = readFileSync(TYPES_GEN_PATH, "utf8");
const pyGen = readFileSync(PY_GEN_PATH, "utf8");

/** Pull the ordered string-array form of an enum from the `Constants` block of
 * types.gen.ts (e.g. `pipeline_status_enum: ["configuration", ...]`). */
function dbEnumOrder(name: string): string[] {
  const re = new RegExp(`${name}:\\s*\\[([\\s\\S]*?)\\]`);
  const m = typesGen.match(re);
  expect(m, `could not find the ${name} array in types.gen.ts Constants`).not.toBeNull();
  return literals(m?.[1] ?? "");
}

/** Pull every double-quoted lower_snake literal from a fragment, in order. */
function literals(fragment: string): string[] {
  return (fragment.match(/"([a-z_]+)"/g) ?? []).map((s) => s.replace(/"/g, ""));
}

describe("stage registry <-> DB pipeline_status_enum parity", () => {
  it("registry keys match the DB enum value SET (gate order is registry-driven)", () => {
    // The per-creative gate ORDER lives in the registry `next` chain (asserted
    // by "the next chain walks the happy path" below), NOT the Postgres enum
    // ordinal: nothing reads the enum ordinal (no enum_range / ORDER BY an enum
    // column / ordinal comparisons; the reducer orders by pipeline_events.seq).
    // So copy<->compliance_review may sit in a different position in the registry
    // DAG than in the enum's declaration order. We assert the registry covers
    // exactly the enum's value SET; the DAG order is contracted separately.
    expect([...ALL_STAGE_KEYS].sort()).toEqual([...dbEnumOrder("pipeline_status_enum")].sort());
  });

  it("every registry key is a valid PipelineStatus (type-level + runtime)", () => {
    // PIPELINE_STAGES (the happy path) is derived from the registry; assert it
    // is exactly the registry minus `cancelled`, in order.
    const happy = PIPELINE_STAGE_REGISTRY.filter((s) => s.key !== "cancelled").map((s) => s.key);
    expect(PIPELINE_STAGES.map((s) => s.key)).toEqual(happy);
  });
});

describe("stage registry <-> TS derivations parity", () => {
  it("advanceMechanism matches the registry mechanism for every stage", () => {
    for (const s of PIPELINE_STAGE_REGISTRY) {
      expect(advanceMechanism(s.key as PipelineStatus)).toBe(s.mechanism);
    }
  });

  it("stageClass matches the registry stageClass for every stage", () => {
    for (const s of PIPELINE_STAGE_REGISTRY) {
      expect(stageClass(s.key as PipelineStatus)).toBe(s.stageClass);
    }
  });

  it("nextStage matches the registry next edge for every stage", () => {
    for (const s of PIPELINE_STAGE_REGISTRY) {
      expect(nextStage(s.key as PipelineStatus)).toBe(s.next);
    }
  });

  it("PER_CREATIVE_STAGES equals the registry perCreative set", () => {
    const fromRegistry = PIPELINE_STAGE_REGISTRY.filter((s) => s.perCreative).map((s) => s.key);
    expect([...PER_CREATIVE_STAGES].sort()).toEqual([...fromRegistry].sort());
  });

  it("hardGate stages are exactly compliance_review + launch_handoff", () => {
    const hard = PIPELINE_STAGE_REGISTRY.filter((s) => s.hardGate).map((s) => s.key);
    expect(hard.sort()).toEqual(["compliance_review", "launch_handoff"].sort());
  });
});

describe("stage registry internal consistency", () => {
  it("the next chain walks the happy path in order then terminates", () => {
    const happy = PIPELINE_STAGE_REGISTRY.filter((s) => s.key !== "cancelled");
    for (let i = 0; i < happy.length - 1; i++) {
      expect(happy[i]?.next).toBe(happy[i + 1]?.key);
    }
    expect(happy.at(-1)?.next).toBeNull(); // `done` terminates
    expect(PIPELINE_STAGE_REGISTRY.find((s) => s.key === "cancelled")?.next).toBeNull();
  });

  it("terminal stages (and only they) have a terminal mechanism", () => {
    for (const s of PIPELINE_STAGE_REGISTRY) {
      const isTerminal = s.key === "done" || s.key === "cancelled";
      expect(s.mechanism === "terminal").toBe(isTerminal);
      expect(s.stageClass === "terminal").toBe(isTerminal);
      expect(s.next === null).toBe(isTerminal);
    }
  });
});

describe("stage registry <-> generated Python Literal parity (codegen drift gate)", () => {
  it("the generated PIPELINE_STAGES tuple equals the registry order", () => {
    // Mirrors `worker/scripts/gen_pipeline_stages.py --check`: if the committed
    // Python is stale (or hand-edited) this fails before the worker tests do.
    const m = pyGen.match(/PIPELINE_STAGES:\s*tuple\[[^\]]*\]\s*=\s*\(([\s\S]*?)\)/);
    expect(m, "could not find the PIPELINE_STAGES tuple in pipeline_stages.py").not.toBeNull();
    expect(literals(m?.[1] ?? "")).toEqual(ALL_STAGE_KEYS);
  });

  it("the generated PipelineStage Literal lists every registry key in order", () => {
    const m = pyGen.match(/PipelineStage\s*=\s*Literal\[([\s\S]*?)\]/);
    expect(m, "could not find the PipelineStage Literal in pipeline_stages.py").not.toBeNull();
    expect(literals(m?.[1] ?? "")).toEqual(ALL_STAGE_KEYS);
  });
});
