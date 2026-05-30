"""Periodic background scheduler for the worker's cron cores (#354).

Each job runs in its own supervised asyncio loop (``asyncio.create_task`` +
``asyncio.sleep``); the live set is:

  1. **observability / ops-alert delivery** -- roll the metrics snapshot and
     page the Slack ops channel on an SLO breach
     (:func:`run_observability_once`);
  2. the **GHL daily reconciliation** -- real CPL = Meta spend / GHL leads
     (:func:`routes.integrations.reconcile_pipeline`);
  3. the **kie video render reconciliation** -- poll-and-close renders the
     callback never resolved (:func:`run_kie_reconcile_once`);
  4. the **unified work_item watchdog** -- rotate stale-claim work_item rows and
     flip stale consumers (:func:`run_work_item_watchdog_once`);
  5. the **outbox drain** -- dispatch the outbox-kind work_item rows
     (:func:`services.outbox_consumer.run_outbox_drain_once`);
  6. the **worker-stage drain** -- run the deterministic ideation/generation/
     monitor work_item rows
     (:func:`services.worker_stage_consumer.run_worker_stage_drain_once`).

It follows the pattern already in the worker (the ``asyncio`` loops in
:mod:`services.hermes_approval`) rather than adding an APScheduler dependency:
the worker has no scheduler dep today and the team prefers staying
dependency-light. Every tick is wrapped so a job failure is logged and the loop
sleeps and retries -- a single bad tick (or a transient Supabase blip) NEVER
crashes the worker.

Lifecycle: :func:`start_scheduler` is invoked from the FastAPI lifespan on
startup and returns a :class:`Scheduler` whose :meth:`~Scheduler.stop`
cancels every loop and awaits clean exit on shutdown. The whole thing is a
no-op (logs and returns an empty scheduler) when Supabase isn't configured,
exactly like :func:`services.compliance_rules_seed.seed_compliance_rules_safe`
-- so local boots / tests that don't set Supabase env still start cleanly.

All intervals come from :class:`config.Settings` (env-backed) with conservative
defaults; see the ``scheduler_*`` fields there for the documented env vars.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

import structlog

from ..config import Settings, get_settings
from . import observability, work_queue
from .outbox_consumer import run_outbox_drain_once
from .worker_stage_consumer import run_worker_stage_drain_once
from .work_item_watchdog import (
    StaleConsumer,
    StuckWorkItem,
    compute_backoff_seconds,
    find_stale_consumers,
    find_stuck_work_items,
)


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Reconciliation targets -- the one seam that still needs a built data source.
# ---------------------------------------------------------------------------
#
# reconcile_pipeline() needs, per pipeline: a GHL location_id, a campaign_ref
# (attribution string), an ad_entity_id, and a window. The architecture says
# these come from a `client_integrations` table (services/ghl.py docstring),
# but that table is not built yet -- it's referenced only in docstrings. Until
# it lands, this returns [] and the GHL job is a logged no-op. See the
# load_reconciliation_targets() docstring (#354).


class ReconcileTarget:
    """One pipeline to reconcile in the daily GHL pass.

    Plain container (not a pydantic model) so the eventual ``client_integrations``
    read can build these from raw rows with zero ceremony.
    """

    __slots__ = ("pipeline_id", "location_id", "campaign_ref", "ad_entity_id")

    def __init__(
        self,
        *,
        pipeline_id: str,
        location_id: str,
        campaign_ref: str,
        ad_entity_id: str | None = None,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.location_id = location_id
        self.campaign_ref = campaign_ref
        self.ad_entity_id = ad_entity_id


def load_reconciliation_targets() -> list[ReconcileTarget]:
    """Return the pipelines the daily GHL reconciliation should process.

    DEFERRED DATA SOURCE (#354): the per-client (pipeline -> GHL location_id +
    campaign_ref + ad_entity_id) mapping lives in the not-yet-built
    ``client_integrations`` table. Until that schema lands this returns ``[]`` and
    the reconcile job is a safe no-op (it logs "no targets" and sleeps). When the
    table exists, build the list here, e.g.::

        from ..supabase_client import get_supabase_admin
        sb = get_supabase_admin()
        resp = (
            sb.table("client_integrations")
            .select("pipeline_id, ghl_location_id, ghl_campaign_ref, ad_entity_id")
            .eq("active", True)
            .execute()
        )
        rows = resp.data if (resp is not None and isinstance(resp.data, list)) else []
        return [
            ReconcileTarget(
                pipeline_id=r["pipeline_id"],
                location_id=r["ghl_location_id"],
                campaign_ref=r["ghl_campaign_ref"],
                ad_entity_id=r.get("ad_entity_id"),
            )
            for r in rows
            if isinstance(r, dict) and r.get("pipeline_id")
        ]
    """
    return []


# Silent-failure PR-4: the legacy per-domain `run_dispatch_watchdog_once`
# and `run_outbox_relay_once` jobs were retired in PR-3 (the unified
# `run_work_item_watchdog_once` covers BOTH responsibilities -- stuck operator
# dispatches AND outbox-style retries ride the same kind enum + the same
# heartbeat-stale rotation). PR-4 deletes the underlying functions plus the
# legacy modules they pulled in (operator_bridge, operator_dispatch_watchdog,
# outbox_relay). The legacy tables they read from were renamed `_legacy_*`
# by migration 0051.


# ---------------------------------------------------------------------------
# Ops alert delivery (E5.6 / #526)
# ---------------------------------------------------------------------------
#
# The watchdog ticks already CLASSIFY problems (stuck dispatches, a growing /
# over-threshold outbox dead-letter pile, an open breaker, cost over its cap)
# and log them; the gap E5.6 closes is DELIVERY -- paging a human. When a tick
# detects a problem it posts a Slack alert to a SEPARATE ops channel
# (``SLACK_OPS_CHANNEL_ID``) via the existing Slack sender
# (:func:`services.approval_notifications.post_slack_message`). Two properties
# keep it production-safe:
#
#   * Best-effort: the whole path is wrapped so it NEVER raises and never blocks
#     the supervised loop -- a Slack outage degrades to a logged warning.
#   * De-duped / throttled: a persistent bad state pages on TRANSITION into bad
#     (the first tick that sees it), then is suppressed for
#     ``ops_alert_throttle_s`` so we don't spam the channel every tick. The
#     throttle re-arms when the condition clears (a return to healthy), so a
#     flap pages again only after it actually recovered + re-broke.


@dataclass(frozen=True)
class AlertCondition:
    """One operational problem a watchdog tick detected.

    ``kind`` is a stable throttle key (one entry per distinct problem class);
    ``severity`` is ``"critical"`` or ``"warning"``; ``summary`` is the one-line
    headline; ``detail`` is the human-readable body posted to Slack.
    """

    kind: str
    severity: str
    summary: str
    detail: str


def evaluate_alert_conditions(
    settings: Settings,
    *,
    metrics: Mapping[str, Any],
) -> list[AlertCondition]:
    """Classify the current metrics snapshot into firing alerts (pure).

    The input is an already-fetched
    :func:`services.observability.metrics_snapshot`, so this is deterministic
    and unit-tested with no I/O. Each SLO breach maps to one
    :class:`AlertCondition` with a stable ``kind`` (its throttle key):

      * ``outbox_dead_letter`` -- the dead-letter pile (status dead+failed) is
        at/above ``ops_alert_outbox_dead_letter_threshold``.
      * ``outbox_backlog`` -- the live outbox depth (pending+inflight) is
        at/above ``ops_alert_outbox_depth_threshold``.
      * ``breaker_open`` -- any circuit breaker in the metrics snapshot is open.
      * ``cost_over_cap`` -- cost exceeded its configured cap (only when a cap
        is set; the snapshot reports ``over_cap``).

    Stale-claim rotation for stuck operator dispatches is owned by the unified
    work_item watchdog now, so the legacy stuck-dispatch alert branch was
    removed -- a wedged dispatch surfaces as a dead-letter / backlog breach here
    and a ``work_item_watchdog_dead_lettered`` log line from the watchdog.
    """
    conditions: list[AlertCondition] = []

    outbox = metrics.get("outbox") if isinstance(metrics, Mapping) else None
    outbox = outbox if isinstance(outbox, Mapping) else {}

    # 1. Outbox dead-letter pile (durable external-write failures).
    dead = int(outbox.get("dead", 0) or 0) + int(outbox.get("failed", 0) or 0)
    if dead >= settings.ops_alert_outbox_dead_letter_threshold:
        conditions.append(
            AlertCondition(
                kind="outbox_dead_letter",
                severity="critical",
                summary=f"{dead} outbox dead-letter row(s)",
                detail=(
                    f"The integration outbox has {dead} dead/failed row(s) "
                    f"(SLO threshold {settings.ops_alert_outbox_dead_letter_threshold}). "
                    "External side effects (Meta activation / Drive finalize) are "
                    "not being delivered -- inspect integration_outbox."
                ),
            )
        )

    # 2. Outbox backlog depth (the relay is falling behind).
    depth = int(outbox.get("depth", 0) or 0)
    if depth >= settings.ops_alert_outbox_depth_threshold:
        conditions.append(
            AlertCondition(
                kind="outbox_backlog",
                severity="warning",
                summary=f"outbox depth {depth}",
                detail=(
                    f"The integration outbox depth is {depth} "
                    f"(SLO threshold {settings.ops_alert_outbox_depth_threshold}). "
                    "The relay is draining slower than work arrives."
                ),
            )
        )

    # 3. Open circuit breaker(s). The metrics snapshot carries a per-host map;
    # an "open" state means a downstream connector is shedding load. (The
    # snapshot's breaker map is empty until the cron-held connector singleton
    # feeds it -- see docs/observability.md; this fires the moment it does.)
    breakers = metrics.get("breakers") if isinstance(metrics, Mapping) else None
    if isinstance(breakers, Mapping):
        open_hosts = [h for h, state in breakers.items() if str(state).lower() == "open"]
        if open_hosts:
            conditions.append(
                AlertCondition(
                    kind="breaker_open",
                    severity="critical",
                    summary=f"{len(open_hosts)} circuit breaker(s) open",
                    detail=(
                        "Circuit breaker open for: "
                        + ", ".join(sorted(open_hosts))
                        + ". A downstream connector is failing."
                    ),
                )
            )

    # 4. Cost over cap (only meaningful when a cap is configured + fed).
    cost = metrics.get("cost") if isinstance(metrics, Mapping) else None
    if isinstance(cost, Mapping) and cost.get("over_cap"):
        conditions.append(
            AlertCondition(
                kind="cost_over_cap",
                severity="critical",
                summary="cost over cap",
                detail=(
                    f"Spend ${cost.get('total_usd')} exceeded the cap "
                    f"${cost.get('cap_usd')}."
                ),
            )
        )

    return conditions


# Every alert kind :func:`evaluate_alert_conditions` can emit -- the throttle
# walks this set each tick to re-arm kinds that stopped firing.
_ALL_ALERT_KINDS: frozenset[str] = frozenset(
    {
        "outbox_dead_letter",
        "outbox_backlog",
        "breaker_open",
        "cost_over_cap",
    }
)


class _AlertThrottle:
    """Per-kind transition + rate-limit gate for ops alerts.

    Holds the monotonic timestamp each alert kind last fired. :meth:`should_send`
    returns True only when a kind is firing AND either it was healthy on the
    previous tick (a transition into bad) OR the throttle window has lapsed since
    it last paged -- so a persistent bad state pages on transition, not every
    tick. :meth:`mark_healthy` clears a kind so its next breach re-arms an
    immediate page (a recover-then-rebreak flaps a fresh alert).
    """

    def __init__(self, throttle_s: float) -> None:
        self._throttle_s = throttle_s
        self._last_sent: dict[str, float] = {}

    def should_send(self, kind: str, *, now: float | None = None) -> bool:
        clock = time.monotonic() if now is None else now
        last = self._last_sent.get(kind)
        if last is not None and (clock - last) < self._throttle_s:
            return False
        self._last_sent[kind] = clock
        return True

    def mark_healthy(self, kind: str) -> None:
        self._last_sent.pop(kind, None)


# Process-wide throttle. Built lazily on first use from the live settings so the
# window is configurable; the scheduler is a singleton per process so one shared
# instance is correct (every observability tick consults the same gate).
_alert_throttle: _AlertThrottle | None = None


def _get_alert_throttle(settings: Settings) -> _AlertThrottle:
    global _alert_throttle
    if _alert_throttle is None:
        _alert_throttle = _AlertThrottle(settings.ops_alert_throttle_s)
    return _alert_throttle


def reset_alert_throttle() -> None:
    """Drop the process-wide alert throttle (test hook + clean re-arm)."""
    global _alert_throttle
    _alert_throttle = None


def _build_ops_alert_blocks(conditions: list[AlertCondition]) -> list[dict[str, Any]]:
    """Compose the Slack Block Kit body for a batch of firing alert conditions."""
    has_critical = any(c.severity == "critical" for c in conditions)
    icon = "\U0001f6a8" if has_critical else "⚠️"  # 🚨 / ⚠️
    header = f"{icon} VoxHorizon ops alert ({len(conditions)})"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150], "emoji": True}}
    ]
    for cond in conditions:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*[{cond.severity}] {cond.summary}*\n{cond.detail}",
                },
            }
        )
    return blocks


async def deliver_ops_alerts(
    settings: Settings, conditions: list[AlertCondition]
) -> int:
    """Deliver firing ops-alert conditions to the Slack ops channel (best-effort).

    Applies the per-kind throttle (only paging on transition into bad / after the
    window lapses), then posts ONE batched Slack message for the kinds that pass
    the gate. Kinds NOT currently firing are marked healthy so their next breach
    re-arms an immediate page. Returns the number of conditions actually paged.

    Never raises: a missing ops channel, a Slack outage, or any unexpected error
    degrades to a logged warning so the supervised loop is never disturbed.
    """
    throttle = _get_alert_throttle(settings)

    # Re-arm any kind that is no longer firing (transition back to healthy).
    firing_kinds = {c.kind for c in conditions}
    for kind in _ALL_ALERT_KINDS:
        if kind not in firing_kinds:
            throttle.mark_healthy(kind)

    if not conditions:
        return 0

    to_send = [c for c in conditions if throttle.should_send(c.kind)]
    if not to_send:
        log.info("ops_alert_throttled", kinds=sorted(firing_kinds))
        return 0

    token = settings.slack_bot_token
    channel = settings.slack_ops_channel_id
    if not token or not channel:
        log.warning(
            "ops_alert_skipped_no_channel",
            has_token=bool(token),
            has_channel=bool(channel),
            kinds=[c.kind for c in to_send],
        )
        return 0

    summary = "; ".join(f"[{c.severity}] {c.summary}" for c in to_send)
    text = f"VoxHorizon ops alert: {summary}"
    try:
        from .approval_notifications import post_slack_message

        ok = await post_slack_message(
            token=token,
            channel=channel,
            text=text,
            blocks=_build_ops_alert_blocks(to_send),
            context={"alert_kinds": [c.kind for c in to_send]},
        )
    except Exception as exc:  # noqa: BLE001 -- alert delivery never sinks the loop
        log.warning("ops_alert_delivery_failed", error=str(exc))
        return 0

    if ok:
        log.info("ops_alert_sent", kinds=[c.kind for c in to_send])
    return len(to_send)


# ---------------------------------------------------------------------------
# Job: observability ops-alert delivery
# ---------------------------------------------------------------------------


async def run_observability_once(settings: Settings) -> dict[str, int]:
    """One observability pass. Returns the count of alert conditions delivered.

    Silent-failure PR-4: the legacy per-domain readers
    (``_all_dispatch_rows``/``_all_outbox_rows``) were removed -- the
    operator_dispatches + integration_outbox tables were renamed `_legacy_*`
    in migration 0051 and nothing writes to them anymore. The unified
    `run_work_item_watchdog_once` covers the equivalent surface against the
    `work_item` queue. This tick rolls the metrics snapshot and DELIVERS the
    alert conditions it reports (outbox dead-letter / backlog, open breaker,
    cost over cap) to the Slack ops channel; the structured logs the legacy
    watchdogs emitted are gone with their data sources.
    """
    delivered = 0

    # E5.6: turn the metrics snapshot into ops alerts + DELIVER them
    # (best-effort). The snapshot supplies the outbox dead-letter/depth, breaker
    # map, and cost-vs-cap. The whole step is wrapped so an alerting failure
    # never disturbs the loop.
    try:
        from ..supabase_client import get_supabase_admin

        sb = get_supabase_admin()
        snapshot = observability.metrics_snapshot(sb)
        conditions = evaluate_alert_conditions(settings, metrics=snapshot)
        delivered = await deliver_ops_alerts(settings, conditions)
    except Exception as exc:  # noqa: BLE001 -- alert delivery never sinks the tick
        log.warning("ops_alert_tick_failed", error=str(exc))

    log.info("observability_pass_done", alerts_delivered=delivered)
    return {"alerts_delivered": delivered}


# ---------------------------------------------------------------------------
# Job: GHL daily reconciliation
# ---------------------------------------------------------------------------


async def run_reconciliation_once(settings: Settings) -> int:
    """One daily reconciliation pass over the active reconcile targets.

    For each target (see :func:`load_reconciliation_targets`): build the look-back
    window, count GHL leads, sum Meta spend, compute real CPL, and write a
    ``campaign_perf_image`` row -- all via the existing pure core
    :func:`routes.integrations.reconcile_pipeline`. One GHL client is shared
    across the pass and closed at the end. A per-target failure is logged and
    skipped so one bad client never aborts the whole nightly run.

    Returns the number of targets reconciled. When there are no targets (the
    ``client_integrations`` source isn't built yet) this is a logged no-op.
    """
    targets = load_reconciliation_targets()
    if not targets:
        log.info("reconciliation_no_targets")
        return 0

    # Imported lazily so the scheduler module never forces these heavier imports
    # at import time (and so tests can patch them on the route module).
    from ..routes.integrations import reconcile_pipeline
    from .ghl import GhlClient

    now = datetime.now(timezone.utc)
    window = (now - timedelta(days=settings.scheduler_reconcile_window_days), now)

    reconciled = 0
    client = GhlClient()  # FAKE_GHL-aware; needs no key under the fake flag
    try:
        for target in targets:
            try:
                result = await reconcile_pipeline(
                    pipeline_id=target.pipeline_id,
                    location_id=target.location_id,
                    campaign_ref=target.campaign_ref,
                    window=window,
                    ghl_client=client,
                    ad_entity_id=target.ad_entity_id,
                )
                reconciled += 1
                log.info(
                    "reconciliation_target_done",
                    pipeline_id=target.pipeline_id,
                    leads=result.ghl_leads,
                    meta_spend_usd=result.meta_spend_usd,
                    real_cpl=result.real_cpl,
                    perf_row_written=result.perf_row_written,
                )
            except Exception as exc:  # noqa: BLE001 -- one client never aborts the run
                log.warning(
                    "reconciliation_target_failed",
                    pipeline_id=target.pipeline_id,
                    error=str(exc),
                )
    finally:
        await client.aclose()

    log.info("reconciliation_pass_done", targets=len(targets), reconciled=reconciled)
    return reconciled


# ---------------------------------------------------------------------------
# Job: kie video render reconciliation (E5.2 / #514)
# ---------------------------------------------------------------------------
#
# THE BUG this job backstops: the live broll-search path submits a kie video
# render and BLOCKS on a 10-minute poll. A restart mid-poll abandoned the render
# (kie still produced + billed the clip, nothing recorded it), and even with the
# callback receiver a dropped/never-delivered callback would still lose it. This
# sweep is the durable safety net the watchdog CANNOT replace: the watchdog only
# rotates stale CLAIMS, but a kie render can FINISH remotely while no callback
# ever arrives -- only an explicit poll of the kie API discovers that. The sweep
# reads the open ``work_item(kind='kie_video_render')`` rows (silent-failure
# PR-6 -- the durable record is the work_item now, not ``video_render_tasks``),
# polls kie for each via a single non-blocking probe, and closes the work_item
# -- exactly what the callback would have done.
#
# FIX-C: the open-row read MUST include ``queued`` rows. The live submit path
# (``kie_video._submit`` -> ``persist_submitted_render_work_item``) persists the
# render's work_item ``queued`` and passes NO callback_url, and nothing claims
# the ``kie_video_render`` kind -- so the row sits ``queued`` forever. Reading
# only claimed/running left every queued (billed-but-unresolved) render in a
# blind spot: the callback was never armed, the watchdog only rotates
# claimed/running rows, and the billed clip was silently lost. Including
# ``queued`` is what closes the blind spot PR-6 claimed to. The sweep is
# idempotent (the callback + the sweep both resolve a task by its idempotency
# key, and the close is scoped to non-terminal rows, so the loser writes 0 rows)
# and BOUNDED per pass (``scheduler_kie_reconcile_max_per_pass``), mirroring the
# dispatch watchdog's redispatch cap so a backlog can't fan out an unbounded
# burst. A queued row with no submitted task_id yet is skipped, never failed.


def _open_render_tasks(sb: Any, *, limit: int) -> list[dict[str, Any]]:
    """Read up to ``limit`` open ``kie_video_render`` work_items (oldest first).

    Silent-failure PR-6: the durable render record is the
    ``work_item(kind='kie_video_render')``. The reconciliation sweep reads the
    open (non-terminal) rows -- those are the renders submitted-and-awaiting-
    resolution that a poll of kie can close. The ``task_id`` / ``is_veo`` /
    ``theme`` / ``creative_id`` live in the work_item ``payload``.

    FIX-C: the read MUST include ``queued`` rows, not just ``claimed`` /
    ``running``. The live broll/video submit path persists the render's
    work_item in ``status='queued'`` (``persist_submitted_render_work_item``,
    called from ``kie_video._submit`` AFTER kie returns the ``taskId``) but
    passes NO ``callback_url`` and NOTHING claims the ``kie_video_render`` kind
    -- so the row never advances past ``queued``. Reading only claimed/running
    made every queued (billed-but-unresolved) render INVISIBLE to the sweep:
    the callback was never armed, the watchdog only rotates claimed/running
    rows, and the billed kie clip was silently lost with the row stuck
    ``queued`` forever. Including ``queued`` here is what lets the sweep poll
    kie for the submitted ``taskId`` and close the work_item. A queued row
    with no ``taskId`` yet in its payload is NEVER failed prematurely -- the
    sweep skips any row whose ``payload.task_id`` is missing/empty (see
    :func:`run_kie_reconcile_once`).
    """
    resp = (
        sb.table("work_item")
        .select(
            "id, kind, status, attempt, payload, idempotency_key, "
            "pipeline_id, creative_id, brief_id, claim_token"
        )
        .eq("kind", "kie_video_render")
        .in_("status", ["queued", "claimed", "running"])
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return resp.data if (resp is not None and isinstance(resp.data, list)) else []


def _render_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the kie render payload (task_id / is_veo / theme) off a work_item."""
    payload = row.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


async def run_kie_reconcile_once(settings: Settings) -> int:
    """One pass of the kie video render reconciliation. Returns rows resolved.

    Reads the open ``work_item(kind='kie_video_render')`` rows -- ``queued``,
    ``claimed``, or ``running`` (bounded by
    ``scheduler_kie_reconcile_max_per_pass``) -- polls kie once for each via
    :meth:`services.kie_video.KieVideoClient.poll_status`, and closes the
    outcome on the work_item: a success downloads + stores the clip and marks it
    ``completed``; a terminal kie failure marks it ``failed``; a still-pending
    render bumps its attempt + pushes ``next_attempt_at`` out via the watchdog's
    exponential backoff and is left for the next pass. A per-row failure is
    logged and skipped so one bad render never aborts the sweep.

    FIX-C: ``queued`` rows are recovered too. The live submit path enqueues the
    render's work_item ``queued`` with NO callback armed and NOTHING claiming
    the kind, so the row never leaves ``queued`` -- previously invisible to the
    sweep AND the watchdog, silently losing the billed clip. The sweep now polls
    kie for a queued row's submitted ``task_id`` (always present in the payload
    -- the producer writes it AFTER kie returns it) and closes the work_item via
    the same path. A row whose ``payload.task_id`` is missing/empty is SKIPPED
    (left ``queued``, never failed prematurely): only a row with a submitted
    task_id is polled.

    No double-resolve: the close path (``video_callback._mark_completed`` /
    ``_mark_work_item_failed``) is scoped to non-terminal rows, so if a callback
    is ever armed and both touch the same render, whichever writes the terminal
    status first wins and the loser's UPDATE matches 0 rows. A row the callback
    already resolved is terminal (not queued/claimed/running) and is no longer
    even read by :func:`_open_render_tasks`.
    """
    from ..routes import video_callback
    from ..supabase_client import get_supabase_admin  # lazy: never forces a client
    from .kie_video import (
        RENDER_FAILED,
        RENDER_SUCCESS,
        KieVideoClient,
    )

    sb = get_supabase_admin()
    rows = _open_render_tasks(sb, limit=settings.scheduler_kie_reconcile_max_per_pass)
    if not rows:
        log.info("kie_reconcile_no_open_renders")
        return 0

    client = KieVideoClient()
    resolved = 0
    for row in rows:
        payload = _render_payload(row)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        try:
            status = await client.poll_status(task_id, bool(payload.get("is_veo")))
        except Exception as exc:  # noqa: BLE001 -- one bad render never sinks the pass
            log.warning("kie_reconcile_poll_failed", task_id=task_id, error=str(exc))
            _bump_render_attempt(sb, row)
            continue

        if status.state == RENDER_SUCCESS:
            try:
                stored = await video_callback._store_render_result(
                    task_id=task_id, theme=payload.get("theme"), urls=status.urls
                )
            except Exception as exc:  # noqa: BLE001 -- store failure is non-terminal
                log.warning(
                    "kie_reconcile_store_failed", task_id=task_id, error=str(exc)
                )
                _bump_render_attempt(sb, row)
                continue
            video_callback._mark_completed(
                task_id,
                result_url=status.urls[0],
                clip_id=str(stored.get("clip_id") or "") or None,
            )
            resolved += 1
            log.info(
                "kie_reconcile_recorded",
                task_id=task_id,
                creative_id=row.get("creative_id"),
                clip_id=stored.get("clip_id"),
            )
        elif status.state == RENDER_FAILED:
            video_callback._mark_work_item_failed(
                task_id, error=status.error or "kie reported a render failure"
            )
            resolved += 1
            log.info("kie_reconcile_marked_failed", task_id=task_id)
        else:
            _bump_render_attempt(sb, row)

    log.info("kie_reconcile_pass_done", open_rows=len(rows), resolved=resolved)
    return resolved


def _bump_render_attempt(sb: Any, row: Mapping[str, Any]) -> None:
    """Push a still-pending render's ``next_attempt_at`` out via backoff.

    Silent-failure PR-6: a render kie has not resolved yet is left ``running``
    on the work_item; we only bump the attempt counter and delay the next poll
    (the watchdog's :func:`compute_backoff_seconds`, capped) so a long render
    isn't re-polled every pass. Best-effort -- bookkeeping never aborts the
    sweep.
    """
    work_item_id = row.get("id")
    if not work_item_id:
        return
    try:
        attempt = int(row.get("attempt") or 0)
        backoff = compute_backoff_seconds(attempt=attempt)
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=backoff)
        ).isoformat()
        sb.table("work_item").update(
            {"attempt": attempt + 1, "next_attempt_at": next_attempt_at}
        ).eq("id", work_item_id).execute()
    except Exception as exc:  # noqa: BLE001 -- bookkeeping never aborts the sweep
        log.warning(
            "kie_reconcile_attempt_bump_failed",
            work_item_id=work_item_id,
            error=str(exc),
        )


