"""Stdio MCP server exposing the pipeline-operator worker tools.

This is the *transport* half of the ``pipeline-operator`` skill. ``SKILL.md``
is the playbook, ``helper.py`` is the validated HTTP client, and this module
publishes the helper's capabilities as **real, named MCP tools** so the
operator agent invokes them as first-class tools (and the voxhorizon-approvals
plugin can gate the approval-gated tools by name).

Single source of truth
-----------------------
This server does **not** reimplement any HTTP or validation. Each tool is a
thin wrapper that delegates straight to the matching ``helper.py`` function;
``helper.py`` stays the only place that talks to the worker, reads env
(``WORKER_BASE_URL`` / ``WORKER_SHARED_SECRET``), and validates payloads.

The gating contract (tool names)
--------------------------------
The tools are published under the helper's exact entrypoint names so the
approval policy (``ekko-plugins/voxhorizon_approvals/policy.operator.yaml``)
maps one-for-one:

* ``pipeline_operator_read``        — READ tool (allowlisted; no spend)
* ``pipeline_operator_client_read`` — CLIENT-CONTEXT tool (allowlisted; no spend)
* ``pipeline_operator_brief``       — BRIEF tool (free Supabase write)
* ``pipeline_operator_render``      — RENDER tool (free codex render, $0; allowlisted)
* (the only approval-gated action is the Meta launch ``pipeline_operator_launch``,
  the integrations agent's tool, intentionally NOT published here)

Run it over stdio (the default Hermes MCP transport)::

    python mcp_server.py
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# Import the helper as a sibling module. When Hermes launches this file
# directly (``python mcp_server.py``) the skill directory is not necessarily on
# ``sys.path``; add it so ``import helper`` resolves to the co-located client.
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import helper  # noqa: E402  (path tweak must run before this import)

#: The MCP server name. Hermes namespaces tools by this when it advertises
#: them, presenting ``mcp_<server>_<tool>`` with single underscores — the
#: hyphen is normalized to ``_`` — e.g.
#: ``mcp_pipeline_operator_pipeline_operator_render``. The approval overlay
#: keys on that exact full name (see policy.operator.yaml).
SERVER_NAME = "pipeline-operator"

mcp = FastMCP(SERVER_NAME)


@mcp.tool()
def pipeline_operator_read(pipeline_id: str) -> dict[str, Any]:
    """Read the full pipeline state for a pipeline_id.

    Use this FIRST on every dispatch, before any other action. Returns the
    worker's state object (``status``/stage, ``format_choice``,
    ``config_draft``, ``picks``, ``brief``, ``concepts``, ``finals``,
    ``events_tail``) so you can branch on the current stage and stay
    idempotent. No spend, no side effects — the operator policy allowlists it.
    """
    return helper.pipeline_operator_read(pipeline_id)


@mcp.tool()
def pipeline_operator_client_read(client_id: str) -> dict[str, Any]:
    """Read the client's brand / company / campaign context for a client_id.

    Use this AFTER ``pipeline_operator_read`` whenever the pipeline is linked to
    a client (the pipeline read carries a compact ``client`` block; this returns
    the FULL context). Returns ``slug``, ``name``, ``service_type``,
    ``brand_colors``, ``profile`` (the typed client_profiles row or null),
    ``offers``, ``offer_constraints`` (the do-not-say rules you MUST honor),
    ``services``, ``value_props`` (``usps`` / ``differentiators``), ``assets``,
    and ``past_projects``. Author on-brand, compliant ads from this: the
    client's REAL offers, brand voice/tone, proof points, and local market. No
    spend, no side effects — the operator policy allowlists it.
    """
    return helper.pipeline_operator_client_read(client_id)


@mcp.tool()
def pipeline_operator_brief(
    pipeline_id: str,
    image_payload: dict[str, Any],
    notes: Optional[str] = None,
    concepts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Author or upsert the image brief for the pipeline.

    Use this in the ``configuration`` stage to record the brief the manager
    will review. ``image_payload`` must carry ``market`` plus EITHER a non-empty
    ``concepts`` list (concepts-first) OR both ``offer_text`` and ``angles``
    (offer-first); build it with the ``image-ad-authoring`` skill.

    PASS ``concepts``, the full set of N concept specs (each
    ``{concept | concept_name, prompt, offer_text?}`` from ``build_concept``), so the brief
    PERSISTS the whole concept plan. That lets the ideation render run as a
    single deterministic, worker-driven pass over the stored plan: you then call
    ``pipeline_operator_render(pipeline_id, "concept_preview")`` with NO items
    and the worker renders ALL persisted concepts at once, with no LLM in the
    per-image loop. Author all N concepts up front and pass them here.

    This is a free Supabase write — no paid API — so it is not spend-gated; the
    manager reviews the brief via the dashboard stage gate. Returns
    ``{ok, brief_id}``.
    """
    return helper.pipeline_operator_brief(
        pipeline_id=pipeline_id,
        image_payload=image_payload,
        notes=notes,
        concepts=concepts,
    )


