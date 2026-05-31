"""Operator STAGE-persist endpoints (P3 — the 12-stage rebuild).

The operator runs the 12-stage image-ad pipeline as a hired employee; the
manager supervises and clears the gates. These endpoints are the operator's
*persistence* hands for the post-generation stages: the operator delegates the
judgment (to a specialist sub-agent or in-context reasoning), then POSTs the
structured result HERE, where the **worker validates and writes** it. The
operator has NO tool that clears a gate or writes a compliance pass — these
routes only record per-creative evidence + roll the per-(creative, stage) gate
state forward to a terminal-good state the rollup predicate reads.

They are the post-``generation`` siblings of :mod:`worker.src.routes.pipeline_tools`
(brief/render/store_creative) and mirror its conventions exactly: bearer-authed
via :func:`verify_secret`, ``get_supabase_admin()`` for every read/write,
idempotent (resume-by-skip-done), and they return the per-stage rollup so the
operator can narrate "M of N done".

Endpoints (all bearer-authed):

  POST /work/pipeline/tools/copy
      Upsert ``copy_variants`` (>=1 per creative) + roll
      ``creative_stage_state(stage='copy')`` to ``in_progress``. Idempotent on
      ``unique(creative_id, platform, variant_index)``.

  POST /work/pipeline/tools/spec_result
      Write ``spec_check`` (per placement) + roll
      ``creative_stage_state(stage='spec_validation')`` to the worst placement
      status. Idempotent on ``unique(creative_id, platform, placement)``.

  POST /work/pipeline/tools/finalize_result
      Record the ``creatives`` finalize columns (asset_name, drive_folder_id,
      finalized_at, finalize_verified, file_path_drive). Idempotent per creative
      (skip already-verified).

  POST /work/pipeline/tools/monitor_result
      Write ``campaign_perf_image`` rows linked to the pipeline + ad entity,
      computing ``cpl_real = spend / ghl_leads`` (GHL is lead truth). Idempotent
      on the daily-unique index (skip a row already pulled today).

  POST /work/pipeline/tools/monitor_action_result
      Record the EXECUTED post-approval monitor action (the operator already
      paused the campaign on Meta for a kill, or raised its daily_budget for a
      scale, via the Meta MCP). Writes one ``monitor_action_result`` audit row
      (decision, target_budget, approved_by, meta_payload) linked to the
      pipeline + ad entity (migration 0058). This is the recorder half of the
      monitor connector -- the WORKER never touches Meta (operator-held MCP);
      it audits what the operator executed.

  POST /work/pipeline/tools/signal
      Record one operator dispatch signal on the unified ``work_item`` queue
      (kind ``operator_dispatch``): a fresh ``dispatched`` row, a ``running``
      heartbeat, or a terminal close (completed/failed/timed_out). Idempotent
      on ``(pipeline_id, dispatch_id, status)`` via the work_item idempotency
      key.

NOT here (other agents own them): ``/work/pipeline/tools/{qa_run,compliance_run}``
(the qa_compliance route module — the worker adjudicates QA/compliance there)
and ``/work/pipeline/tools/launch`` (the integrations route module — Meta
PAUSED-first launch, the HARD gate that requires approval).

Authorship note (same constraint as pipeline_tools): rows that carry an author
column default to ``'operator'`` (the rebuilt copy_variants / qa_result /
compliance_finding tables added ``author``/``checked_by`` text columns with that
default), so no enum juggling is needed here.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import verify_secret
from ..generated.db_enums import Placements, Ratios
from ..services import video_probe
from ..services.pipeline_runner import emit_pipeline_event, fetch_pipeline
from ..services.storage import BUCKET
from ..supabase_client import get_supabase_admin


log = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp the routes stamp onto write rows."""
    return datetime.now(timezone.utc).isoformat()


def _require_pipeline(pipeline_id: str) -> dict[str, Any]:
    """Fetch the pipeline row or 404, mirroring the pipeline_tools guard."""
    pipeline = fetch_pipeline(pipeline_id)
    if not pipeline:
        raise HTTPException(
            status_code=404, detail=f"pipeline not found: {pipeline_id}"
        )
    return pipeline


def _existing_stage_state(
    sb: Any, *, creative_id: str, stage: str
) -> dict[str, Any] | None:
    """Return the current creative_stage_state row for (creative, stage), or None.

    ``unique(creative_id, stage)`` makes this a single-row lookup; we use it to
    upsert idempotently (update in place when present, insert otherwise).
    """
    resp = (
        sb.table("creative_stage_state")
        .select("id, status")
        .eq("creative_id", creative_id)
        .eq("stage", stage)
        .maybe_single()
        .execute()
    )
    return resp.data if (resp is not None and isinstance(resp.data, dict)) else None


