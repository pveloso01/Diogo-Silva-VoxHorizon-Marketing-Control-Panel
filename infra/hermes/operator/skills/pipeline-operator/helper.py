"""Thin worker-tool client for the pipeline operator agent.

This is the *mechanical* half of the ``pipeline-operator`` skill. ``SKILL.md``
is the playbook (when to read state, when to author a brief, when to render);
this module is the typed, validated HTTP surface the operator calls to talk to
the Wave-A worker endpoints:

* ``GET  {WORKER_BASE_URL}/work/pipeline/tools/{pipeline_id}``  → read state
* ``GET  {WORKER_BASE_URL}/work/client/{client_id}``            → client context
* ``POST {WORKER_BASE_URL}/work/pipeline/tools/brief``          → author brief
* ``POST {WORKER_BASE_URL}/work/pipeline/tools/render``         → render via Kie
* ``POST {WORKER_BASE_URL}/work/pipeline/tools/store_creative`` → store codex bytes

Conventions match the sibling dashboard skills: synchronous ``httpx.Client``,
a custom error type, lazy env reads (so importing is free and tests need no
env), and a fresh client per call.

Render routing (ideation always free; finals per-pipeline)
----------------------------------------------------------
``pipeline_operator_render`` routes by ``kind``, NOT by an env var:

* IDEATION (``kind="concept_preview"``) is HARDWIRED to the FREE codex model
  (gpt-image-2, LOW quality): each image is generated IN THE OPERATOR CONTAINER
  via Hermes' codex image-gen plugin (the operator's ChatGPT/Codex subscription,
  $0; see :mod:`codex_render`) and uploaded to ``/store_creative``. It is never
  selectable to a paid model.
* FINALS (``kind="final"``) use the manager's PER-PIPELINE "Finals model" choice,
  persisted at kickoff on ``config_draft.finals_render_backend`` +
  ``finals_render_model`` (default: the free codex model). The codex backend
  renders in-container at HIGH quality; the ``kie`` backend POSTs the chosen Kie
  model id (nano-banana-2 / Flux / Seedream) to ``/render``.

Both backends produce identical worker-side rows / events / cost lines, so the
dashboard is unaffected by the choice; only the bill is. The legacy
``RENDER_BACKEND`` env is no longer consulted for routing.

Tool-name surface (THE GATING CONTRACT)
---------------------------------------
The approval plugin (voxhorizon-approvals) gates tool calls **by tool name**
(exact match against its allowlist / requires-approval sets — see
``ekko-plugins/voxhorizon_approvals/policy.py::evaluate``). The operator
capabilities are exposed under *distinct, stable* entrypoint names that the
operator policy references one-for-one:

* ``pipeline_operator_read``        — the READ tool (allowlisted; no spend)
* ``pipeline_operator_client_read`` — the CLIENT-CONTEXT tool (allowlisted; no spend)
* ``pipeline_operator_brief``       — the BRIEF tool (free Supabase write)
* ``pipeline_operator_render``      — the RENDER tool (free codex render, $0; allowlisted)
* the stage-persist tools (qa/compliance/copy/spec/finalize/monitor/signal) are
  all allowlisted too: they only submit results for the worker to write/adjudicate

Do NOT rename these tools without updating ``policy.operator.yaml`` in the
plugin — the gate keys on the exact names. The one approval-gated action is the
Meta launch (``pipeline_operator_launch``), the integrations agent's tool, not
implemented here.
``get_pipeline`` / ``post_brief`` / ``post_render`` remain as readable
aliases, but the gating-canonical names are the ``pipeline_operator_*`` ones.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class PipelineOperatorError(Exception):
    """Raised when a worker tool call fails (network, HTTP error, or config)."""


#: Worker base URL env var (e.g. ``http://worker:8000``).
ENV_WORKER_BASE_URL = "WORKER_BASE_URL"
#: Bearer shared secret env var (matches the worker's ``verify_secret``).
ENV_WORKER_SHARED_SECRET = "WORKER_SHARED_SECRET"
#: Render backend selector env var. ``openai-codex`` (default) generates the
#: image in-container via the operator's ChatGPT/Codex subscription ($0) and
#: uploads the bytes to the worker; ``kie`` POSTs to the worker's Kie /render
#: path (the legacy paid fallback).
ENV_RENDER_BACKEND = "RENDER_BACKEND"

#: Route prefix for the operator tool endpoints on the worker.
_TOOLS_PREFIX = "/work/pipeline/tools"

#: The render ``kind`` values the worker accepts (Wave A contract).
RENDER_KINDS: frozenset[str] = frozenset({"concept_preview", "final"})

#: Supported render backends.
BACKEND_OPENAI_CODEX = "openai-codex"
BACKEND_KIE = "kie"
RENDER_BACKENDS: frozenset[str] = frozenset({BACKEND_OPENAI_CODEX, BACKEND_KIE})
#: Default backend: the operator's subscription-backed codex renderer (free).
DEFAULT_RENDER_BACKEND = BACKEND_OPENAI_CODEX

#: The FREE image model — codex/gpt-image-2. IDEATION (concept_preview) ALWAYS
#: uses this, regardless of any per-pipeline finals choice; it is the default
#: for finals too. Never selectable to a paid model for ideation.
FREE_MODEL = "gpt-image-2"

# ---------------------------------------------------------------------------
# Finals model registry — the manager's per-pipeline "Finals model" choice.
# ---------------------------------------------------------------------------
#
# Each entry maps a manager-facing label to the (backend, model) that renders
# the FINALS (generation) stage for that pipeline. IDEATION is NOT in here — it
# is hardwired to the free codex/gpt-image-2 model below. The label set here is
# the single source of truth the dashboard mirrors in its dropdown.
#
# Verified Kie model ids (see worker/src/services/kie.py): nano-banana-2,
# flux-2/pro-text-to-image (Flux), bytedance/seedream-v4-text-to-image (Seedream).
FINALS_MODELS: dict[str, dict[str, str]] = {
    "gpt-image-2 (free)": {"backend": BACKEND_OPENAI_CODEX, "model": FREE_MODEL},
    "nano-banana-2": {"backend": BACKEND_KIE, "model": "nano-banana-2"},
    "Flux": {"backend": BACKEND_KIE, "model": "flux-2/pro-text-to-image"},
    "Seedream": {
        "backend": BACKEND_KIE,
        "model": "bytedance/seedream-v4-text-to-image",
    },
}
#: Default finals model label — the FREE one.
DEFAULT_FINALS_LABEL = "nano-banana-2"

#: Per-kind render parameters for the codex backend, mirroring the worker's
#: SOP (``pipeline_tools._CONCEPT_PREVIEW`` / ``_FINAL``): which ratios to
#: render, the version string each creative is stamped with, and the codex image
#: quality (ideation is LOW/cheap; finals are HIGH for production-grade output).
_KIND_PARAMS: dict[str, dict[str, Any]] = {
    "concept_preview": {
        "ratios": ("1x1",),
        "version": "v0.ideation",
        "quality": "low",
    },
    "final": {"ratios": ("1x1", "9x16"), "version": "v1.0", "quality": "high"},
}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    """Build an httpx client pointed at the worker, authed with the secret.

    Reads ``WORKER_BASE_URL`` and ``WORKER_SHARED_SECRET`` from the
    environment. Both must be set; an empty string counts as unset so a
    misconfigured ``.env`` fails loudly rather than calling an anon worker.

    The read timeout is generous (120s) because ``render`` is a synchronous
    batch — the operator waits for Kie to finish so it can narrate the
    results to the manager.
    """
    url = os.environ.get(ENV_WORKER_BASE_URL, "").strip().rstrip("/")
    secret = os.environ.get(ENV_WORKER_SHARED_SECRET, "").strip()
    if not url or not secret:
        raise PipelineOperatorError(
            f"{ENV_WORKER_BASE_URL} or {ENV_WORKER_SHARED_SECRET} not set"
        )
    return httpx.Client(
        base_url=url,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=30.0),
    )


def _request(
    method: str, path: str, *, json_body: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Issue one worker call and return the decoded JSON object.

    Any transport error or non-2xx is re-raised as
    :class:`PipelineOperatorError` with the response body so the operator can
    narrate a clear failure to the manager rather than retrying blindly.
    """
    try:
        with _client() as c:
            resp = c.request(method, path, json=json_body)
    except httpx.HTTPError as exc:
        raise PipelineOperatorError(
            f"network error calling {method} {path}: {exc}"
        ) from exc

    if resp.status_code >= 300:
        snippet = resp.text[:500] if resp.text else ""
        raise PipelineOperatorError(
            f"{method} {path} failed: {resp.status_code} {snippet}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise PipelineOperatorError(
            f"{method} {path} returned non-JSON body: {resp.text[:200]}"
        ) from exc

    if not isinstance(data, dict):
        raise PipelineOperatorError(
            f"{method} {path} returned non-object body: {resp.text[:200]}"
        )
    return data