@mcp.tool()
def pipeline_operator_render(
    pipeline_id: str,
    kind: str,
    items: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Render a stage's images in ONE deterministic pass (free codex render).

    PREFERRED: OMIT ``items``. The worker then renders the PERSISTED plan —
    every concept spec you stored via ``pipeline_operator_brief(concepts=...)``
    for ``kind="concept_preview"``, or one final per pick for ``kind="final"`` —
    ALL in one pass. You just trigger the stage; you do NOT author or loop items
    at render time, so a slow render can never collapse to "only one concept
    landed". A retried render resumes the remainder (already-rendered concepts
    are skipped). This is the path the SKILL prescribes.

    Legacy: pass ``items`` (``{concept, prompt, offer_text?,
    parent_creative_id?}``) to render exactly those — kept for back-compat.

    Use ``kind="concept_preview"`` in the ``ideation`` stage and ``kind="final"``
    in the ``generation`` stage (finals need ``parent_creative_id``; the
    deterministic path threads it from the picks; 9:16 finals come back as a true
    864x1536).

    Routing is by ``kind``, not an env var: ideation is hardwired to the free
    codex model (``gpt-image-2``, $0); finals use the per-pipeline finals-model
    choice (default the same free codex model; ``kie`` is the legacy paid path).
    Rendering is **free and allowlisted** (no per-call approval) and the manager
    supervises spend at the dashboard STAGE gates; the only approval-gated tool
    is ``pipeline_operator_launch``. Returns ``{ok, renders, total_cost_usd,
    errors, skipped}`` (``total_cost_usd`` is 0 on the codex backend).
    """
    return helper.pipeline_operator_render(
        pipeline_id=pipeline_id,
        kind=kind,
        items=items,
    )


# ---------------------------------------------------------------------------
# STAGE-PERSIST tools (P3) — the post-generation persistence surface.
#
# Each delegates straight to the matching helper.py wrapper (the only thing that
# talks to the worker). Names match the helper one-for-one so the approval
# policy maps exactly: all are allowlisted (no spend); none clears a gate. The
# Meta-launch tool is NOT here — it requires approval and is the integrations
# agent's tool.
# ---------------------------------------------------------------------------


@mcp.tool()
def pipeline_operator_qa_result(
    pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist per-creative QA verdicts (ONE array call) for the worker to write.

    Use in ``creative_qa``. Pass the qa specialist's verdicts as ``results=
    [{creative_id, verdict, scores, defects, remediation}, ...]`` — the whole
    batch in one call, looping every OUTSTANDING final, never per creative. The
    worker runs its deterministic backstops, writes ``qa_result`` and rolls
    ``creative_stage_state(creative_qa)``. You persist the verdict; the manager
    signs off QA at the gate. No spend; allowlisted.
    """
    return helper.pipeline_operator_qa_result(
        pipeline_id=pipeline_id, results=results
    )