def _upsert_stage_state(
    sb: Any,
    *,
    pipeline_id: str,
    creative_id: str,
    stage: str,
    status: str,
    summary: dict[str, Any] | None = None,
) -> str:
    """Idempotently roll the per-(creative, stage) gate state forward.

    Inserts a fresh row at ``status`` or updates the existing one in place
    (``unique(creative_id, stage)``). The worker is the only writer of this gate
    row from the operator's stage-persist tools; the audited manager override is
    written elsewhere. Returns the row's resulting state for the rollup.
    """
    payload: dict[str, Any] = {"status": status}
    if summary is not None:
        payload["summary"] = summary
    existing = _existing_stage_state(sb, creative_id=creative_id, stage=stage)
    if existing is not None:
        sb.table("creative_stage_state").update(payload).eq(
            "id", existing["id"]
        ).execute()
    else:
        sb.table("creative_stage_state").insert(
            {
                "pipeline_id": pipeline_id,
                "creative_id": creative_id,
                "stage": stage,
                **payload,
            }
        ).execute()
    return status


def _is_video_creative(sb: Any, creative_id: str) -> bool:
    """True when ``creative_id`` is a ``video_creatives`` row (vs image creatives).

    Video pipelines route the gate-persist writes (copy / finalize / monitor /
    launch) to the ``video_*`` tables. A single id lookup; callers cache the
    result per creative across a batch.
    """
    resp = (
        sb.table("video_creatives")
        .select("id")
        .eq("id", creative_id)
        .maybe_single()
        .execute()
    )
    return resp is not None and isinstance(resp.data, dict) and bool(resp.data.get("id"))


# ===========================================================================
# POST /work/pipeline/tools/copy
# ===========================================================================


class CopyVariant(BaseModel):
    """One authored copy variant for a creative (rebuilt copy_variants shape)."""

    model_config = {"extra": "allow"}

    creative_id: str = Field(..., min_length=1)
    platform: Literal["meta", "google", "tiktok"] = "meta"
    placement: str | None = None
    variant_index: int = Field(1, ge=1)
    pattern: str | None = None
    headline: str | None = None
    primary_text: str | None = None
    description: str | None = None
    cta: str | None = None
    validation: dict[str, Any] = Field(default_factory=dict)


class CopyInput(BaseModel):
    """POST body for ``/work/pipeline/tools/copy`` — the whole batch in one call."""

    pipeline_id: str = Field(..., min_length=1)
    variants: list[CopyVariant] = Field(..., min_length=1)


def _existing_copy_variant_id(
    sb: Any,
    *,
    creative_id: str,
    platform: str,
    variant_index: int,
    table: str = "copy_variants",
) -> str | None:
    """Return the id of an existing copy row for the unique key, or None.

    ``unique(creative_id, platform, variant_index)`` makes a re-persist an
    idempotent update rather than a duplicate insert. ``table`` is
    ``copy_variants`` (image) or ``video_copy_variants`` (video; gained the
    matching columns + unique key in 0031).
    """
    resp = (
        sb.table(table)
        .select("id")
        .eq("creative_id", creative_id)
        .eq("platform", platform)
        .eq("variant_index", variant_index)
        .maybe_single()
        .execute()
    )
    if resp is not None and isinstance(resp.data, dict):
        return resp.data.get("id")
    return None


@router.post("/work/pipeline/tools/copy", dependencies=[Depends(verify_secret)])
async def persist_copy(body: CopyInput) -> dict[str, Any]:
    """Persist authored copy variants and arm the per-creative copy gate.

    Upserts each variant onto ``copy_variants`` (keyed by
    ``(creative_id, platform, variant_index)`` so a re-dispatch updates rather
    than duplicates) at ``status='draft'`` — the manager approves them at the
    copy stage gate, which is what flips them to ``approved``. Each touched
    creative's ``creative_stage_state(stage='copy')`` is rolled to
    ``in_progress`` (not ``passed`` — the operator never clears a gate). Returns
    the per-creative rollup so the operator can narrate "N variants across M
    creatives".
    """
    _require_pipeline(body.pipeline_id)
    sb = get_supabase_admin()

    # Per-creative format detection — video pipelines route copy to
    # video_copy_variants. Cached so a multi-variant creative looks up once.
    _video_cache: dict[str, bool] = {}

    def _is_video(cid: str) -> bool:
        if cid not in _video_cache:
            _video_cache[cid] = _is_video_creative(sb, cid)
        return _video_cache[cid]

    by_creative: dict[str, int] = {}
    upserted: list[dict[str, Any]] = []

    for variant in body.variants:
        cid = variant.creative_id
        by_creative[cid] = by_creative.get(cid, 0) + 1
        is_vid = _is_video(cid)
        table = "video_copy_variants" if is_vid else "copy_variants"

        # The body field is `primary_text`; both tables store Meta's primary text
        # on `body` (copy_variants since 0020, video_copy_variants since 0031).
        row: dict[str, Any] = {
            "pipeline_id": body.pipeline_id,
            "creative_id": cid,
            "platform": variant.platform,
            "variant_index": variant.variant_index,
            "headline": variant.headline,
            "body": variant.primary_text,
            "description": variant.description,
            "cta": variant.cta,
            "pattern": variant.pattern,
            "validation": variant.validation,
            "status": "draft",
        }
        if variant.placement is not None:
            row["placement"] = variant.placement
        if is_vid:
            row["humanized"] = bool(variant.validation.get("humanized"))

        existing_id = _existing_copy_variant_id(
            sb,
            creative_id=cid,
            platform=variant.platform,
            variant_index=variant.variant_index,
            table=table,
        )
        if existing_id is not None:
            sb.table(table).update(row).eq("id", existing_id).execute()
            copy_id = existing_id
        else:
            created = sb.table(table).insert(row).execute().data
            copy_id = (
                created[0]["id"] if isinstance(created, list) and created else None
            )
        upserted.append(
            {
                "creative_id": cid,
                "platform": variant.platform,
                "variant_index": variant.variant_index,
                "copy_variant_id": copy_id,
            }
        )

    rollup: list[dict[str, Any]] = []
    for creative_id, count in by_creative.items():
        state = _upsert_stage_state(
            sb,
            pipeline_id=body.pipeline_id,
            creative_id=creative_id,
            stage="copy",
            status="in_progress",
            summary={"variant_count": count},
        )
        rollup.append(
            {"creative_id": creative_id, "variant_count": count, "stage_state": state}
        )

    emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="copy_authored",
        stage="copy",
        payload={"creative_count": len(by_creative), "variant_count": len(upserted)},
    )
    log.info(
        "operator_copy_persisted",
        pipeline_id=body.pipeline_id,
        variants=len(upserted),
        creatives=len(by_creative),
    )
    return {"ok": True, "variants": upserted, "rollup": rollup}


