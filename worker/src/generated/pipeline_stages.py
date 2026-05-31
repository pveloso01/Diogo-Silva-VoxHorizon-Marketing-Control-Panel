"""Generated pipeline-stage mirror -- DO NOT EDIT BY HAND.

Source of truth: the stage registry in ``lib/pipeline/stages.ts``
(``PIPELINE_STAGE_REGISTRY``). Regenerate this module with::

    uv run python scripts/gen_pipeline_stages.py

and verify it in CI with ``--check``. See ``docs/codegen.md``.
"""

from __future__ import annotations

from typing import Literal

# pipeline_status_enum / PIPELINE_STAGE_REGISTRY order (DAG + terminal cancelled)
PipelineStage = Literal["configuration", "ideation", "review", "generation", "creative_qa", "copy", "compliance_review", "spec_validation", "variant_plan", "finalize_assets", "launch_handoff", "monitor", "done", "cancelled"]
PIPELINE_STAGES: tuple[PipelineStage, ...] = ("configuration", "ideation", "review", "generation", "creative_qa", "copy", "compliance_review", "spec_validation", "variant_plan", "finalize_assets", "launch_handoff", "monitor", "done", "cancelled")

# Per-creative gate stages (registry `perCreative` flag).
PER_CREATIVE_STAGES: tuple[PipelineStage, ...] = ("creative_qa", "copy", "compliance_review", "spec_validation")

# Hard-gate stages (registry `hardGate` flag): compliance + launch.
HARD_GATE_STAGES: tuple[PipelineStage, ...] = ("compliance_review", "launch_handoff")

# Each stage's advance mechanism (registry `mechanism`).
STAGE_MECHANISM: dict[PipelineStage, str] = {
    "configuration": "gate",
    "ideation": "gate",
    "review": "decision",
    "generation": "auto",
    "creative_qa": "gate",
    "copy": "gate",
    "compliance_review": "gate",
    "spec_validation": "gate",
    "variant_plan": "gate",
    "finalize_assets": "auto",
    "launch_handoff": "decision",
    "monitor": "decision",
    "done": "terminal",
    "cancelled": "terminal",
}

# Each stage's successor in the DAG (registry `next`; None at terminals).
NEXT_STAGE: dict[PipelineStage, str | None] = {
    "configuration": "ideation",
    "ideation": "review",
    "review": "generation",
    "generation": "creative_qa",
    "creative_qa": "copy",
    "copy": "compliance_review",
    "compliance_review": "spec_validation",
    "spec_validation": "variant_plan",
    "variant_plan": "finalize_assets",
    "finalize_assets": "launch_handoff",
    "launch_handoff": "monitor",
    "monitor": "done",
    "done": None,
    "cancelled": None,
}

__all__ = [
    "HARD_GATE_STAGES",
    "NEXT_STAGE",
    "PER_CREATIVE_STAGES",
    "PIPELINE_STAGES",
    "PipelineStage",
    "STAGE_MECHANISM",
]