@mcp.tool()
def pipeline_operator_compliance_result(
    pipeline_id: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Submit per-creative compliance CANDIDATE findings — the WORKER adjudicates.

    Use in ``compliance_review`` (HARD GATE). Pass the compliance specialist's
    candidates as ``candidates=[{creative_id, findings:[{rule_id, version,
    label, confidence, evidence_span, required_edit, citation_url}]}, ...]`` —
    the whole batch in one call. You have NO pass-writing tool: these are
    CANDIDATES only; the worker adjudicates and writes the verdict, escalating
    uncertain/low-confidence to the manager queue (never auto-passing). A failed
    unit leaves failed only via an audited manager override. No spend;
    allowlisted.
    """
    return helper.pipeline_operator_compliance_result(
        pipeline_id=pipeline_id, candidates=candidates
    )


@mcp.tool()
def pipeline_operator_copy(
    pipeline_id: str, variants: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist authored copy variants (>=1 per creative, ONE array call).

    Use in ``copy``. Pass the copy specialist's drafts as ``variants=
    [{creative_id, platform, variant_index, pattern, headline, primary_text,
    description, cta, validation}, ...]`` — the whole batch in one call. The
    worker upserts ``copy_variants`` (idempotent on
    ``(creative_id, platform, variant_index)``) at ``draft`` and arms the copy
    gate; the manager approves at the copy stage gate. Approving copy re-arms
    that creative's compliance unit (two-pass). No spend; allowlisted.
    """
    return helper.pipeline_operator_copy(
        pipeline_id=pipeline_id, variants=variants
    )


@mcp.tool()
def pipeline_operator_spec_result(
    pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist per-placement spec checks + derived crops (ONE array call).

    Use in ``spec_validation`` (an auto stage). Pass ``results=[{creative_id,
    platform, placement, ratio, status, checks, derived_path_supabase?,
    derived_path_drive?}, ...]``: the whole batch in one call. The worker
    upserts ``spec_check`` (idempotent on ``(creative_id, platform,
    placement)``) and rolls the spec_validation gate to the worst placement
    status (a failing placement holds the gate for the manager, never
    auto-passed). No spend; allowlisted.

    ``placement`` and ``ratio`` are CLOSED enums. Submit only these values (an
    out-of-vocabulary value like "feed_square" is rejected with HTTP 422):
      placement: feed | stories | reels | marketplace | search | display | pmax
      ratio:     1x1 | 9x16 | 16x9 | 4x5 | 1.91x1
      status:    pass | warn | fail | exception | pending
    placement and aspect ratio are SEPARATE fields. A square feed unit is
    ``placement="feed", ratio="1x1"`` (NOT "feed_square"); a vertical story is
    ``placement="stories", ratio="9x16"``; a reel is ``placement="reels",
    ratio="9x16"``. Emit one result row per (creative, placement, ratio).
    """
    return helper.pipeline_operator_spec_result(
        pipeline_id=pipeline_id, results=results
    )


@mcp.tool()
def pipeline_operator_finalize_result(
    pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record naming + Drive folder + verify report per creative (ONE array call).

    Use in ``finalize_assets`` (an auto stage; Drive runs through your Drive
    MCP, the worker is the recorder). Pass ``results=[{creative_id, asset_name,
    drive_folder_id?, file_path_drive?, verified}, ...]``. The worker writes the
    ``creatives`` finalize columns and resumes idempotently (a creative already
    finalized is skipped). No spend; allowlisted.
    """
    return helper.pipeline_operator_finalize_result(
        pipeline_id=pipeline_id, results=results
    )


@mcp.tool()
def pipeline_operator_monitor_result(
    pipeline_id: str,
    results: list[dict[str, Any]],
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    """Persist monitor KPIs + kill/watch/keep verdicts (GHL is lead truth).

    Use in ``monitor``. Pass the monitor specialist's reads as ``results=
    [{campaign_id, ad_entity_id?, window_days, spend, ghl_leads, ctr, freq,
    verdict, verdict_reason}, ...]`` — the whole batch in one call. The worker
    writes ``campaign_perf_image`` rows computing ``cpl_real = spend /
    ghl_leads`` (NEVER Meta leads) and resumes idempotently on the daily key.
    Verdicts are recommendations; the manager approves kill/scale at the gate.
    No spend; allowlisted.
    """
    return helper.pipeline_operator_monitor_result(
        pipeline_id=pipeline_id, results=results, client_id=client_id
    )


@mcp.tool()
def pipeline_operator_signal(
    pipeline_id: str,
    dispatch_id: str,
    status: str,
    stage: Optional[str] = None,
    expected_status: Optional[str] = None,
    exec_id: Optional[str] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Signal dispatch completion / health to the workflow — call this LAST.

    End EVERY dispatch with this so the workflow knows the dispatch landed and
    the watchdog does not re-dispatch a healthy stage. ``status`` is one of:
    ``dispatched|running|completed|failed|timed_out|stale|waiting|partial|
    error``. On a stale/duplicate dispatch (``status != expected_status`` on
    read) signal ``stale`` and STOP. On a capped per-creative batch signal
    ``partial``. The worker records this signal on the ``operator_dispatch``
    work_item idempotently on ``(pipeline_id, dispatch_id)``. No spend;
    allowlisted.
    """
    return helper.pipeline_operator_signal(
        pipeline_id=pipeline_id,
        dispatch_id=dispatch_id,
        status=status,
        stage=stage,
        expected_status=expected_status,
        exec_id=exec_id,
        summary=summary,
        error=error,
    )


# ---------------------------------------------------------------------------
# VIDEO tools (VID-7) — author a video brief + trigger video generation.
# video_brief is a free DB write (allowlisted). video_render SPENDS (kie), so it
# is gated by an estimated-cost THRESHOLD in voxhorizon_approvals.policy
# (VIDEO_RENDER_TOOL): pass estimated_cost_usd so the gate can decide
# inline-vs-approve. (A broll_select tool for review_each curation is a follow-up;
# unattended generation runs broll selection in auto mode server-side.)
# ---------------------------------------------------------------------------


@mcp.tool()
def pipeline_operator_video_brief(
    pipeline_id: str,
    video_payload: dict[str, Any],
    notes: Optional[str] = None,
    concepts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Author or upsert the VIDEO brief for the pipeline.

    Use this in the ``configuration`` stage for a video (or ``both``) pipeline.
    ``video_payload`` must carry ``market``, ``offer_text``, ``angles``,
    ``target_duration_s``, ``voice_id`` (build it with the ``video-ad-authoring``
    skill's ``build_video_brief``). PASS ``concepts`` — the full set of N script
    concepts (each ``{concept, angle, script}`` from ``build_video_concept`` +
    ``assert_distinct_concepts``) — so the brief PERSISTS the whole plan for the
    deterministic ideation pass. Free Supabase write; the manager reviews at the
    dashboard stage gate. Returns ``{ok, brief_id}``.
    """
    return helper.pipeline_operator_video_brief(
        pipeline_id=pipeline_id,
        video_payload=video_payload,
        notes=notes,
        concepts=concepts,
    )


@mcp.tool()
def pipeline_operator_video_render(
    pipeline_id: str,
    estimated_cost_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Trigger VIDEO generation for the pipeline's picked concepts — THE SPEND TOOL.

    Use in the ``generation`` stage after the manager has approved the picks at
    Review. Fans out the video substage chain (script -> voiceover -> b-roll ->
    compose -> caption) for each picked video creative in the background. This
    SPENDS real money (kie generation), unlike the free image render.

    PASS ``estimated_cost_usd`` — your per-ad cost estimate (sum the kie clip cost
    across the script's segments). The approval gate reads it: at or under the
    per-ad threshold the render runs inline; over it (or if omitted) it long-polls
    the manager for approval first. The worker also enforces a hard per-ad budget
    cap before any submit. Returns the generation-accepted body.
    """
    return helper.pipeline_operator_video_render(
        pipeline_id=pipeline_id,
        estimated_cost_usd=estimated_cost_usd,
    )


def main() -> None:
    """Run the server over stdio (the default Hermes MCP transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