# ===========================================================================
# POST /work/pipeline/tools/spec_result
# ===========================================================================


class SpecResult(BaseModel):
    """One per-placement spec-validation result (+ derived crop refs).

    ``placement`` and ``ratio`` are constrained to the DB ``placement_enum`` /
    ``ratio`` value sets (the generated :data:`Placements` / :data:`Ratios`
    Literals). An out-of-vocabulary value (e.g. the operator inventing
    ``"feed_square"``) is now a clean 422 at the API boundary instead of a raw
    500 when the value reaches the Postgres enum column. The operator must split
    a "feed square" into ``placement="feed"`` + ``ratio="1x1"`` (see the
    pipeline-operator SKILL spec_validation procedure).
    """

    model_config = {"extra": "allow"}

    creative_id: str = Field(..., min_length=1)
    platform: Literal["meta", "google", "tiktok"] = "meta"
    placement: Placements
    ratio: Ratios | None = None
    status: Literal["pending", "pass", "warn", "fail", "exception"]
    checks: dict[str, Any] = Field(default_factory=dict)
    derived_path_supabase: str | None = None
    derived_path_drive: str | None = None


class SpecInput(BaseModel):
    """POST body for ``/work/pipeline/tools/spec_result`` — whole batch in one call."""

    pipeline_id: str = Field(..., min_length=1)
    results: list[SpecResult] = Field(..., min_length=1)


#: Worst-first ordering so a single failing placement holds the creative's gate.
_SPEC_RANK = {"fail": 4, "exception": 3, "warn": 2, "pending": 1, "pass": 0}


def _existing_spec_check_id(
    sb: Any, *, creative_id: str, platform: str, placement: str
) -> str | None:
    """Return the id of an existing spec_check for the unique key, or None."""
    resp = (
        sb.table("spec_check")
        .select("id")
        .eq("creative_id", creative_id)
        .eq("platform", platform)
        .eq("placement", placement)
        .maybe_single()
        .execute()
    )
    if resp is not None and isinstance(resp.data, dict):
        return resp.data.get("id")
    return None


def _spec_gate_status(placement_statuses: list[str]) -> str:
    """Map the worst placement status onto the creative's spec gate state.

    ``pass`` for all-pass; ``in_progress`` while anything is still pending/warn;
    ``failed`` if any placement is ``fail``/``exception`` (a hard hold the
    manager surfaces — the operator never auto-passes a failing placement).
    """
    if not placement_statuses:
        return "in_progress"
    worst = max(placement_statuses, key=lambda s: _SPEC_RANK.get(s, 1))
    if worst in ("fail", "exception"):
        return "failed"
    if worst in ("warn", "pending"):
        return "in_progress"
    return "passed"


def _fetch_video_creative_asset(sb: Any, creative_id: str) -> dict[str, Any] | None:
    """Return the video creative's finished-asset columns, or None for an image.

    A single id lookup against ``video_creatives``; ``None`` means the id is not
    a video creative (an image creative, which keeps the operator-submitted spec
    status -- see the docstring on :func:`persist_spec_result`).
    """
    resp = (
        sb.table("video_creatives")
        .select("id, composed_path, captioned_path")
        .eq("id", creative_id)
        .maybe_single()
        .execute()
    )
    return resp.data if (resp is not None and isinstance(resp.data, dict)) else None


def _video_asset_storage_path(creative: dict[str, Any]) -> str | None:
    """The finished video asset path: captioned first, then composed, or None."""
    for key in ("captioned_path", "composed_path"):
        path = creative.get(key)
        if isinstance(path, str) and path.strip():
            return path
    return None


