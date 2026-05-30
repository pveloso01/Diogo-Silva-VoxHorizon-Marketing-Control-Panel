import "server-only";

/**
 * Operator instructions + the operator-driven-pipeline predicate.
 *
 * Silent-failure PR-4 cutover: the legacy fire-and-forget `dispatchOperator`
 * helper + `DispatchEnvelope` type were removed. Routes now enqueue
 * operator_dispatch work_items via `lib/work-queue/enqueue`; the
 * operator-daemon claims the queued row and docker-execs into the operator
 * container. The two surfaces still exported here are the natural-language
 * instruction strings the daemon dispatches with and the
 * `isOperatorDriven` predicate the routes branch on.
 */

export type OperatorStage =
  | "configuration"
  | "ideation"
  | "review"
  | "generation"
  | "creative_qa"
  | "compliance_review"
  | "copy"
  | "spec_validation"
  | "variant_plan"
  | "finalize_assets"
  | "launch_handoff"
  | "monitor"
  // The POST-APPROVAL monitor action: the manager has approved a kill/scale
  // verdict and the operator now EXECUTES it on Meta (kill -> pause the live
  // campaign; scale -> raise the winning campaign's daily budget). This is the
  // execute counterpart to the recommend-only `monitor` stage. It is not a
  // pipeline_status_enum value -- the verdict run already reached `done`.
  | "monitor_action";

/**
 * Build the natural-language instruction the operator receives for a given
 * stage. The operator's playbook (Wave B) keys off the pipeline_id (its
 * session id) to read live state, so the instruction only needs to name the
 * pipeline and the intent. An optional free-text brief rides along on
 * kickoff.
 */
export function operatorInstruction(
  stage: OperatorStage,
  pipelineId: string,
  brief?: string,
): string {
  switch (stage) {
    case "configuration": {
      const ask = brief?.trim()
        ? `The manager's brief: ${brief.trim()}`
        : "Use the client + format on the pipeline to draft the image brief.";
      return `You are the operator for pipeline ${pipelineId}. ${ask} Read the pipeline state, author the image brief, and stop for the manager's review.`;
    }
    case "ideation":
      return `The manager approved the brief for pipeline ${pipelineId}. Render ALL concept previews NOW in one batch: call the pipeline_operator_render tool with kind "concept_preview" and no items. Rendering is FREE ($0, gpt-image-2) and ungated -- do NOT wait for any spend approval, and do NOT just describe the concepts in text. After the renders land, call pipeline_operator_signal and STOP for the manager's picks. Do NOT advance the stage yourself.`;
    case "generation":
      return `The manager picked the concepts for pipeline ${pipelineId}. Render the final assets for the picked concepts, then stop — the pipeline finishes itself.`;
    case "review":
      // Review is a pure manager gate (approve/reject the picks); the operator
      // has nothing to do until it transitions to generation. Exposed for
      // completeness so callers don't special-case the enum.
      return `Pipeline ${pipelineId} is awaiting the manager's review. Stand by.`;
    case "creative_qa":
      return `Run the QA pass on each final for pipeline ${pipelineId}: pass/fail with defects, flag re-renders, then stop for the manager's QA sign-off.`;
    case "compliance_review":
      return `Screen each final and its copy against the Meta/FTC ruleset for pipeline ${pipelineId}. Record pass/block with required edits. You cannot pass a blocked item — stop for the manager.`;
    case "copy":
      return `Author the copy for pipeline ${pipelineId}: three variants per final, owner voice, sourced from the winning-copy registry and humanized. Stop for the manager's copy approval.`;
    case "spec_validation":
      return `Validate placement specs and derive the crops for pipeline ${pipelineId}; surface any exceptions, then stop.`;
    case "variant_plan":
      return `Build the A/B test matrix for pipeline ${pipelineId}, one variable per cell, sized to the budget. Stop for the manager's test-plan approval.`;
    case "finalize_assets":
      return `Finalize assets for pipeline ${pipelineId}: apply the naming convention, register, upload to Drive, and verify. Stop with the verify report.`;
    case "launch_handoff":
      return `Assemble and validate the launch package for pipeline ${pipelineId}. Do not touch Meta without an explicit approval naming the live action; create PAUSED first. Stop at the launch gate.`;
    case "monitor":
      return `Monitor the live ads for pipeline ${pipelineId}. Pull GHL leads as the source of truth, apply the kill/watch/keep thresholds, and recommend kill or scale. Stop for the manager's call.`;
    case "monitor_action": {
      // POST-APPROVAL execute path. The manager already approved the verdict;
      // the operator now EXECUTES it on Meta via the Meta MCP, then records the
      // outcome through the worker recorder. The verdict details (decision,
      // campaign, target budget) ride on the `brief` free-text the route
      // builds, since the daemon keys live state off the pipeline_id.
      const detail = brief?.trim() ? ` ${brief.trim()}` : "";
      return `The manager APPROVED a monitor verdict for pipeline ${pipelineId}.${detail} Look up the campaign's live meta_id from ad_entity (kind='campaign' for this pipeline's launch). For a kill: call the Meta MCP ads_update_entity to set status PAUSED. For a scale: call ads_update_entity to raise the campaign's daily_budget to the approved target. Then record the executed action via the worker monitor_action_result tool. This is an EXECUTE path, not a recommendation -- do the Meta write, then record it.`;
    }
  }
}

/**
 * Whether a pipeline is operator-driven (created via the kickoff route) rather
 * than a regular dashboard pipeline.
 *
 * This is the switch that keeps the two execution models from colliding:
 *   - operator-driven → the Hermes operator authors + renders (one
 *     spend-gated render path); the gate routes MUST NOT also fire the
 *     deterministic `/work/pipeline/{ideation,generation}` producers, or every
 *     concept + final would render twice and double the Kie spend.
 *   - regular → the deterministic producers run as before and the operator is
 *     never dispatched.
 *
 * The kickoff route stamps `config_draft.operator_driven = true`; we also treat
 * a stored `operator_instruction` as the marker so rows created before the
 * explicit flag existed still resolve correctly.
 */
export function isOperatorDriven(configDraft: unknown): boolean {
  if (!configDraft || typeof configDraft !== "object" || Array.isArray(configDraft)) {
    return false;
  }
  const draft = configDraft as Record<string, unknown>;
  if (draft.operator_driven === true) return true;
  return (
    typeof draft.operator_instruction === "string" && draft.operator_instruction.trim().length > 0
  );
}
