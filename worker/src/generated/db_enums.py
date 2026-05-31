"""Generated DB enum mirror -- DO NOT EDIT BY HAND.

Source of truth: the Postgres enums, reflected into
``lib/supabase/types.gen.ts`` by ``pnpm regen:types``. Regenerate this
module with::

    uv run python scripts/gen_db_enums.py

and verify it in CI with ``--check``. See ``docs/codegen.md``.
"""

from __future__ import annotations

from typing import Literal

# pipeline_status_enum
PipelineStatuses = Literal["configuration", "ideation", "review", "generation", "creative_qa", "compliance_review", "copy", "spec_validation", "variant_plan", "finalize_assets", "launch_handoff", "monitor", "done", "cancelled"]
PIPELINE_STATUSES: tuple[PipelineStatuses, ...] = ("configuration", "ideation", "review", "generation", "creative_qa", "compliance_review", "copy", "spec_validation", "variant_plan", "finalize_assets", "launch_handoff", "monitor", "done", "cancelled")

# pipeline_format_enum
PipelineFormats = Literal["image", "video", "both"]
PIPELINE_FORMATS: tuple[PipelineFormats, ...] = ("image", "video", "both")

# creative_stage_enum
PerCreativeStages = Literal["creative_qa", "compliance_review", "copy", "spec_validation"]
PER_CREATIVE_STAGES: tuple[PerCreativeStages, ...] = ("creative_qa", "compliance_review", "copy", "spec_validation")

# stage_state_enum
StageStates = Literal["pending", "in_progress", "passed", "failed", "overridden", "skipped"]
STAGE_STATES: tuple[StageStates, ...] = ("pending", "in_progress", "passed", "failed", "overridden", "skipped")

# compliance_verdict_enum
ComplianceVerdicts = Literal["pending", "pass", "fail", "needs_review", "override_released"]
COMPLIANCE_VERDICTS: tuple[ComplianceVerdicts, ...] = ("pending", "pass", "fail", "needs_review", "override_released")

# qa_status_enum
QaStatuses = Literal["pass", "fail", "needs_review"]
QA_STATUSES: tuple[QaStatuses, ...] = ("pass", "fail", "needs_review")

# spec_status_enum
SpecStatuses = Literal["pending", "pass", "warn", "fail", "exception"]
SPEC_STATUSES: tuple[SpecStatuses, ...] = ("pending", "pass", "warn", "fail", "exception")

# placement_enum
Placements = Literal["feed", "stories", "reels", "marketplace", "search", "display", "pmax"]
PLACEMENTS: tuple[Placements, ...] = ("feed", "stories", "reels", "marketplace", "search", "display", "pmax")

# ratio
Ratios = Literal["1x1", "9x16", "16x9", "4x5", "1.91x1"]
RATIOS: tuple[Ratios, ...] = ("1x1", "9x16", "16x9", "4x5", "1.91x1")

# copy_variant_status_enum
CopyVariantStatuses = Literal["draft", "validated", "approved", "rejected", "retired"]
COPY_VARIANT_STATUSES: tuple[CopyVariantStatuses, ...] = ("draft", "validated", "approved", "rejected", "retired")

# launch_package_status_enum
LaunchPackageStatuses = Literal["assembling", "validating", "blocked", "ready", "approved", "queued", "live", "failed", "cancelled"]
LAUNCH_PACKAGE_STATUSES: tuple[LaunchPackageStatuses, ...] = ("assembling", "validating", "blocked", "ready", "approved", "queued", "live", "failed", "cancelled")

# ad_entity_kind_enum
AdEntityKinds = Literal["campaign", "adset", "ad", "creative"]
AD_ENTITY_KINDS: tuple[AdEntityKinds, ...] = ("campaign", "adset", "ad", "creative")

# ad_entity_state_enum
AdEntityStates = Literal["paused", "active", "archived", "deleted", "error"]
AD_ENTITY_STATES: tuple[AdEntityStates, ...] = ("paused", "active", "archived", "deleted", "error")

# hermes_task_status_enum
HermesTaskStatuses = Literal["pending", "ready", "claimed", "running", "completed", "failed", "blocked", "cancelled"]
HERMES_TASK_STATUSES: tuple[HermesTaskStatuses, ...] = ("pending", "ready", "claimed", "running", "completed", "failed", "blocked", "cancelled")

__all__ = [
    "AD_ENTITY_KINDS",
    "AD_ENTITY_STATES",
    "AdEntityKinds",
    "AdEntityStates",
    "COMPLIANCE_VERDICTS",
    "COPY_VARIANT_STATUSES",
    "ComplianceVerdicts",
    "CopyVariantStatuses",
    "HERMES_TASK_STATUSES",
    "HermesTaskStatuses",
    "LAUNCH_PACKAGE_STATUSES",
    "LaunchPackageStatuses",
    "PER_CREATIVE_STAGES",
    "PIPELINE_FORMATS",
    "PIPELINE_STATUSES",
    "PLACEMENTS",
    "PerCreativeStages",
    "PipelineFormats",
    "PipelineStatuses",
    "Placements",
    "QA_STATUSES",
    "QaStatuses",
    "RATIOS",
    "Ratios",
    "SPEC_STATUSES",
    "STAGE_STATES",
    "SpecStatuses",
    "StageStates",
]