#: Recompute verdicts that DOWNGRADE the operator status to a hard ``fail``.
_SPEC_BACKSTOP_FAIL = {"fail"}


async def _spec_backstop_video(
    sb: Any, result: "SpecResult"
) -> tuple[str, dict[str, Any] | None]:
    """Worker recompute of one video placement's spec from the actual asset.

    Returns ``(status, backstop_detail)``. The status is the operator-submitted
    ``result.status`` UNLESS the worker recompute (ffprobe vs the placement spec)
    fails -- in which case it is downgraded to ``fail`` so the operator can never
    pass a non-conformant asset (E3.3). When the placement is unknown to the
    spec table, or the asset is not yet present, the recompute is skipped and the
    operator status stands (with the reason recorded). A probe failure escalates:
    an unverifiable asset is downgraded to ``fail`` rather than trusted.
    """
    spec = video_probe.get_placement_spec(result.placement)
    if spec is None:
        return result.status, {"backstop": "skipped", "reason": "unknown placement"}

    creative = _fetch_video_creative_asset(sb, result.creative_id)
    if creative is None:
        # Not a video creative after all -- nothing to recompute here.
        return result.status, None

    asset_path = _video_asset_storage_path(creative)
    if asset_path is None:
        return result.status, {"backstop": "skipped", "reason": "no asset yet"}

    try:
        data = sb.storage.from_(BUCKET).download(str(asset_path))
    except Exception as e:  # noqa: BLE001 — unverifiable asset must not auto-pass
        log.warning("spec_backstop_download_failed", creative_id=result.creative_id, error=str(e))
        return "fail", {"backstop": "fail", "reason": f"download failed: {e}"}

    work_dir = Path(tempfile.mkdtemp(prefix="vox-spec-probe-"))
    local = work_dir / "asset.mp4"
    local.write_bytes(bytes(data) if data else b"")
    try:
        probe = await video_probe.probe_video(local)
    except video_probe.ProbeError as e:
        # ProbeError is a RuntimeError subclass -- catch it FIRST. An asset that
        # will not probe is unverifiable, so it must not auto-pass: downgrade.
        log.warning("spec_backstop_probe_failed", creative_id=result.creative_id, error=str(e))
        return "fail", {"backstop": "fail", "reason": f"probe failed: {e}"}
    except RuntimeError as e:
        # ffprobe missing (ships in the worker image) -> surface as 503.
        raise HTTPException(status_code=503, detail=str(e)) from e

    report = video_probe.video_spec_verdict(probe, spec)
    detail = {
        "backstop": report.status,
        "ruleset_version": report.ruleset_version,
        "probe": probe.to_dict(),
        "checks": report.to_dict()["checks"],
    }
    if report.status in _SPEC_BACKSTOP_FAIL:
        # Downgrade: the asset violates the placement spec. The operator's
        # submitted status (even a 'pass') can never override a worker fail.
        return "fail", detail
    # The recompute did not fail -- keep the operator status (the backstop only
    # ever tightens, never loosens, the verdict).
    return result.status, detail


@router.post(
    "/work/pipeline/tools/spec_result", dependencies=[Depends(verify_secret)]
)
async def persist_spec_result(body: SpecInput) -> dict[str, Any]:
    """Persist per-placement spec checks + roll the spec_validation gate.

    Upserts each ``spec_check`` row (keyed by
    ``(creative_id, platform, placement)`` for idempotent resume) and then rolls
    each creative's ``creative_stage_state(stage='spec_validation')`` to the
    worst placement status (``passed`` only when every placement passes; a
    failing placement holds the gate ``failed`` for the manager to surface).

    WORKER BACKSTOP (E3.3): for a VIDEO creative the worker does NOT trust the
    operator-submitted status -- it downloads the finished asset, probes it with
    ffprobe, recomputes the spec verdict against the placement spec
    (``video_probe.video_spec_verdict``), and DOWNGRADES the persisted status to
    ``fail`` when the asset violates the spec. The operator can never pass a
    non-conformant asset. Image creatives keep the operator status: the operator
    already submits the deterministic Pillow ``checks`` for images, and the QA
    route (``qa_run``) is the worker-owned image backstop; a duplicate per-spec
    Pillow recompute here is deferred (see follow-ups).
    Returns the per-creative rollup.
    """
    _require_pipeline(body.pipeline_id)
    sb = get_supabase_admin()

    by_creative: dict[str, list[str]] = {}
    written: list[dict[str, Any]] = []
    for result in body.results:
        # Worker spec backstop: recompute video placements from the real asset
        # and downgrade a non-conformant 'pass' to 'fail'. Image keeps status.
        effective_status, backstop_detail = await _spec_backstop_video(sb, result)

        merged_checks = dict(result.checks)
        if backstop_detail is not None:
            merged_checks["worker_backstop"] = backstop_detail

        row: dict[str, Any] = {
            "pipeline_id": body.pipeline_id,
            "creative_id": result.creative_id,
            "platform": result.platform,
            "placement": result.placement,
            "status": effective_status,
            "checks": merged_checks,
        }
        if result.ratio is not None:
            row["ratio"] = result.ratio
        if result.derived_path_supabase is not None:
            row["derived_path_supabase"] = result.derived_path_supabase
        if result.derived_path_drive is not None:
            row["derived_path_drive"] = result.derived_path_drive

        existing_id = _existing_spec_check_id(
            sb,
            creative_id=result.creative_id,
            platform=result.platform,
            placement=result.placement,
        )
        if existing_id is not None:
            sb.table("spec_check").update(row).eq("id", existing_id).execute()
            spec_id = existing_id
        else:
            created = sb.table("spec_check").insert(row).execute().data
            spec_id = (
                created[0]["id"]
                if isinstance(created, list) and created
                else None
            )
        by_creative.setdefault(result.creative_id, []).append(effective_status)
        written.append(
            {
                "creative_id": result.creative_id,
                "placement": result.placement,
                "status": effective_status,
                "submitted_status": result.status,
                "spec_check_id": spec_id,
                "backstop_downgraded": effective_status != result.status,
            }
        )

    rollup: list[dict[str, Any]] = []
    for creative_id, statuses in by_creative.items():
        gate = _spec_gate_status(statuses)
        state = _upsert_stage_state(
            sb,
            pipeline_id=body.pipeline_id,
            creative_id=creative_id,
            stage="spec_validation",
            status=gate,
            summary={"placements": statuses},
        )
        rollup.append(
            {
                "creative_id": creative_id,
                "placements": len(statuses),
                "stage_state": state,
            }
        )

    emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="spec_validated",
        stage="spec_validation",
        payload={"creative_count": len(by_creative), "placement_count": len(written)},
    )
    log.info(
        "operator_spec_persisted",
        pipeline_id=body.pipeline_id,
        placements=len(written),
        creatives=len(by_creative),
    )
    return {"ok": True, "results": written, "rollup": rollup}


