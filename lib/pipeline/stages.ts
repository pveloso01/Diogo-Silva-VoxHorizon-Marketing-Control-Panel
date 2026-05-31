/**
 * The pipeline stage registry (E2.1) -- the SINGLE checked-in source of truth
 * for the 12-stage DAG + the terminal `cancelled` escape.
 *
 * Before this module the stage list, each stage's advance mechanism
 * (gate/auto/decision/terminal), its UI class (human_gate/agent_work/...), its
 * per-creative flag, its hard-gate flag and its successor were hand-synced
 * across FIVE places that could (and did) drift:
 *   - `lib/pipeline/types.ts`        (the `PIPELINE_STAGES` ordered list),
 *   - `lib/pipeline/transitions.ts`  (`advanceMechanism` / `nextStage`),
 *   - `lib/pipeline/phases.ts`       (`stageClass` / `PIPELINE_PHASES`),
 *   - `worker/src/services/pipeline_runner.py` (the `PipelineStage` Literal),
 *   - the DB `pipeline_status_enum` (migrations 0006/0016).
 *
 * Now every one of those is DERIVED from {@link PIPELINE_STAGE_REGISTRY}:
 *   - `types.ts` / `transitions.ts` / `phases.ts` build their maps from it,
 *   - `worker/scripts/gen_pipeline_stages.py` reads THIS file and emits
 *     `worker/src/generated/pipeline_stages.py` (mirroring the #550 enum codegen),
 *   - parity tests (`stages.parity.test.ts` + the worker `test_pipeline_stages_parity`)
 *     fail CI if the registry, the TS derivations, the generated Python Literal
 *     or the DB enum disagree.
 *
 * This is a behaviour-preserving de-duplication: the order, the mechanisms, the
 * classes, the per-creative set and the DAG edges are EXACTLY today's. Do not
 * change the DAG here -- only add/remove a stage in lockstep with a DB migration.
 *
 * Why a checked-in manifest and not a DB table: the values change at most once
 * per milestone (with a migration), the consumers are pure/compile-time, and a
 * table would need a runtime read + a migration anyway. Codegen from this file
 * keeps the Python worker and the TS app honest with zero schema churn.
 *
 * The order of the array IS the DAG order and MUST match the DB
 * `pipeline_status_enum` value order (asserted by the parity tests).
 *
 * Pure data -- no IO, no imports of the derived modules (they import this).
 */

/** How a stage hands off to its successor. See `transitions.ts#advanceMechanism`. */
export type StageMechanism = "gate" | "auto" | "decision" | "terminal";

/** What kind of work a stage is, for UI affordances. See `phases.ts#stageClass`. */
export type StageClassName =
  | "human_gate"
  | "agent_work"
  | "per_creative"
  | "hard_gate"
  | "auto"
  | "terminal";

/** The 5 display phases (plus `closed`) the manager thinks in. See `phases.ts`. */
export type StagePhase = "define" | "create" | "vet" | "pack" | "live" | "closed";

/**
 * One stage's full definition. `key` is the literal value (also the DB enum
 * value); `next` is the successor in the DAG (null at the terminal stages).
 */
export type StageDef = {
  /** The status literal -- equals the DB `pipeline_status_enum` value. */
  readonly key: string;
  /** Stepper label shown in the UI. */
  readonly label: string;
  /** How the stage advances (drives the route + whether the UI shows a button). */
  readonly mechanism: StageMechanism;
  /** UI class (badge / controls). */
  readonly stageClass: StageClassName;
  /** The display phase this stage clusters into. */
  readonly phase: StagePhase;
  /** True for the per-creative gate stages (the creative_stage_state rollup). */
  readonly perCreative: boolean;
  /** True for the non-bypassable / irreversible-spend gates (compliance, launch). */
  readonly hardGate: boolean;
  /** The successor stage key, or null at a terminal stage (`done` / `cancelled`). */
  readonly next: string | null;
};

/**
 * THE REGISTRY. The array order is the DAG order and the DB enum order; the
 * happy path runs top to bottom, `cancelled` is the terminal escape appended
 * last (it matches the DB enum, which lists `cancelled` after `done`).
 *
 * Behaviour-preserving snapshot of today's hand-synced tables:
 *  - mechanism: only `generation` + `finalize_assets` are `auto`; the four
 *    per-creative stages + `configuration`/`ideation`/`variant_plan` are `gate`;
 *    `review`/`launch_handoff`/`monitor` are `decision`; `done`/`cancelled` terminal.
 *  - stageClass: `configuration`/`review`/`variant_plan`/`monitor` = human_gate;
 *    `ideation`/`generation` = agent_work; `creative_qa`/`copy`/`spec_validation`
 *    = per_creative; `compliance_review`/`launch_handoff` = hard_gate;
 *    `finalize_assets` = auto; `done`/`cancelled` = terminal.
 */