# Silent-failure PR-4: the legacy `run_outbox_relay_once` (and the
# `outbox_relay` module it delegated to) were deleted. The unified
# `run_work_item_watchdog_once` below owns the equivalent surface against the
# `work_item` queue, and the legacy `integration_outbox` table was renamed
# `_legacy_*` by migration 0051.


# ---------------------------------------------------------------------------
# Job: unified work_item watchdog (silent-failure PR-1)
# ---------------------------------------------------------------------------
#
# Reads claimed/running work_item rows whose heartbeat is stale and the
# consumer presence rows; rotates the claim_token (mark timed_out + parent-
# chained requeue with exponential backoff) and flips stale consumers to
# ``degraded`` / ``down``. Runs ALONGSIDE the legacy watchdogs in PR-1 -- no
# behavior change to in-flight pipelines.


def _open_held_work_items(sb: Any) -> list[dict[str, Any]]:
    """Read the held (``claimed`` / ``running``) ``work_item`` rows."""
    resp = (
        sb.table("work_item")
        .select(
            "id, kind, pipeline_id, creative_id, brief_id, status, attempt, "
            "claim_token, claimed_at, heartbeat_at, payload, idempotency_key, "
            "parent_work_item_id, created_by"
        )
        .in_("status", ["claimed", "running"])
        .execute()
    )
    return resp.data if (resp is not None and isinstance(resp.data, list)) else []