# ===========================================================================
# POST /work/pipeline/tools/finalize_result
# ===========================================================================


class FinalizeResult(BaseModel):
    """One creative's finalize record (naming + Drive + verify report)."""

    model_config = {"extra": "allow"}

    creative_id: str = Field(..., min_length=1)
    asset_name: str = Field(..., min_length=1)
    drive_folder_id: str | None = None
    file_path_drive: str | None = None
    verified: bool = False


class FinalizeInput(BaseModel):
    """POST body for ``/work/pipeline/tools/finalize_result``."""

    pipeline_id: str = Field(..., min_length=1)
    results: list[FinalizeResult] = Field(..., min_length=1)


def _existing_creative_finalize(
    sb: Any, creative_id: str, *, table: str = "creatives"
) -> dict[str, Any] | None:
    """Return the finalize columns of a creative row (image or video), or None.

    ``table`` is ``creatives`` (image) or ``video_creatives`` (video) — both
    carry the finalize columns (video_creatives gained them in migration 0031).
    """
    resp = (
        sb.table(table)
        .select("id, finalize_verified")
        .eq("id", creative_id)
        .maybe_single()
        .execute()
    )
    return resp.data if (resp is not None and isinstance(resp.data, dict)) else None


@router.post(
    "/work/pipeline/tools/finalize_result", dependencies=[Depends(verify_secret)]
)
async def persist_finalize_result(body: FinalizeInput) -> dict[str, Any]:
    """Record naming + Drive folder + verify report onto each creative.

    Writes the ``creatives`` finalize columns (``asset_name``,
    ``drive_folder_id``, ``file_path_drive``, ``finalized_at``,
    ``finalize_verified``). Idempotent resume: a creative already
    ``finalize_verified`` is skipped (re-running the stage after a partial Drive
    upload completes only the remainder). Returns the recorded + skipped sets.
    """
    _require_pipeline(body.pipeline_id)
    sb = get_supabase_admin()

    recorded: list[dict[str, Any]] = []
    skipped: list[str] = []
    finalized_at = _now_iso()
    for result in body.results:
        # Route video creatives to video_creatives (0031 added the finalize cols).
        table = (
            "video_creatives"
            if _is_video_creative(sb, result.creative_id)
            else "creatives"
        )
        existing = _existing_creative_finalize(sb, result.creative_id, table=table)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"creative not found: {result.creative_id}",
            )
        if existing.get("finalize_verified"):
            # Already finalized on a prior dispatch — idempotent skip.
            skipped.append(result.creative_id)
            continue
        update: dict[str, Any] = {
            "asset_name": result.asset_name,
            "finalized_at": finalized_at,
            "finalize_verified": result.verified,
        }
        if result.drive_folder_id is not None:
            update["drive_folder_id"] = result.drive_folder_id
        if result.file_path_drive is not None:
            update["file_path_drive"] = result.file_path_drive
        sb.table(table).update(update).eq("id", result.creative_id).execute()
        recorded.append(
            {
                "creative_id": result.creative_id,
                "asset_name": result.asset_name,
                "verified": result.verified,
            }
        )

    emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="assets_finalized",
        stage="finalize_assets",
        payload={"recorded": len(recorded), "skipped": len(skipped)},
    )
    log.info(
        "operator_finalize_persisted",
        pipeline_id=body.pipeline_id,
        recorded=len(recorded),
        skipped=len(skipped),
    )
    return {"ok": True, "recorded": recorded, "skipped": skipped}