def _require_pipeline_id(pipeline_id: Any) -> str:
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise PipelineOperatorError("pipeline_id must be a non-empty string")
    return pipeline_id.strip()


def _require_client_id(client_id: Any) -> str:
    if not isinstance(client_id, str) or not client_id.strip():
        raise PipelineOperatorError("client_id must be a non-empty string")
    return client_id.strip()


def _resolve_finals_model(state: dict[str, Any]) -> tuple[str, str]:
    """Resolve the per-pipeline FINALS (backend, model) from pipeline state.

    The manager's "Finals model" choice is persisted at kickoff on
    ``config_draft.finals_render_backend`` + ``finals_render_model``. When absent
    or unrecognized we fall back to the FREE default (codex/gpt-image-2) so a
    pipeline created before this feature, or with a stale value, still renders
    finals for $0. IDEATION never calls this — it is hardwired to the free model.
    """
    default = FINALS_MODELS[DEFAULT_FINALS_LABEL]
    config_draft = state.get("config_draft")
    if not isinstance(config_draft, dict):
        return default["backend"], default["model"]
    backend = config_draft.get("finals_render_backend")
    model = config_draft.get("finals_render_model")
    if not isinstance(backend, str) or backend not in RENDER_BACKENDS:
        return default["backend"], default["model"]
    if not isinstance(model, str) or not model.strip():
        return default["backend"], default["model"]
    return backend, model.strip()


# ---------------------------------------------------------------------------
# READ — pipeline_operator_read (allowlisted by the operator policy)
# ---------------------------------------------------------------------------


def pipeline_operator_read(pipeline_id: str) -> dict[str, Any]:
    """Read the full pipeline state for ``pipeline_id``.

    Calls ``GET {WORKER_BASE_URL}/work/pipeline/tools/{pipeline_id}`` and
    returns the worker's JSON: ``status``, ``format_choice``, ``config_draft``,
    ``picks``, ``brief``, ``concepts``, ``finals``, ``creatives``, ``client``,
    ``events_tail``.

    ``creatives`` is the PER-CREATIVE GATE ROLLUP — one entry per creative with
    a ``stage_state`` map ``{creative_qa, compliance_review, copy,
    spec_validation}`` → ``pending|in_progress|passed|failed|overridden|
    skipped`` (plus the creative lifecycle ``status``). The operator reads it to
    find the OUTSTANDING creatives for a per-creative stage and resume by
    skip-done. It is ``[]`` until the first per-creative stage runs.

    This is the operator's first move on every dispatch. It performs NO spend
    and the operator policy ALLOWLISTS it, so it never round-trips the manager.

    Raises:
        PipelineOperatorError: On missing env, network failure, non-2xx
            (e.g. 404 when the pipeline is missing), or a non-object body.
    """
    pid = _require_pipeline_id(pipeline_id)
    return _request("GET", f"{_TOOLS_PREFIX}/{pid}")


# ---------------------------------------------------------------------------
# CLIENT READ — pipeline_operator_client_read (allowlisted; pure GET, no spend)
# ---------------------------------------------------------------------------


def pipeline_operator_client_read(client_id: str) -> dict[str, Any]:
    """Read the full client context for ``client_id``.

    Calls ``GET {WORKER_BASE_URL}/work/client/{client_id}`` and returns the
    worker's JSON: ``slug``, ``name``, ``service_type``, ``brand_colors``,
    ``profile`` (the typed ``client_profiles`` row or null), ``offers``,
    ``offer_constraints`` (the do-not-say rules), ``services``, ``value_props``
    (``usps`` / ``differentiators``), ``assets``, and ``past_projects``.

    Use this after ``pipeline_operator_read`` whenever the pipeline is linked to
    a client, so you can author on-brand, compliant ads from the client's REAL
    offers, brand voice, and proof points. This performs NO spend and the
    operator policy ALLOWLISTS it, so it never round-trips the manager.

    Raises:
        PipelineOperatorError: On missing env, network failure, non-2xx
            (e.g. 404 when the client is missing), or a non-object body.
    """
    cid = _require_client_id(client_id)
    return _request("GET", f"/work/client/{cid}")