def _all_consumer_rows(sb: Any) -> list[dict[str, Any]]:
    """Read every ``work_item_consumers`` row (small table; full scan is fine)."""
    resp = sb.table("work_item_consumers").select(
        "id, kind, status, last_seen_at"
    ).execute()
    return resp.data if (resp is not None and isinstance(resp.data, list)) else []


def _rotate_stuck_work_item(
    sb: Any,
    item: StuckWorkItem,
    *,
    max_attempts: int,
    base_seconds: int,
    cap_seconds: int,
) -> str:
    """Rotate one stuck work_item; returns ``'requeued'`` or ``'dead_lettered'``.

    Two paths:
      * attempt < max_attempts -- mark the row ``timed_out`` (clears the
        claim) with ``error_kind='heartbeat_stale'``, then enqueue a NEW row
        with ``parent_work_item_id`` set to the timed-out row, ``attempt``
        carried forward, and ``next_attempt_at`` pushed out via exponential
        backoff. The retry chain is the audit trail of "this work tried N
        times and these are the diagnostics for each attempt".
      * attempt >= max_attempts -- mark the row ``failed`` with
        ``error_kind='max_attempts_exceeded'`` and leave it for the dead-letter
        view. No new row is enqueued.

    The token-scoped UPDATE returns 0 rows when the consumer raced us; that's
    fine -- it means the consumer heartbeated/completed between our SELECT and
    UPDATE and the row is no longer stuck.
    """
    now = datetime.now(timezone.utc).isoformat()
    if item.attempt < max_attempts:
        terminal_status = "timed_out"
        terminal_error_kind = "heartbeat_stale"
    else:
        terminal_status = "failed"
        terminal_error_kind = "max_attempts_exceeded"

    update_query = (
        sb.table("work_item")
        .update(
            {
                "status": terminal_status,
                "completed_at": now,
                "error_kind": terminal_error_kind,
                "error_detail": {
                    "idle_seconds": item.idle_seconds,
                    "rotated_at": now,
                    "attempt_at_rotation": item.attempt,
                },
                "claim_token": None,
                "claimed_by": None,
                "claimed_at": None,
            }
        )
        .eq("id", item.work_item_id)
    )
    if item.claim_token is not None:
        update_query = update_query.eq("claim_token", item.claim_token)
    update_query.execute()

    if terminal_status == "failed":
        log.warning(
            "work_item_watchdog_dead_lettered",
            work_item_id=item.work_item_id,
            kind=item.kind,
            attempt=item.attempt,
        )
        return "dead_lettered"

    backoff = compute_backoff_seconds(
        attempt=item.attempt,
        base_seconds=base_seconds,
        cap_seconds=cap_seconds,
    )
    next_attempt_at = (
        datetime.now(timezone.utc) + timedelta(seconds=backoff)
    ).isoformat()
    # The retry row is a fresh row with a fresh idempotency_key: a retry chain
    # under ONE idempotency_key would unique-conflict on the second hop. The
    # parent_work_item_id pointer is the audit chain.
    retry_idempotency_key = (
        f"{item.work_item_id}:retry:{item.attempt + 1}"
    )
    work_queue.enqueue_work_item(
        sb,
        kind=item.kind,
        pipeline_id=item.pipeline_id,
        creative_id=item.creative_id,
        brief_id=item.brief_id,
        payload=item.payload,
        idempotency_key=retry_idempotency_key,
        created_by="work_item_watchdog",
        parent_work_item_id=item.work_item_id,
        next_attempt_at=next_attempt_at,
    )
    log.info(
        "work_item_watchdog_requeued",
        work_item_id=item.work_item_id,
        kind=item.kind,
        next_attempt_in_s=backoff,
        attempt=item.attempt + 1,
    )
    return "requeued"