# ===========================================================================
# POST /work/pipeline/tools/monitor_result
# ===========================================================================


class MonitorResult(BaseModel):
    """One ad's monitor read (GHL-truth KPIs + kill/watch/keep verdict)."""

    model_config = {"extra": "allow"}

    campaign_id: str = Field(..., min_length=1)
    ad_entity_id: str | None = None
    window_days: int = Field(..., ge=1)
    spend: float = Field(0.0, ge=0)
    impressions: int | None = None
    clicks: int | None = None
    ctr: float | None = None
    leads_meta: int | None = None
    ghl_leads: int = Field(0, ge=0)
    freq: float | None = None
    verdict: Literal["kill", "watch", "keep"]
    verdict_reason: str | None = None
    # Video-only engagement funnel (written to campaign_perf_video; ignored for
    # image pipelines). Optional so an image monitor read is unchanged.
    hook_rate: float | None = None
    drop_off_3s: float | None = None
    view_rate_avg: float | None = None
    watch_time_p50: float | None = None
    thruplays: int | None = None
    video_plays_3s: int | None = None
    completion_p25: float | None = None
    completion_p75: float | None = None
    completion_p100: float | None = None
    avg_watch_time_s: float | None = None


class MonitorInput(BaseModel):
    """POST body for ``/work/pipeline/tools/monitor_result``."""

    pipeline_id: str = Field(..., min_length=1)
    client_id: str | None = None
    results: list[MonitorResult] = Field(..., min_length=1)


def _cpl_real(spend: float, ghl_leads: int) -> float | None:
    """Real CPL = Meta spend / GHL leads. GHL is lead truth, never Meta.

    Returns None when there are zero GHL leads (an undefined CPL — the operator
    narrates "no leads yet", it must never divide by zero or imply a CPL).
    """
    if ghl_leads <= 0:
        return None
    return round(spend / ghl_leads, 4)


@router.post(
    "/work/pipeline/tools/monitor_result", dependencies=[Depends(verify_secret)]
)
async def persist_monitor_result(body: MonitorInput) -> dict[str, Any]:
    """Persist monitor KPIs as ``campaign_perf_{image,video}`` rows (pipeline-linked).

    Computes ``cpl_real = spend / ghl_leads`` (GHL is the lead source of truth)
    and writes one row per ad, linked to the pipeline (and the ad entity when
    supplied). Video pipelines (``pipelines.format_choice == 'video'``) write
    ``campaign_perf_video`` with the extra engagement funnel; image (and ``both``)
    write ``campaign_perf_image``. Idempotent on the daily-unique index: a
    ``(client, campaign, window, day)`` already pulled today is skipped so a
    re-dispatch does not duplicate the day's read. Returns the per-ad recorded set
    + the verdict tally.
    """
    pipeline = _require_pipeline(body.pipeline_id)
    sb = get_supabase_admin()
    client_id = body.client_id or pipeline.get("client_id")
    # Route by pipeline format. ('both' writes the image table; per-campaign video
    # routing for a mixed pipeline needs ad->creative linkage, deferred.)
    is_video = pipeline.get("format_choice") == "video"
    perf_table = "campaign_perf_video" if is_video else "campaign_perf_image"

    recorded: list[dict[str, Any]] = []
    skipped: list[str] = []
    tally: dict[str, int] = {"kill": 0, "watch": 0, "keep": 0}
    for result in body.results:
        # Daily idempotency: skip a row already pulled today for this key. We
        # match in Python on the captured rows (the unique index enforces it in
        # the live DB; this guard keeps a re-dispatch from raising on conflict).
        if _already_pulled_today(
            sb,
            client_id=client_id,
            campaign_id=result.campaign_id,
            window_days=result.window_days,
            table=perf_table,
        ):
            skipped.append(result.campaign_id)
            continue

        cpl_real = _cpl_real(result.spend, result.ghl_leads)
        row: dict[str, Any] = {
            "client_id": client_id,
            "pipeline_id": body.pipeline_id,
            "campaign_id": result.campaign_id,
            "window_days": result.window_days,
            "spend": result.spend,
            "impressions": result.impressions,
            "clicks": result.clicks,
            "ctr": result.ctr,
            "leads_meta": result.leads_meta,
            "leads_ghl": result.ghl_leads,
            "cpl_real": cpl_real,
            "freq": result.freq,
            "verdict": result.verdict,
            "verdict_reason": result.verdict_reason,
        }
        if result.ad_entity_id is not None:
            row["ad_entity_id"] = result.ad_entity_id
        if is_video:
            # Engagement funnel -> campaign_perf_video columns (omit unset).
            for k in (
                "hook_rate", "drop_off_3s", "view_rate_avg", "watch_time_p50",
                "thruplays", "video_plays_3s", "completion_p25",
                "completion_p75", "completion_p100", "avg_watch_time_s",
            ):
                v = getattr(result, k, None)
                if v is not None:
                    row[k] = v
        created = sb.table(perf_table).insert(row).execute().data
        perf_id = (
            created[0]["id"] if isinstance(created, list) and created else None
        )
        tally[result.verdict] += 1
        recorded.append(
            {
                "campaign_id": result.campaign_id,
                "verdict": result.verdict,
                "cpl_real": cpl_real,
                "perf_id": perf_id,
            }
        )

    emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="monitor_recorded",
        stage="monitor",
        payload={"recorded": len(recorded), "skipped": len(skipped), "tally": tally},
    )
    log.info(
        "operator_monitor_persisted",
        pipeline_id=body.pipeline_id,
        recorded=len(recorded),
        skipped=len(skipped),
    )
    return {"ok": True, "recorded": recorded, "skipped": skipped, "tally": tally}