# ---------------------------------------------------------------------------
# BRIEF — pipeline_operator_brief (free Supabase write; not spend-gated)
# ---------------------------------------------------------------------------


def pipeline_operator_brief(
    *,
    pipeline_id: str,
    image_payload: dict[str, Any],
    notes: Optional[str] = None,
    concepts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Author / upsert the image brief for the pipeline.

    Calls ``POST {WORKER_BASE_URL}/work/pipeline/tools/brief`` with
    ``{pipeline_id, image_payload, notes?, concepts?}``. ``image_payload`` must
    carry the worker-required key ``market`` plus EITHER a non-empty
    ``concepts`` list (concepts-first: the per-concept prompts carry the
    creative direction) OR both ``offer_text`` and ``angles`` (offer-first).
    Build it with the ``image-ad-authoring`` skill so it is valid before it
    leaves the agent.

    ``concepts`` is the full set of N concept specs (each
    ``{concept | concept_name, prompt, offer_text?}``, build them with
    ``build_concept`` and ``assert_distinct_concepts``). Persisting them HERE,
    at brief time, is what
    lets the ideation render run as a single DETERMINISTIC, worker-driven pass
    over the stored plan: ``pipeline_operator_render(pipeline_id, "concept_preview")``
    then renders ALL persisted concepts with no LLM in the per-image loop, and a
    retried render resumes the remainder instead of re-authoring. Author every
    concept up front and pass them all here.

    This is a free database write (no paid API), so the operator policy does
    not gate it for spend; the manager reviews the brief through the dashboard
    stage gate instead. Returns ``{ok, brief_id}``.

    Raises:
        PipelineOperatorError: On bad input, missing env, or a failed call.
    """
    pid = _require_pipeline_id(pipeline_id)
    if not isinstance(image_payload, dict) or not image_payload:
        raise PipelineOperatorError("image_payload must be a non-empty dict")
    if "market" not in image_payload:
        raise PipelineOperatorError(
            "image_payload missing required key: ['market']"
        )
    # A brief is valid in either shape: concepts-first (market + a non-empty
    # `concepts` list, the per-concept prompts carrying the creative direction)
    # or offer-first (market + offer_text + angles). Require one of the two.
    has_concepts = isinstance(concepts, list) and len(concepts) > 0
    has_offer = "offer_text" in image_payload and "angles" in image_payload
    if not has_concepts and not has_offer:
        raise PipelineOperatorError(
            "image_payload needs either a non-empty `concepts` list or both "
            "`offer_text` and `angles`"
        )

    body: dict[str, Any] = {"pipeline_id": pid, "image_payload": image_payload}
    if notes is not None:
        if not isinstance(notes, str):
            raise PipelineOperatorError("notes must be a string or None")
        body["notes"] = notes
    if concepts is not None:
        body["concepts"] = _normalize_concept_specs(concepts)
    return _request("POST", f"{_TOOLS_PREFIX}/brief", json_body=body)


def pipeline_operator_video_brief(
    *,
    pipeline_id: str,
    video_payload: dict[str, Any],
    notes: Optional[str] = None,
    concepts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Author / upsert the VIDEO brief for the pipeline.

    Calls ``POST {WORKER_BASE_URL}/work/pipeline/tools/video/brief`` with
    ``{pipeline_id, video_payload, notes?, concepts?}``. ``video_payload`` must
    carry ``market``, ``offer_text``, ``angles``, ``target_duration_s``,
    ``voice_id`` — build it with the ``video-ad-authoring`` skill's
    ``build_video_brief``. ``concepts`` is the N script concepts (each
    ``{concept, angle?, script}`` from ``build_video_concept`` +
    ``assert_distinct_concepts``). Free database write (no paid API), so it is not
    spend-gated; the manager reviews the brief at the dashboard stage gate.
    Returns ``{ok, brief_id}``.

    Raises:
        PipelineOperatorError: On bad input, missing env, or a failed call.
    """
    pid = _require_pipeline_id(pipeline_id)
    if not isinstance(video_payload, dict) or not video_payload:
        raise PipelineOperatorError("video_payload must be a non-empty dict")
    required = ("market", "offer_text", "angles", "target_duration_s", "voice_id")
    missing = [k for k in required if k not in video_payload]
    if missing:
        raise PipelineOperatorError(
            f"video_payload missing required keys: {missing}"
        )
    body: dict[str, Any] = {"pipeline_id": pid, "video_payload": video_payload}
    if notes is not None:
        if not isinstance(notes, str):
            raise PipelineOperatorError("notes must be a string or None")
        body["notes"] = notes
    if concepts is not None:
        if not isinstance(concepts, list) or not concepts:
            raise PipelineOperatorError("concepts must be a non-empty list")
        for c in concepts:
            if not isinstance(c, dict) or "concept" not in c or "script" not in c:
                raise PipelineOperatorError(
                    "each video concept must have 'concept' and 'script'"
                )
        body["concepts"] = concepts
    return _request("POST", f"{_TOOLS_PREFIX}/video/brief", json_body=body)


def pipeline_operator_video_render(
    *,
    pipeline_id: str,
    estimated_cost_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Trigger video generation for the pipeline's picked video concepts.

    Calls ``POST {WORKER_BASE_URL}/work/pipeline/generation``, which fans out the
    video substage chain (script -> voiceover -> b-roll -> compose -> caption) for
    each picked video creative in the background. THIS SPENDS (kie generation).

    ``estimated_cost_usd`` is the operator's per-ad cost estimate (sum the kie clip
    cost across the script segments). The approval gate reads this arg: at or under
    the threshold the render runs inline; over it (or if omitted) the manager
    approves first. The worker also enforces a hard per-ad budget cap before any
    submit, so this estimate is the gate SIGNAL, not the enforcement. Only
    ``pipeline_id`` is sent to the worker. Returns the generation-accepted body.

    Raises:
        PipelineOperatorError: On bad input, missing env, or a failed call.
    """
    pid = _require_pipeline_id(pipeline_id)
    if estimated_cost_usd is not None and (
        not isinstance(estimated_cost_usd, (int, float))
        or isinstance(estimated_cost_usd, bool)
    ):
        raise PipelineOperatorError("estimated_cost_usd must be a number or None")
    return _request(
        "POST", "/work/pipeline/generation", json_body={"pipeline_id": pid}
    )


def _normalize_concept_specs(
    concepts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate + normalize the persisted concept specs for the brief body.

    Each must carry a label (``concept``, or ``concept_name`` which the operator
    authoring skill emits and which is normalised to ``concept`` here) and a
    ``prompt`` (+ optional ``offer_text`` / ``parent_creative_id`` passthrough).
    Mirrors the per-item validation in :func:`pipeline_operator_render` so a
    malformed plan fails loudly at brief time rather than producing a broken
    deterministic render later.
    """
    if not isinstance(concepts, list) or not concepts:
        raise PipelineOperatorError("concepts must be a non-empty list")
    out: list[dict[str, Any]] = []
    for idx, spec in enumerate(concepts):
        if not isinstance(spec, dict):
            raise PipelineOperatorError(f"concepts[{idx}] must be a dict")
        concept = spec.get("concept") or spec.get("concept_name")
        prompt = spec.get("prompt")
        if not isinstance(concept, str) or not concept.strip():
            raise PipelineOperatorError(
                f"concepts[{idx}].concept must be a non-empty string"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise PipelineOperatorError(
                f"concepts[{idx}].prompt must be a non-empty string"
            )
        norm: dict[str, Any] = {
            "concept": concept.strip(),
            "prompt": prompt.strip(),
        }
        offer_text = spec.get("offer_text")
        if offer_text is not None:
            if not isinstance(offer_text, str):
                raise PipelineOperatorError(
                    f"concepts[{idx}].offer_text must be a string"
                )
            norm["offer_text"] = offer_text
        out.append(norm)
    return out


# ---------------------------------------------------------------------------
# RENDER — pipeline_operator_render (free codex render; allowlisted)
# ---------------------------------------------------------------------------


def pipeline_operator_render(
    *,
    pipeline_id: str,
    kind: str,
    items: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Render a stage's images in ONE deterministic pass (free codex render).

    **Preferred (deterministic) usage: omit ``items``.** The worker/helper then
    fans out over the PERSISTED plan — every concept spec authored at brief time
    (``concept_preview``) or one final per pick (``final``) — and renders ALL of
    them in a single pass. The operator just triggers the stage; it does NOT
    author or loop items at render time, so a slow render can never collapse to
    "only one concept landed". A retried render resumes the remainder
    idempotently (already-rendered concepts are skipped). This is the path the
    SKILL prescribes — persist all N concepts via ``pipeline_operator_brief`` and
    then call ``pipeline_operator_render(pipeline_id, kind)``.

    Legacy / explicit usage: pass ``items`` (``{concept, prompt, offer_text?,
    parent_creative_id?}``) to render exactly those. Kept for back-compat and
    one-off renders.

    * ``kind="concept_preview"`` renders 1:1 previews (ideation). IDEATION IS
      ALWAYS FREE: it renders via codex/gpt-image-2 (LOW quality) regardless of
      the ``RENDER_BACKEND`` env or the pipeline's finals choice — never paid,
      never selectable. The deterministic path renders ALL persisted concepts in
      one approval.
    * ``kind="final"`` renders 1:1 + 9:16 finals (generation). The image model is
      the manager's PER-PIPELINE "Finals model" choice, persisted at kickoff on
      ``config_draft.finals_render_backend`` + ``finals_render_model`` (default:
      the free codex/gpt-image-2). Each item MUST carry ``parent_creative_id``
      (the picked concept it derives from); the deterministic path threads it
      from the picks automatically. 9:16 is a TRUE 9:16 (864x1536) on codex.

    **Finals backend (per pipeline, NOT the env):**

    * ``openai-codex`` — generate each image IN-CONTAINER via the operator's
      ChatGPT/Codex subscription (gpt-image-2 through the Codex Responses
      ``image_generation`` tool, $0, HIGH quality for finals) and upload the
      bytes to the worker's ``/work/pipeline/tools/store_creative``. No paid API.
      This is also the ideation path (LOW quality).
    * ``kie`` — POST to the worker's ``/work/pipeline/tools/render`` (the paid Kie
      path) with the chosen Kie model id (nano-banana-2 / Flux / Seedream).

    Either way the worker emits the SAME pipeline_events + cost line + creative
    row, so the dashboard, the auto-advance trigger, and the cost aggregator
    behave identically. Rendering is FREE and ALLOWLISTED (no per-call approval):
    the manager supervises spend at the dashboard STAGE gates, and the only
    approval-gated tool is ``pipeline_operator_launch`` (the Meta launch).

    Returns ``{ok, renders:[...], total_cost_usd, errors:[...]}`` for both
    backends (the codex path synthesizes the same shape; ``total_cost_usd`` is
    0 there).

    Raises:
        PipelineOperatorError: On bad input, missing env, or a failed call.
            Per-item render failures are reported inside ``errors`` with a 2xx
            (they do NOT raise here), matching the Kie path.
    """
    pid = _require_pipeline_id(pipeline_id)
    if kind not in RENDER_KINDS:
        raise PipelineOperatorError(
            f"kind must be one of {sorted(RENDER_KINDS)}, got {kind!r}"
        )

    # Resolve the (backend, model) for THIS render.
    #   * IDEATION (concept_preview) is HARDWIRED to the free codex/gpt-image-2.
    #     It never consults RENDER_BACKEND or the finals choice — it can never be
    #     a paid model.
    #   * FINALS read the manager's per-pipeline choice from pipeline state
    #     (config_draft.finals_render_backend / finals_render_model), defaulting
    #     to the free codex model.
    if kind == "concept_preview":
        backend, model = BACKEND_OPENAI_CODEX, FREE_MODEL
        state: Optional[dict[str, Any]] = None
    else:
        state = pipeline_operator_read(pid)
        backend, model = _resolve_finals_model(state)

    # Deterministic path: no items supplied → render the persisted plan.
    if items is None:
        if backend == BACKEND_KIE:
            # The worker resolves the persisted plan itself for the Kie path; we
            # thread the chosen model id so it renders with the finals model.
            body = {"pipeline_id": pid, "kind": kind, "model": model}
            return _request("POST", f"{_TOOLS_PREFIX}/render", json_body=body)
        resolved = _resolve_deterministic_items(pid, kind, state=state)
        if not resolved:
            # Nothing to render — empty plan or everything already done.
            return {
                "ok": True,
                "renders": [],
                "total_cost_usd": 0,
                "errors": [],
                "skipped": [],
            }
        return _render_via_codex(pipeline_id=pid, kind=kind, items=resolved)

    if not isinstance(items, list) or not items:
        raise PipelineOperatorError("items must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise PipelineOperatorError(f"items[{idx}] must be a dict")
        concept = item.get("concept")
        prompt = item.get("prompt")
        if not isinstance(concept, str) or not concept.strip():
            raise PipelineOperatorError(
                f"items[{idx}].concept must be a non-empty string"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise PipelineOperatorError(
                f"items[{idx}].prompt must be a non-empty string"
            )
        out: dict[str, Any] = {
            "concept": concept.strip(),
            "prompt": prompt.strip(),
        }
        offer_text = item.get("offer_text")
        if offer_text is not None:
            if not isinstance(offer_text, str):
                raise PipelineOperatorError(
                    f"items[{idx}].offer_text must be a string"
                )
            out["offer_text"] = offer_text
        parent = item.get("parent_creative_id")
        if kind == "final":
            # Finals are children of a picked concept; the worker needs the
            # parent to set creatives.parent_creative_id.
            if not isinstance(parent, str) or not parent.strip():
                raise PipelineOperatorError(
                    f"items[{idx}].parent_creative_id is required for "
                    "kind='final'"
                )
            out["parent_creative_id"] = parent.strip()
        elif parent is not None:
            if not isinstance(parent, str):
                raise PipelineOperatorError(
                    f"items[{idx}].parent_creative_id must be a string"
                )
            out["parent_creative_id"] = parent
        normalized.append(out)

    if backend == BACKEND_KIE:
        body = {
            "pipeline_id": pid,
            "kind": kind,
            "items": normalized,
            "model": model,
        }
        return _request("POST", f"{_TOOLS_PREFIX}/render", json_body=body)
    return _render_via_codex(pipeline_id=pid, kind=kind, items=normalized)


def _resolve_deterministic_items(
    pipeline_id: str, kind: str, *, state: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    """Build the codex render batch from the PERSISTED plan (no items supplied).

    Reads pipeline state and, for ``concept_preview``, returns every persisted
    concept spec not yet stored at ``v0.ideation``; for ``final``, returns one
    item per picked creative (``parent_creative_id`` threaded) not yet stored at
    ``v1``. The already-rendered skip makes a retried render resume the
    remainder — the exact fix for a pipeline stuck at 1/N concepts.

    This keeps the LLM out of the per-image loop entirely: the operator triggers
    the stage, and the deterministic plan comes from the brief it already
    authored, not from prompts re-held across a long synchronous render.

    ``state`` is an optional already-fetched pipeline read (the finals path reads
    it once to resolve the per-pipeline model and reuses it here); when omitted we
    fetch it.
    """
    if state is None:
        state = pipeline_operator_read(pipeline_id)
    specs = _persisted_concepts_from_state(state)

    if kind == "concept_preview":
        done = {
            c.get("concept")
            for c in (state.get("concepts") or [])
            if isinstance(c, dict) and c.get("concept")
        }
        out: list[dict[str, Any]] = []
        for spec in specs:
            concept = str(spec.get("concept") or "").strip()
            prompt = str(spec.get("prompt") or "").strip()
            if not concept or not prompt or concept in done:
                continue
            item: dict[str, Any] = {"concept": concept, "prompt": prompt}
            if spec.get("offer_text"):
                item["offer_text"] = spec["offer_text"]
            out.append(item)
        return out

    # final: one item per pick; recover concept + prompt from the picked
    # creative and the persisted plan, thread parent_creative_id.
    picks = state.get("picks")
    picked_ids = (picks.get("image", []) if isinstance(picks, dict) else []) or []
    specs_by_concept = {
        str(s.get("concept")): s for s in specs if s.get("concept")
    }
    by_id = {
        c.get("creative_id"): c
        for c in (state.get("concepts") or [])
        if isinstance(c, dict)
    }
    done = {
        f.get("concept")
        for f in (state.get("finals") or [])
        if isinstance(f, dict) and f.get("concept")
    }
    out = []
    for cid in picked_ids:
        row = by_id.get(cid)
        if not isinstance(row, dict):
            continue
        concept = str(row.get("concept") or "").strip()
        if not concept or concept in done:
            continue
        spec = specs_by_concept.get(concept)
        prompt = (
            str(spec.get("prompt")).strip()
            if spec and spec.get("prompt")
            else None
        )
        if not prompt:
            continue
        item = {
            "concept": concept,
            "prompt": prompt,
            "parent_creative_id": str(cid),
        }
        if spec and spec.get("offer_text"):
            item["offer_text"] = spec["offer_text"]
        out.append(item)
    return out


def _persisted_concepts_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the persisted concept specs out of a pipeline read response.

    The brief endpoint mirrors them onto ``config_draft.concepts`` (and the
    brief payload's ``concepts`` key); prefer config_draft, fall back to the
    brief payload.
    """
    config_draft = state.get("config_draft")
    if isinstance(config_draft, dict):
        specs = config_draft.get("concepts")
        if isinstance(specs, list) and specs:
            return [s for s in specs if isinstance(s, dict)]
        payload = config_draft.get("image_payload")
        if isinstance(payload, dict):
            specs = payload.get("concepts")
            if isinstance(specs, list) and specs:
                return [s for s in specs if isinstance(s, dict)]
    brief = state.get("brief")
    if isinstance(brief, dict) and isinstance(brief.get("payload"), dict):
        specs = brief["payload"].get("concepts")
        if isinstance(specs, list) and specs:
            return [s for s in specs if isinstance(s, dict)]
    return []


# ---------------------------------------------------------------------------
# STORE — the worker side of the codex render (free; store pre-rendered bytes)
# ---------------------------------------------------------------------------


def pipeline_operator_store_creative(
    *,
    pipeline_id: str,
    kind: str,
    concept: str,
    ratio: str,
    version: str,
    prompt: str,
    image_b64: str,
    offer_text: Optional[str] = None,
    parent_creative_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a pre-rendered (codex) image to the worker as a creative.

    Calls ``POST {WORKER_BASE_URL}/work/pipeline/tools/store_creative``. The
    worker uploads the bytes, records the creative + iteration + event rows,
    emits the same task_running/task_done pipeline_events, and records a
    zero-cost line against ``openai-codex``. Returns
    ``{creative_id, file_path_supabase, version}``.

    This is an internal helper for the codex render backend (not a separately
    gated tool): it is reached only THROUGH ``pipeline_operator_render`` after
    the spend gate has already cleared.
    """
    body: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "kind": kind,
        "concept": concept,
        "ratio": ratio,
        "version": version,
        "prompt": prompt,
        "image_b64": image_b64,
    }
    if offer_text is not None:
        body["offer_text"] = offer_text
    if parent_creative_id is not None:
        body["parent_creative_id"] = parent_creative_id
    return _request("POST", f"{_TOOLS_PREFIX}/store_creative", json_body=body)


def _render_via_codex(
    *, pipeline_id: str, kind: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Generate each item's image(s) via codex, then store them on the worker.

    Mirrors the worker's per-kind ratio fan-out (concept_preview → 1x1; final →
    1x1 + 9x16) and synthesizes the same ``{ok, renders, total_cost_usd,
    errors}`` shape the Kie ``/render`` path returns. Per-item failures land in
    ``errors`` (a 2xx-equivalent) so one bad render doesn't abort the batch —
    matching the Kie path's behaviour.

    The image bytes are generated in-process via :mod:`codex_render` (the
    operator's ChatGPT/Codex subscription) and uploaded base64-encoded; cost is
    always 0 because nothing is paid.
    """
    import base64 as _b64

    # Imported lazily so the Kie path and the unit tests don't require Hermes.
    import codex_render

    params = _KIND_PARAMS[kind]
    ratios: tuple[str, ...] = params["ratios"]
    version: str = params["version"]
    # Per-kind codex image quality: ideation is LOW (cheap previews), finals are
    # HIGH (production-grade). Passed explicitly so the operator container's
    # OPENAI_IMAGE_QUALITY env (set to low for ideation) can't downgrade finals.
    quality: str = params["quality"]

    renders: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in items:
        concept = item["concept"]
        prompt = item["prompt"]
        offer_text = item.get("offer_text")
        parent = item.get("parent_creative_id")
        for ratio in ratios:
            try:
                image_bytes = codex_render.render_image(
                    prompt, ratio, quality=quality
                )
                image_b64 = _b64.b64encode(image_bytes).decode("ascii")
                stored = pipeline_operator_store_creative(
                    pipeline_id=pipeline_id,
                    kind=kind,
                    concept=concept,
                    ratio=ratio,
                    version=version,
                    prompt=prompt,
                    image_b64=image_b64,
                    offer_text=offer_text,
                    parent_creative_id=parent,
                )
                renders.append(
                    {
                        "creative_id": stored.get("creative_id"),
                        "concept": concept,
                        "ratio": ratio,
                        "file_path_supabase": stored.get("file_path_supabase"),
                        "cost_usd": 0,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — collect, don't abort batch
                errors.append(
                    {"concept": concept, "ratio": ratio, "error": str(exc)}
                )

    return {
        "ok": True,
        "renders": renders,
        "total_cost_usd": 0,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# STAGE-PERSIST tools (P3) — the post-generation persistence wrappers.
#
# Each delegates straight to a worker endpoint that VALIDATES + WRITES the
# result (and rolls the per-(creative, stage) gate forward). The operator has
# NO tool here that clears a gate or writes a compliance pass: qa/compliance
# only SUBMIT (the worker adjudicates), copy/spec/finalize record evidence, and
# the manager's audited action at the gate is what advances the pipeline.
#
# All are array-shaped (one call per stage, not per creative) and idempotent
# (the worker resumes by skip-done). They are allowlisted in
# policy.operator.yaml — no spend, so no per-call approval; the manager gates at
# the dashboard stage gates. (pipeline_operator_launch is the EXCEPTION — it
# requires approval and is the integrations agent's tool, not built here.)
# ---------------------------------------------------------------------------


def _require_list(value: Any, name: str) -> list[dict[str, Any]]:
    """Validate an array payload is a non-empty list of dicts before it ships."""
    if not isinstance(value, list) or not value:
        raise PipelineOperatorError(f"{name} must be a non-empty list")
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise PipelineOperatorError(f"{name}[{idx}] must be a dict")
    return value


def _require_creative_ids(items: list[dict[str, Any]], name: str) -> None:
    """Every per-creative item must carry a non-empty ``creative_id``."""
    for idx, item in enumerate(items):
        cid = item.get("creative_id")
        if not isinstance(cid, str) or not cid.strip():
            raise PipelineOperatorError(
                f"{name}[{idx}].creative_id must be a non-empty string"
            )


#: Per-item QA keys the worker's ``QAItem`` model reads alongside the
#: ``vision_candidates`` (``check_id``/``score``/``label``/``note`` candidate
#: fields plus extras pass through to ``VisionCandidate``, which is
#: ``extra="allow"``).
_QA_ITEM_PASSTHROUGH_KEYS: frozenset[str] = frozenset(
    {"surface", "vertical", "ratio", "image_b64", "overlay_region"}
)
#: Per-item compliance keys the worker's ``ComplianceItem`` model reads
#: alongside the ``llm_candidates`` (``rule_id``/``label`` required, plus
#: ``confidence``/``evidence_span``/``version``/... passing through to
#: ``LLMCandidate``, which is ``extra="allow"``).
_COMPLIANCE_ITEM_PASSTHROUGH_KEYS: frozenset[str] = frozenset(
    {"copy_variant_id", "surface", "vertical"}
)


def pipeline_operator_qa_result(
    *, pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Submit per-creative QA vision CANDIDATES for the worker to ADJUDICATE.

    POSTs the worker ``QARunInput`` shape — ``{pipeline_id, items:[{creative_id,
    ratio, vision_candidates:[{check_id, score, label, note, ...}],
    surface?, vertical?, image_b64?, overlay_region?}, ...]}`` — to
    ``/work/pipeline/tools/qa_run`` (the qa_compliance route module). ONE array
    call for the whole batch, never per creative.

    The operator (its qa specialist) submits CANDIDATES only: per-check vision
    observations, NOT a verdict the worker trusts. The worker fetches the image
    bytes, runs its own deterministic Pillow backstops
    (resolution/legibility/...), adjudicates the vision candidates via
    ``qa_engine.evaluate``, writes ``qa_result``, and rolls
    ``creative_stage_state(creative_qa)``. The verdict is always the worker's;
    a confident operator ``pass`` candidate can never clear a deterministic
    fail. The specialist's ``verdict`` / ``scores`` / ``defects`` /
    ``remediation`` keys (the old shape) are dropped here — they are NOT part of
    the candidate contract and the worker would 422 on them; the per-check
    observations belong in ``vision_candidates`` and the verdict is the worker's
    to write.

    Each result item is normalized to the worker ``QAItem`` shape: the per-check
    observations are mapped into ``vision_candidates`` (when not already supplied
    in that key) and the QA passthrough keys (``ratio``/``image_b64``/...) are
    forwarded. ``ratio`` defaults to ``"1x1"`` (the worker default) when absent.
    """
    pid = _require_pipeline_id(pipeline_id)
    raw = _require_list(results, "results")
    _require_creative_ids(raw, "results")
    items = [_to_qa_item(item) for item in raw]
    return _request(
        "POST",
        f"{_TOOLS_PREFIX}/qa_run",
        json_body={"pipeline_id": pid, "items": items},
    )


def _to_qa_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one operator QA result onto the worker ``QAItem`` shape.

    The worker's QA gate is CANDIDATE-only: it reads ``vision_candidates`` and
    writes the verdict itself. We forward ``creative_id`` + the QA passthrough
    keys and carry the per-check observations in ``vision_candidates`` (honoring
    an already-shaped ``vision_candidates`` if the specialist supplied one).
    """
    out: dict[str, Any] = {"creative_id": str(item["creative_id"]).strip()}
    for key in _QA_ITEM_PASSTHROUGH_KEYS:
        if item.get(key) is not None:
            out[key] = item[key]
    out.setdefault("ratio", "1x1")
    candidates = item.get("vision_candidates")
    if isinstance(candidates, list):
        out["vision_candidates"] = [
            _to_vision_candidate(c) for c in candidates if isinstance(c, dict)
        ]
    else:
        out["vision_candidates"] = []
    return out


def _to_vision_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """Keep the worker ``VisionCandidate`` fields (+ extras), drop nulls.

    ``check_id`` is required by the worker model; the rest are optional
    observations. Extra keys pass through (the model is ``extra="allow"``).
    """
    return {k: v for k, v in cand.items() if v is not None}


def pipeline_operator_compliance_result(
    *, pipeline_id: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Submit per-creative compliance CANDIDATE findings (the worker adjudicates).

    POSTs the worker ``ComplianceRunInput`` shape — ``{pipeline_id,
    items:[{creative_id, copy_variant_id?, surface, vertical?,
    llm_candidates:[{rule_id, label, confidence, evidence_span, version?,
    ...}]}, ...]}`` — to ``/work/pipeline/tools/compliance_run`` (the
    qa_compliance route module). ONE array call for the whole batch.

    HARD GATE: the operator has NO pass-writing tool — these are CANDIDATES
    only; the **worker** adjudicates deterministic + LLM findings and writes the
    verdict, escalating ``uncertain``/low-confidence to the manager queue rather
    than auto-passing. A ``failed`` unit leaves ``failed`` only via an audited
    manager override.

    Each input item is normalized to the worker ``ComplianceItem`` shape: the
    operator's per-creative ``findings`` (the specialist's candidate findings)
    are mapped into ``llm_candidates``, and ``copy_variant_id`` / ``surface`` /
    ``vertical`` are forwarded so the worker can build the engine context. The
    candidate finding fields (``rule_id``, ``label``, ``confidence``,
    ``evidence_span``, plus extras like ``version`` / ``required_edit`` /
    ``citation_url``) ride along to the worker's ``LLMCandidate`` model.
    """
    pid = _require_pipeline_id(pipeline_id)
    raw = _require_list(candidates, "candidates")
    _require_creative_ids(raw, "candidates")
    items = [_to_compliance_item(item) for item in raw]
    return _request(
        "POST",
        f"{_TOOLS_PREFIX}/compliance_run",
        json_body={"pipeline_id": pid, "items": items},
    )


def _to_compliance_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one operator compliance candidate onto the worker ``ComplianceItem``.

    The operator's per-creative ``findings`` become the worker's
    ``llm_candidates`` (the candidate-only payload the engine adjudicates). We
    forward ``creative_id`` + the compliance passthrough keys and carry the
    candidate findings under ``llm_candidates`` (honoring an already-shaped
    ``llm_candidates`` if the specialist supplied one).
    """
    out: dict[str, Any] = {"creative_id": str(item["creative_id"]).strip()}
    for key in _COMPLIANCE_ITEM_PASSTHROUGH_KEYS:
        if item.get(key) is not None:
            out[key] = item[key]
    findings = item.get("llm_candidates")
    if not isinstance(findings, list):
        findings = item.get("findings")
    if isinstance(findings, list):
        out["llm_candidates"] = [
            _to_llm_candidate(c) for c in findings if isinstance(c, dict)
        ]
    else:
        out["llm_candidates"] = []
    return out


def _to_llm_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """Keep the worker ``LLMCandidate`` fields (+ extras), drop nulls.

    ``rule_id`` + ``label`` are required by the worker model; the rest
    (``confidence``, ``evidence_span``) are optional, and extra keys
    (``version``, ``required_edit``, ``citation_url``, ``bbox``) pass through
    because the model is ``extra="allow"``.
    """
    return {k: v for k, v in cand.items() if v is not None}


def pipeline_operator_copy(
    *, pipeline_id: str, variants: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist authored copy variants (>=1 per creative) — the worker writes them.

    POSTs ``{pipeline_id, variants:[{creative_id, platform, variant_index,
    pattern, headline, primary_text, description, cta, validation}, ...]}`` to
    ``/work/pipeline/tools/copy``. The worker upserts ``copy_variants``
    (idempotent on ``(creative_id, platform, variant_index)``) at
    ``status='draft'`` and arms the per-creative copy gate to ``in_progress``;
    the manager approves at the copy stage gate. Approved copy edits re-arm that
    creative's compliance unit (two-pass). ONE array call for the whole batch.
    """
    pid = _require_pipeline_id(pipeline_id)
    items = _require_list(variants, "variants")
    _require_creative_ids(items, "variants")
    return _request(
        "POST",
        f"{_TOOLS_PREFIX}/copy",
        json_body={"pipeline_id": pid, "variants": items},
    )


def pipeline_operator_spec_result(
    *, pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist per-placement spec checks + derived crops — the worker writes them.

    POSTs ``{pipeline_id, results:[{creative_id, platform, placement, ratio,
    status, checks, derived_path_supabase?, derived_path_drive?}, ...]}`` to
    ``/work/pipeline/tools/spec_result``. The worker upserts ``spec_check``
    (idempotent on ``(creative_id, platform, placement)``) and rolls the
    spec_validation gate to the worst placement status (a failing placement
    holds the gate for the manager). ONE array call for the whole batch.
    """
    pid = _require_pipeline_id(pipeline_id)
    items = _require_list(results, "results")
    _require_creative_ids(items, "results")
    return _request(
        "POST",
        f"{_TOOLS_PREFIX}/spec_result",
        json_body={"pipeline_id": pid, "results": items},
    )


def pipeline_operator_finalize_result(
    *, pipeline_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record naming + Drive folder + verify report onto each creative.

    POSTs ``{pipeline_id, results:[{creative_id, asset_name, drive_folder_id?,
    file_path_drive?, verified}, ...]}`` to
    ``/work/pipeline/tools/finalize_result``. The worker writes the ``creatives``
    finalize columns and resumes idempotently (a creative already
    ``finalize_verified`` is skipped). ONE array call for the whole batch.
    """
    pid = _require_pipeline_id(pipeline_id)
    items = _require_list(results, "results")
    _require_creative_ids(items, "results")
    return _request(
        "POST",
        f"{_TOOLS_PREFIX}/finalize_result",
        json_body={"pipeline_id": pid, "results": items},
    )


def pipeline_operator_monitor_result(
    *,
    pipeline_id: str,
    results: list[dict[str, Any]],
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    """Persist monitor KPIs + kill/watch/keep verdicts (GHL is lead truth).

    POSTs ``{pipeline_id, client_id?, results:[{campaign_id, ad_entity_id?,
    window_days, spend, ghl_leads, ctr, freq, verdict, verdict_reason, ...},
    ...]}`` to ``/work/pipeline/tools/monitor_result``. The worker writes
    ``campaign_perf_image`` rows computing ``cpl_real = spend / ghl_leads``
    (never Meta leads) and resumes idempotently on the daily-unique key. The
    verdicts are recommendations; the manager approves kill/scale at the gate.
    ONE array call for the whole batch.
    """
    pid = _require_pipeline_id(pipeline_id)
    items = _require_list(results, "results")
    for idx, item in enumerate(items):
        camp = item.get("campaign_id")
        if not isinstance(camp, str) or not camp.strip():
            raise PipelineOperatorError(
                f"results[{idx}].campaign_id must be a non-empty string"
            )
    body: dict[str, Any] = {"pipeline_id": pid, "results": items}
    if client_id is not None:
        body["client_id"] = _require_client_id(client_id)
    return _request("POST", f"{_TOOLS_PREFIX}/monitor_result", json_body=body)


#: The signal statuses the worker accepts (operator narration verbs + the DB
#: lifecycle values). Mirrors ``operator_stage_tools.SignalInput.status``.
SIGNAL_STATUSES: frozenset[str] = frozenset(
    {
        "dispatched",
        "running",
        "completed",
        "failed",
        "timed_out",
        "stale",
        "waiting",
        "partial",
        "error",
    }
)


def pipeline_operator_signal(
    *,
    pipeline_id: str,
    dispatch_id: str,
    status: str,
    stage: Optional[str] = None,
    expected_status: Optional[str] = None,
    exec_id: Optional[str] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Signal dispatch completion / health to the workflow (ALWAYS call last).

    POSTs to ``/work/pipeline/tools/signal`` to record the signal on the
    dispatch's ``operator_dispatch`` work_item so the workflow knows the dispatch
    landed and the watchdog does not re-dispatch a healthy stage. End EVERY
    dispatch with this. ``status`` is one of :data:`SIGNAL_STATUSES`:
    ``stale``→done (a no-op dispatch), ``waiting``/``partial``→still running,
    ``error``→failed. Idempotent on ``(pipeline_id, dispatch_id)``.
    """
    pid = _require_pipeline_id(pipeline_id)
    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        raise PipelineOperatorError("dispatch_id must be a non-empty string")
    if status not in SIGNAL_STATUSES:
        raise PipelineOperatorError(
            f"status must be one of {sorted(SIGNAL_STATUSES)}, got {status!r}"
        )
    body: dict[str, Any] = {
        "pipeline_id": pid,
        "dispatch_id": dispatch_id.strip(),
        "status": status,
    }
    if stage is not None:
        body["stage"] = stage
    if expected_status is not None:
        body["expected_status"] = expected_status
    if exec_id is not None:
        body["exec_id"] = exec_id
    if summary is not None:
        body["summary"] = summary
    if error is not None:
        body["error"] = error
    return _request("POST", f"{_TOOLS_PREFIX}/signal", json_body=body)


# ---------------------------------------------------------------------------
# Conventional aliases (contract naming). The gating-canonical names are the
# pipeline_operator_* functions above; these are thin readability aliases.
# ---------------------------------------------------------------------------

get_pipeline = pipeline_operator_read
get_client = pipeline_operator_client_read
post_brief = pipeline_operator_brief
post_render = pipeline_operator_render


__all__ = [
    "ENV_WORKER_BASE_URL",
    "ENV_WORKER_SHARED_SECRET",
    "ENV_RENDER_BACKEND",
    "BACKEND_OPENAI_CODEX",
    "BACKEND_KIE",
    "RENDER_BACKENDS",
    "DEFAULT_RENDER_BACKEND",
    "FREE_MODEL",
    "FINALS_MODELS",
    "DEFAULT_FINALS_LABEL",
    "PipelineOperatorError",
    "RENDER_KINDS",
    "SIGNAL_STATUSES",
    "get_client",
    "get_pipeline",
    "pipeline_operator_brief",
    "pipeline_operator_client_read",
    "pipeline_operator_compliance_result",
    "pipeline_operator_copy",
    "pipeline_operator_finalize_result",
    "pipeline_operator_monitor_result",
    "pipeline_operator_qa_result",
    "pipeline_operator_read",
    "pipeline_operator_render",
    "pipeline_operator_signal",
    "pipeline_operator_spec_result",
    "pipeline_operator_store_creative",
    "post_brief",
    "post_render",
]