def _flip_stale_consumer(sb: Any, consumer: StaleConsumer) -> None:
    """Write a consumer status transition (degraded / down)."""
    sb.table("work_item_consumers").update(
        {"status": consumer.target_status}
    ).eq("id", consumer.consumer_id).execute()
    log.warning(
        "work_item_consumer_flipped",
        consumer_id=consumer.consumer_id,
        from_=consumer.current_status,
        to=consumer.target_status,
        idle_seconds=consumer.idle_seconds,
    )


async def run_work_item_watchdog_once(
    sb: Any | None = None,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One pass of the unified work_item watchdog. Returns counts per action.

    Returns ``{rotated, requeued, dead_lettered, consumers_flipped}``. The
    scheduler-side wrapper supplies ``settings`` via the live module reload;
    tests inject a clock + a FakeSupabase directly.

    A per-row failure is logged + skipped so one bad row never aborts a
    sweep, matching the legacy watchdogs' single-bad-row tolerance.
    """
    settings = settings or get_settings()
    if sb is None:
        from ..supabase_client import get_supabase_admin  # lazy; mirror peers
        sb = get_supabase_admin()
    held_rows = _open_held_work_items(sb)
    threshold = timedelta(seconds=settings.work_item_heartbeat_threshold_s)
    stuck = find_stuck_work_items(held_rows, threshold=threshold, now=now)
    consumer_rows = _all_consumer_rows(sb)
    consumer_interval = timedelta(seconds=settings.work_item_consumer_heartbeat_s)
    stale_consumers = find_stale_consumers(
        consumer_rows, heartbeat_interval=consumer_interval, now=now
    )

    counts = {
        "rotated": 0,
        "requeued": 0,
        "dead_lettered": 0,
        "consumers_flipped": 0,
    }

    for item in stuck[: settings.work_item_watchdog_max_per_pass]:
        try:
            outcome = _rotate_stuck_work_item(
                sb,
                item,
                max_attempts=settings.work_item_max_attempts,
                base_seconds=settings.work_item_backoff_base_s,
                cap_seconds=settings.work_item_backoff_cap_s,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad row never sinks the pass
            log.warning(
                "work_item_watchdog_rotation_failed",
                work_item_id=item.work_item_id,
                kind=item.kind,
                error=str(exc),
            )
            continue
        counts["rotated"] += 1
        if outcome == "requeued":
            counts["requeued"] += 1
        elif outcome == "dead_lettered":
            counts["dead_lettered"] += 1

    for consumer in stale_consumers:
        try:
            _flip_stale_consumer(sb, consumer)
            counts["consumers_flipped"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "work_item_consumer_flip_failed",
                consumer_id=consumer.consumer_id,
                error=str(exc),
            )

    log.info(
        "work_item_watchdog_pass_done",
        held_rows=len(held_rows),
        stuck=len(stuck),
        rotated=counts["rotated"],
        requeued=counts["requeued"],
        dead_lettered=counts["dead_lettered"],
        consumers=len(consumer_rows),
        consumers_flipped=counts["consumers_flipped"],
    )
    return counts


# ---------------------------------------------------------------------------
# Temp-dir reaper (post-incident disk hygiene)
# ---------------------------------------------------------------------------


async def run_temp_reaper_once(settings: Settings | None = None) -> dict[str, int]:
    """Delete stale ``vox-*`` temp entries left by yt-dlp/ffmpeg/probe runs.

    Several worker subprocess helpers ``mkdtemp(prefix="vox-...")`` under the
    system tmp dir and intentionally leave the artifacts for the caller. Over a
    long uptime these accumulate and can slowly fill the host disk. This sweep
    removes any ``vox-*`` entry whose mtime is older than
    ``scheduler_temp_reaper_max_age_s`` (recent ones -- possibly in active use --
    are always kept). Returns ``{"removed": n}``. Filesystem work runs in a
    thread so the event loop stays responsive.
    """
    settings = settings or get_settings()
    max_age = settings.scheduler_temp_reaper_max_age_s

    def _reap() -> int:
        import shutil
        import tempfile
        import time
        from pathlib import Path

        root = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age
        removed = 0
        for entry in root.glob("vox-*"):
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue  # recent -- may be in active use
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue  # best-effort; a racing delete / perms never crashes the sweep
        return removed

    removed = await asyncio.to_thread(_reap)
    if removed:
        log.info("temp_reaper_swept", removed=removed, max_age_s=max_age)
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Loop supervisor + lifecycle
# ---------------------------------------------------------------------------


async def _interval_loop(
    name: str,
    interval_s: float,
    job: Callable[[], Awaitable[Any]],
    *,
    initial_delay_s: float = 0.0,
) -> None:
    """Run ``job`` every ``interval_s`` seconds until cancelled.

    Each tick is wrapped: ``CancelledError`` propagates (clean shutdown), but
    every other exception is logged and the loop continues -- a job NEVER crashes
    the worker. ``initial_delay_s`` staggers job start-up so they don't all fire
    in the same tick at boot.
    """
    if initial_delay_s:
        try:
            await asyncio.sleep(initial_delay_s)
        except asyncio.CancelledError:
            return
    log.info("scheduler_job_loop_started", job=name, interval_s=interval_s)
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            log.info("scheduler_job_loop_cancelled", job=name)
            raise
        except Exception as exc:  # noqa: BLE001 -- a job tick never kills the loop
            log.error("scheduler_job_failed", job=name, error=str(exc), exc_info=True)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            log.info("scheduler_job_loop_cancelled", job=name)
            raise


class Scheduler:
    """Owns the background job tasks and shuts them down cleanly.

    Constructed by :func:`start_scheduler`; the FastAPI lifespan calls
    :meth:`stop` on shutdown to cancel every loop and await its exit.
    """

    def __init__(self, tasks: list[asyncio.Task[Any]]) -> None:
        self._tasks = tasks

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    async def stop(self) -> None:
        """Cancel every job loop and await clean exit (idempotent)."""
        if not self._tasks:
            return
        for task in self._tasks:
            task.cancel()
        # return_exceptions so a task that raised on cancel doesn't mask shutdown.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("scheduler_stopped", jobs=len(self._tasks))
        self._tasks = []


def start_scheduler(settings: Settings | None = None) -> Scheduler:
    """Start the background job loops and return a :class:`Scheduler` (#354).

    Idempotent + safe: when Supabase isn't configured (local boot / tests) or
    when scheduling is disabled via ``SCHEDULER_ENABLED=false``, this logs and
    returns an empty :class:`Scheduler` (``task_count == 0``) -- the app boots
    normally with no loops. Otherwise it spawns one supervised loop per job.

    Silent-failure PR-4 cutover: the per-domain ``dispatch_watchdog`` and
    ``outbox_relay`` loops are gone -- the unified ``work_item_watchdog``
    owns both responsibilities now (any stuck dispatch is a stuck work_item;
    any failed outbox row rides the same retry chain). The underlying
    ``run_dispatch_watchdog_once`` and ``run_outbox_relay_once`` functions
    plus the legacy modules they pulled in (``operator_bridge``,
    ``operator_dispatch_watchdog``, ``outbox_relay``) are deleted.

    Must be called from inside a running event loop (the FastAPI lifespan).
    """
    settings = settings or get_settings()

    if not settings.scheduler_enabled:
        log.info("scheduler_disabled")
        return Scheduler([])

    # Mirror seed_compliance_rules_safe: no Supabase => nothing to schedule. The
    # admin client raises lazily, so probe the config rather than constructing it.
    if not settings.supabase_url or not settings.supabase_secret_key:
        log.info("scheduler_skipped_no_supabase")
        return Scheduler([])

    loop = asyncio.get_event_loop()
    tasks: list[asyncio.Task[Any]] = [
        # Silent-failure PR-4: the per-domain `dispatch_watchdog` +
        # `outbox_relay` loops + their underlying modules are gone -- the
        # unified `work_item_watchdog` covers BOTH responsibilities now (stuck
        # dispatches AND outbox-style retries ride the same kind enum + the
        # same heartbeat-stale rotation).
        loop.create_task(
            _interval_loop(
                "observability",
                settings.scheduler_observability_interval_s,
                lambda: run_observability_once(settings),
                initial_delay_s=15.0,
            ),
            name="scheduler:observability",
        ),
        loop.create_task(
            _interval_loop(
                "ghl_reconcile",
                settings.scheduler_reconcile_interval_s,
                lambda: run_reconciliation_once(settings),
                initial_delay_s=30.0,
            ),
            name="scheduler:ghl_reconcile",
        ),
        loop.create_task(
            _interval_loop(
                "kie_reconcile",
                settings.scheduler_kie_reconcile_interval_s,
                lambda: run_kie_reconcile_once(settings),
                initial_delay_s=20.0,
            ),
            name="scheduler:kie_reconcile",
        ),
        loop.create_task(
            _interval_loop(
                "work_item_watchdog",
                float(settings.work_item_watchdog_interval_s),
                lambda: run_work_item_watchdog_once(settings=settings),
                initial_delay_s=25.0,
            ),
            name="scheduler:work_item_watchdog",
        ),
        # Silent-failure PR-4: outbox consumer (replaces the deleted
        # ``outbox_relay``). Drains work_item rows of the outbox-* kinds; the
        # watchdog above retries / dead-letters stuck rows so retry policy
        # lives in one place. Same cadence as the watchdog.
        loop.create_task(
            _interval_loop(
                "outbox_drain",
                float(settings.scheduler_outbox_drain_interval_s),
                lambda: run_outbox_drain_once(
                    settings,
                    kinds=[
                        "outbox_meta_record_launch",
                        "outbox_drive_finalize_verified",
                        "outbox_ghl_send",
                    ],
                ),
                initial_delay_s=10.0,
            ),
            name="scheduler:outbox_drain",
        ),
        # Silent-failure PR-8: worker-stage consumer. Drains the deterministic
        # ``worker_ideation`` / ``worker_generation`` work_item rows the Next
        # advance + review-approve routes enqueue for NON-operator-driven
        # pipelines (the PR-3 cutover removed the fire-and-forget HTTP kicks but
        # never built a claimant for these kinds, so they sat queued forever).
        # The drain runs the in-process producer for each claimed row to
        # completion under a live heartbeat; the watchdog above retries /
        # dead-letters a stuck or failed row so retry policy lives in one place.
        # Monitor connector: ``worker_monitor`` is no longer drained -- the
        # monitor decision route now enqueues an ``operator_dispatch`` so the
        # operator EXECUTES the kill/scale on Meta + records it via
        # ``monitor_action_result`` (the old no-op acknowledgement handler is
        # gone). The ``worker_monitor`` enum value stays in the DB (forward-only)
        # but has no producer or consumer now.
        # FIX-A: ``worker_qa`` / ``worker_compliance`` / ``worker_spec`` ride the
        # same drain too -- the deterministic post-generation gate consumers the
        # auto-advance trigger + the advance route enqueue (the producers that
        # had been missing, deadlocking every pipeline at creative_qa).
        loop.create_task(
            _interval_loop(
                "worker_stage_drain",
                float(settings.scheduler_worker_stage_interval_s),
                lambda: run_worker_stage_drain_once(
                    settings,
                    kinds=[
                        "worker_ideation",
                        "worker_generation",
                        # FIX-A: the deterministic post-generation gate consumers
                        # (creative_qa / compliance_review / spec_validation). The
                        # auto-advance trigger + the advance route enqueue these
                        # for non-operator-driven pipelines; this drain claims +
                        # runs the verdict-writer for each.
                        "worker_qa",
                        "worker_compliance",
                        "worker_spec",
                    ],
                ),
                initial_delay_s=12.0,
            ),
            name="scheduler:worker_stage_drain",
        ),
    ]
    # Post-incident hardening: periodic temp-dir reaper (vox-* in /tmp). Gated
    # on a positive interval so it can be disabled via env.
    if settings.scheduler_temp_reaper_interval_s > 0:
        tasks.append(
            loop.create_task(
                _interval_loop(
                    "temp_reaper",
                    float(settings.scheduler_temp_reaper_interval_s),
                    lambda: run_temp_reaper_once(settings),
                    initial_delay_s=45.0,
                ),
                name="scheduler:temp_reaper",
            )
        )
    log.info("scheduler_started", jobs=len(tasks))
    return Scheduler(tasks)


__all__ = [
    "AlertCondition",
    "ReconcileTarget",
    "Scheduler",
    "deliver_ops_alerts",
    "evaluate_alert_conditions",
    "load_reconciliation_targets",
    "reset_alert_throttle",
    "run_kie_reconcile_once",
    "run_observability_once",
    "run_outbox_drain_once",
    "run_reconciliation_once",
    "run_temp_reaper_once",
    "run_work_item_watchdog_once",
    "run_worker_stage_drain_once",
    "start_scheduler",
]