def _already_pulled_today(
    sb: Any,
    *,
    client_id: Any,
    campaign_id: str,
    window_days: int,
    table: str = "campaign_perf_image",
) -> bool:
    """True when a perf row for (client, campaign, window) was pulled today (UTC).

    Mirrors the ``{table}_daily_uniq`` index so the route resumes idempotently
    rather than colliding on the unique constraint. ``table`` is
    ``campaign_perf_image`` (image) or ``campaign_perf_video`` (video).
    """
    resp = (
        sb.table(table)
        .select("id, pulled_at, window_days, campaign_id, client_id")
        .eq("campaign_id", campaign_id)
        .eq("window_days", window_days)
        .execute()
    )
    rows = resp.data if (resp is not None and isinstance(resp.data, list)) else []
    today = datetime.now(timezone.utc).date().isoformat()
    for row in rows:
        if client_id is not None and row.get("client_id") != client_id:
            continue
        pulled = str(row.get("pulled_at") or "")
        if pulled[:10] == today:
            return True
    return False


# ===========================================================================
# POST /work/pipeline/tools/monitor_action_result
# ===========================================================================


class MonitorActionResult(BaseModel):
    """One EXECUTED post-approval monitor action the operator applied on Meta.

    The operator already called the Meta MCP ``ads_update_entity`` (kill ->
    status PAUSED; scale -> raise daily_budget) BEFORE posting here -- the worker
    never touches Meta (operator-held MCP). This records the audited outcome.
    """

    model_config = {"extra": "allow"}

    decision: Literal["kill", "scale"]
    campaign_id: str | None = None
    ad_entity_id: str | None = None
    # The new daily budget a `scale` wrote to Meta (minor currency units, e.g.
    # cents). Required for a scale; ignored for a kill.
    target_budget: float | None = Field(None, gt=0)
    approved_by: str | None = None
    notes: str | None = None
    meta_payload: dict[str, Any] | None = None


class MonitorActionInput(BaseModel):
    """POST body for ``/work/pipeline/tools/monitor_action_result``."""

    pipeline_id: str = Field(..., min_length=1)
    client_id: str | None = None
    result: MonitorActionResult


@router.post(
    "/work/pipeline/tools/monitor_action_result",
    dependencies=[Depends(verify_secret)],
)
async def persist_monitor_action_result(body: MonitorActionInput) -> dict[str, Any]:
    """Record the EXECUTED kill/scale action the operator applied on Meta.

    Writes one ``monitor_action_result`` audit row (migration 0058): the verdict
    the operator executed (kill -> the campaign was paused on Meta; scale -> the
    campaign's daily_budget was raised to ``target_budget``), who approved it,
    the targeted Meta campaign + recorded ad entity, and the Meta MCP echo. This
    is the recorder half of the monitor connector: the worker has NO Meta
    credentials, so it audits what the operator already did rather than calling
    Meta itself (mirror of ``record_launch``). Emits a ``monitor_action_recorded``
    pipeline_event so the executed action is visible on the timeline. Returns the
    recorded row id + the resolved campaign id.
    """
    pipeline = _require_pipeline(body.pipeline_id)
    sb = get_supabase_admin()
    client_id = body.client_id or pipeline.get("client_id")
    result = body.result

    # A scale must carry the new budget; a kill must not (a pause has no budget).
    if result.decision == "scale" and result.target_budget is None:
        raise HTTPException(
            status_code=422,
            detail="scale requires target_budget (the new daily budget)",
        )

    row: dict[str, Any] = {
        "pipeline_id": body.pipeline_id,
        "client_id": client_id,
        "campaign_id": result.campaign_id,
        "decision": result.decision,
        "approved_by": result.approved_by,
        "notes": result.notes,
        "meta_payload": result.meta_payload or {},
    }
    if result.ad_entity_id is not None:
        row["ad_entity_id"] = result.ad_entity_id
    if result.decision == "scale":
        row["target_budget"] = result.target_budget

    created = sb.table("monitor_action_result").insert(row).execute().data
    action_id = created[0]["id"] if isinstance(created, list) and created else None

    emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="monitor_action_recorded",
        stage="monitor",
        payload={
            "decision": result.decision,
            "campaign_id": result.campaign_id,
            "target_budget": result.target_budget,
            "monitor_action_result_id": action_id,
        },
    )
    log.info(
        "operator_monitor_action_persisted",
        pipeline_id=body.pipeline_id,
        decision=result.decision,
        campaign_id=result.campaign_id,
        target_budget=result.target_budget,
    )
    return {
        "ok": True,
        "monitor_action_result_id": action_id,
        "decision": result.decision,
        "campaign_id": result.campaign_id,
    }