export const PIPELINE_STAGE_REGISTRY: readonly StageDef[] = [
  {
    key: "configuration",
    label: "Configuration",
    mechanism: "gate",
    stageClass: "human_gate",
    phase: "define",
    perCreative: false,
    hardGate: false,
    next: "ideation",
  },
  {
    key: "ideation",
    label: "Ideation",
    mechanism: "gate",
    stageClass: "agent_work",
    phase: "define",
    perCreative: false,
    hardGate: false,
    next: "review",
  },
  {
    key: "review",
    label: "Review",
    mechanism: "decision",
    stageClass: "human_gate",
    phase: "define",
    perCreative: false,
    hardGate: false,
    next: "generation",
  },
  {
    key: "generation",
    label: "Generation",
    mechanism: "auto",
    stageClass: "agent_work",
    phase: "create",
    perCreative: false,
    hardGate: false,
    next: "creative_qa",
  },
  {
    key: "creative_qa",
    label: "Creative QA",
    mechanism: "gate",
    stageClass: "per_creative",
    phase: "vet",
    perCreative: true,
    hardGate: false,
    next: "copy",
  },
  {
    // Copy runs BEFORE compliance: compliance screens the COPY text (its default
    // surface), so the copy must exist first. The old order (compliance before
    // copy) screened only the image, then authoring copy voided the clearance
    // with no re-screen. Copy-first means compliance adjudicates the final copy
    // + image in one forward pass.
    key: "copy",
    label: "Copy",
    mechanism: "gate",
    stageClass: "per_creative",
    phase: "vet",
    perCreative: true,
    hardGate: false,
    next: "compliance_review",
  },
  {
    key: "compliance_review",
    label: "Compliance",
    mechanism: "gate",
    stageClass: "hard_gate",
    phase: "vet",
    perCreative: true,
    hardGate: true,
    next: "spec_validation",
  },
  {
    key: "spec_validation",
    label: "Spec Validation",
    mechanism: "gate",
    stageClass: "per_creative",
    phase: "vet",
    perCreative: true,
    hardGate: false,
    next: "variant_plan",
  },
  {
    key: "variant_plan",
    label: "Variant Plan",
    mechanism: "gate",
    stageClass: "human_gate",
    phase: "pack",
    perCreative: false,
    hardGate: false,
    next: "finalize_assets",
  },
  {
    key: "finalize_assets",
    label: "Finalize",
    mechanism: "auto",
    stageClass: "auto",
    phase: "pack",
    perCreative: false,
    hardGate: false,
    next: "launch_handoff",
  },
  {
    key: "launch_handoff",
    label: "Launch",
    mechanism: "decision",
    stageClass: "hard_gate",
    phase: "live",
    perCreative: false,
    hardGate: true,
    next: "monitor",
  },
  {
    key: "monitor",
    label: "Monitor",
    mechanism: "decision",
    stageClass: "human_gate",
    phase: "live",
    perCreative: false,
    hardGate: false,
    next: "done",
  },
  {
    key: "done",
    label: "Done",
    mechanism: "terminal",
    stageClass: "terminal",
    phase: "closed",
    perCreative: false,
    hardGate: false,
    next: null,
  },
  {
    key: "cancelled",
    label: "Cancelled",
    mechanism: "terminal",
    stageClass: "terminal",
    phase: "closed",
    perCreative: false,
    hardGate: false,
    next: null,
  },
] as const;

/**
 * Every stage key in DAG order, INCLUDING the terminal `cancelled` escape.
 * Equals the DB `pipeline_status_enum` value order (parity-tested).
 */
export const ALL_STAGE_KEYS: readonly string[] = PIPELINE_STAGE_REGISTRY.map((s) => s.key);

/** The happy-path stages (everything except the `cancelled` escape), in order. */
export const HAPPY_PATH_STAGES: readonly StageDef[] = PIPELINE_STAGE_REGISTRY.filter(
  (s) => s.key !== "cancelled",
);

/** Lookup a stage definition by key. Throws if the key is unknown. */
const REGISTRY_BY_KEY: ReadonlyMap<string, StageDef> = new Map(
  PIPELINE_STAGE_REGISTRY.map((s) => [s.key, s]),
);

export function stageDef(key: string): StageDef {
  const def = REGISTRY_BY_KEY.get(key);
  if (!def) throw new Error(`unknown pipeline stage: ${key}`);
  return def;
}