# ===========================================================================
# POST /work/pipeline/tools/signal
# ===========================================================================


class SignalInput(BaseModel):
    """POST body for ``/work/pipeline/tools/signal`` — dispatch tracking/heartbeat.

    ``status`` drives the lifecycle signal recorded on the ``work_item`` queue
    (kind ``operator_dispatch``):

      * ``dispatched`` — open a fresh signal (the operator acknowledges a kick).
      * ``running``    — heartbeat signal (the operator is still working).
      * ``completed`` / ``failed`` / ``timed_out`` — terminal close signal.

    ``stale``/``waiting``/``partial``/``error`` (operator narration verbs from
    the SKILL) map onto these states: ``stale``→``completed`` (a no-op
    dispatch is done), ``waiting``/``partial``→``running`` (still working, the
    workflow advances on the manager's gate), ``error``→``failed``.
    """

    pipeline_id: str = Field(..., min_length=1)
    dispatch_id: str = Field(..., min_length=1)
    stage: str | None = None
    status: Literal[
        "dispatched",
        "running",
        "completed",
        "failed",
        "timed_out",
        "stale",
        "waiting",
        "partial",
        "error",
    ]
    expected_status: str | None = None
    exec_id: str | None = None
    summary: str | None = None
    error: str | None = None


#: Operator narration verbs → the resolved dispatch status the work_item
#: signal records (kept as the DB string the dashboard surfaces).
_SIGNAL_DB_STATUS = {
    "dispatched": "dispatched",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "timed_out": "timed_out",
    "stale": "completed",
    "waiting": "running",
    "partial": "running",
    "error": "failed",
}

#: DB statuses that close a dispatch (stamp completed_at; watchdog ignores).
_TERMINAL_DISPATCH = {"completed", "failed", "timed_out"}


@router.post("/work/pipeline/tools/signal", dependencies=[Depends(verify_secret)])
async def signal_dispatch(body: SignalInput) -> dict[str, Any]:
    """Record one operator dispatch lifecycle signal on the audit log.

    Silent-failure PR-4: the legacy ``operator_dispatches`` table (renamed
    ``_legacy_operator_dispatches`` by 0051) was BOTH a work queue and an
    audit trail. The operator-daemon now owns the dispatch *work* lifecycle
    natively (claims `work_item(kind='operator_dispatch')`, runs hermes,
    PATCHes terminal status). The operator-skill's narration verbs
    (`dispatched`/`running`/`completed`/`failed`/`stale`/`waiting`/`partial`/
    `error`/`timed_out`) are AUDIT EVENTS -- not work units -- so this route
    records them as `pipeline_events` rows with `kind='operator_signal'`.
    Treating them as `work_item(kind='operator_dispatch')` would cause the
    daemon to re-claim each signal and spawn an empty hermes chat, which is
    the silent-failure class the redesign exists to eliminate.

    Append-only: a repeated signal is a repeated audit entry. The response
    shape preserves `dispatch_id` + resolved DB `status` + `terminal` for the
    operator skill; `event_id` replaces the prior `dispatch_row_id`.
    """
    _require_pipeline(body.pipeline_id)
    db_status = _SIGNAL_DB_STATUS[body.status]
    stage = body.stage or "configuration"

    payload: dict[str, Any] = {
        "dispatch_id": body.dispatch_id,
        "signal": body.status,
        "db_status": db_status,
        "terminal": db_status in _TERMINAL_DISPATCH,
    }
    if body.expected_status is not None:
        payload["expected_status"] = body.expected_status
    if body.exec_id is not None:
        payload["exec_id"] = body.exec_id
    if body.summary is not None:
        payload["summary"] = body.summary
    if body.error is not None:
        payload["error"] = body.error

    event_id = emit_pipeline_event(
        pipeline_id=body.pipeline_id,
        kind="operator_signal",
        stage=stage,
        payload=payload,
    )

    log.info(
        "operator_signal",
        pipeline_id=body.pipeline_id,
        dispatch_id=body.dispatch_id,
        signal=body.status,
        db_status=db_status,
        event_id=event_id,
    )
    return {
        "ok": True,
        "dispatch_id": body.dispatch_id,
        "status": db_status,
        "event_id": event_id,
        "terminal": db_status in _TERMINAL_DISPATCH,
    }


__all__ = [
    "router",
    "CopyInput",
    "CopyVariant",
    "SpecInput",
    "SpecResult",
    "FinalizeInput",
    "FinalizeResult",
    "MonitorInput",
    "MonitorResult",
    "MonitorActionInput",
    "MonitorActionResult",
    "SignalInput",
]
