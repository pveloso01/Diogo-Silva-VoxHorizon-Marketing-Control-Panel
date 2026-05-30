"""Centralized environment-backed settings.

Mirrors the JS `lib/env.ts` clean_env pattern: whitespace is stripped from
every value, empty strings collapse to None, and access is via a cached
singleton so import order doesn't matter.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BrollBackend = Literal["local", "supabase"]


def clean_env(value: str | None) -> str | None:
    """Strip whitespace and collapse empty values to None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class Settings(BaseSettings):
    """All env vars the worker reads. Required values raise on first access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Worker auth + URLs
    worker_shared_secret: str = Field(..., min_length=1)
    worker_public_base_url: str = "http://localhost:8000"
    worker_cors_origin: str = "http://localhost:3000"

    # Supabase
    supabase_url: str | None = None
    supabase_secret_key: str | None = None

    # External services (optional at boot; routes that need them fail loudly)
    kie_ai_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    submagic_api_key: str | None = None
    meta_ads_api_key: str | None = None

    # kie.ai video completion-callback HMAC secret (E5.2 / #514). kie signs
    # ``f"{taskId}.{timestamp}"`` with this key and sends the Base64 digest in
    # ``X-Webhook-Signature`` on the completion POST to /work/video/kie-callback.
    # The callback receiver verifies it via
    # ``KieVideoClient.verify_webhook_signature`` before recording the result.
    # Optional at boot: when unset the callback route 503s (it cannot verify a
    # signature without the key) and the durable reconciliation sweep is the
    # safety net that still recovers the render.
    kie_ai_webhook_secret: str | None = None

    # Fake-integration mode (T.4 / #317). When a FAKE_* flag is on, the
    # corresponding external integration is stubbed in-process with a
    # deterministic response and makes ZERO outbound network calls — so the
    # whole pipeline can run locally / in CI with no real Meta, GHL, Drive,
    # or image-render credentials. Off by default so production behaviour is
    # never accidentally faked; tests and local runs opt in via env.
    #
    #   FAKE_META    — Meta Ads recorder/launch saga returns canned ad ids.
    #   FAKE_GHL     — GoHighLevel lead pull / webhook returns canned leads.
    #   FAKE_DRIVE   — Google Drive upload returns a deterministic fake url.
    #   FAKE_RENDER  — Kie.ai / codex image render returns a deterministic
    #                  1x1 PNG (see services.kie.KieClient) instead of polling
    #                  the real API.
    #
    # Meta/GHL/Drive are not built yet (see PIPELINE-REBUILD-ARCHITECTURE.md
    # Layer 6); the flags + this convention land now so those services wire
    # to them by construction when they arrive. FAKE_RENDER is live today.
    fake_meta: bool = False
    fake_ghl: bool = False
    fake_drive: bool = False
    fake_render: bool = False

    # === Per-pipeline hard budget cap (E4.4 / #506) ===
    # The worker enforces a SERVER-SIDE hard cap on a pipeline's CUMULATIVE
    # actual spend (summed from the cost_ledger) before every paid vendor call.
    # The per-ad pre-flight estimate (routes.video) only bounds a SINGLE ad's
    # generation; nothing bounded the running total across retries, iterations,
    # and many ads, so cumulative spend could overrun. This is that ceiling.
    #
    # Sourcing precedence (see services.cost_ledger.resolve_pipeline_cap):
    #   1. ``pipelines.budget_cap_usd`` — a per-pipeline override (migration 0042)
    #   2. this default — the agency-wide ceiling when a pipeline sets none
    #
    # Default derived to comfortably hold a multi-ad pipeline's generation +
    # iteration spend while still refusing a runaway. The cap is enforced against
    # the ACTUAL ledger, so an AUTO_APPROVE window (which records the same cost
    # lines) can never push cumulative spend past it.
    pipeline_budget_cap_usd: float = 50.0

    # B-roll storage
    broll_store_backend: BrollBackend = "local"
    broll_local_root: str = "~/voxhorizon-worker/storage/broll-pool"

    # Observability
    tailscale_hostname: str = "voxhorizon-worker"

    # === Periodic background scheduler (#354) ===
    # The worker runs its cron cores in supervised asyncio loops (see
    # services.scheduler): observability/ops-alert delivery, the GHL daily
    # reconciliation, the kie video render reconciliation, the unified work_item
    # watchdog, the outbox drain, and the worker-stage drain. All knobs are
    # env-backed with conservative defaults. Set SCHEDULER_ENABLED=false to
    # disable every loop; the scheduler also auto-skips when Supabase is
    # unconfigured, so local boots / tests need set nothing.
    scheduler_enabled: bool = True
    scheduler_observability_interval_s: float = 300.0
    # GHL daily reconciliation: no-op until the client_integrations table exists.
    scheduler_reconcile_interval_s: float = 86_400.0
    scheduler_reconcile_window_days: int = 1
    # kie video render reconciliation (E5.2 / #514): a periodic sweep that finds
    # renders persisted as ``submitted`` (in video_render_tasks) that the
    # callback never resolved (a restart mid-poll, or a dropped callback), polls
    # kie for each, and records the result. Bounded per pass like the dispatch
    # watchdog so a backlog can't fan out an unbounded burst of record-info GETs.
    scheduler_kie_reconcile_interval_s: float = 120.0
    scheduler_kie_reconcile_max_per_pass: int = 10

    # === Silent-failure redesign PR-1: unified work_item watchdog ===
    # The new observer runs alongside the legacy per-domain watchdogs in PR-1
    # (no behavior change yet). It reads claimed/running work_item rows whose
    # heartbeat is stale, rotates the claim_token (timed_out + parent-chained
    # requeue with exponential backoff), and flags stale consumers as
    # ``degraded`` / ``down`` so the DaemonHealthBadge surfaces them. PR-3
    # retires the legacy watchdogs once cutover is complete.
    work_item_max_attempts: int = 3
    work_item_heartbeat_threshold_s: int = 120
    work_item_consumer_heartbeat_s: int = 30
    work_item_watchdog_interval_s: int = 30
    work_item_backoff_base_s: int = 60
    work_item_backoff_cap_s: int = 3_600
    work_item_watchdog_max_per_pass: int = 25

    # === Silent-failure redesign PR-4: outbox consumer ===
    # The outbox drain loop (``services.outbox_consumer.run_outbox_drain_once``)
    # claims work_item rows of the outbox-* kinds and dispatches to the
    # registered handler -- the replacement for the deleted ``outbox_relay``.
    # Interval mirrors the watchdog cadence so the drain + the rotation observer
    # tick on the same beat. ``outbox_max_attempts`` is the cap surfaced to the
    # consumer's structured logs; the watchdog enforces the actual retry chain
    # via ``work_item_max_attempts`` (kept in sync as defaults of equal value).
    scheduler_outbox_drain_interval_s: int = 5
    outbox_max_attempts: int = 5

    # === Silent-failure redesign PR-8: worker-stage consumer ===
    # The worker-stage drain loop
    # (``services.worker_stage_consumer.run_worker_stage_drain_once``) claims
    # work_item rows of the deterministic ``worker_ideation`` /
    # ``worker_generation`` kinds and runs the in-process producer for each --
    # the missing consumer half of the PR-3 cutover (which removed the routes'
    # fire-and-forget HTTP kicks but never built a claimant for these kinds).
    # These stages are LONGER-running than the outbox handlers (each calls Kie /
    # renders), so the consumer heartbeats the claimed row throughout the run
    # (cadence = ``work_item_consumer_heartbeat_s``) to keep the watchdog from
    # reclaiming it mid-render. A slightly slower interval than the outbox drain
    # is fine: ideation/generation are kicked by a manager gate, not high-volume.
    scheduler_worker_stage_interval_s: int = 5

    # === Post-incident hardening: temp-dir reaper ===
    # Worker subprocess helpers (yt-dlp / ffmpeg / probes) mkdtemp() under the
    # system tmp dir with a ``vox-`` prefix; several callsites intentionally
    # leave the files for the caller, so a long-lived worker accumulates them.
    # This periodic reaper deletes ``vox-*`` temp entries older than the max age
    # so a heavy video run can't slowly fill the host disk between deploys (the
    # deploy-recreate clears /tmp, but uptime is unbounded). Set the interval to
    # 0 to disable.
    scheduler_temp_reaper_interval_s: int = 3_600
    scheduler_temp_reaper_max_age_s: int = 21_600  # 6h

    # Slack approval fan-out (HI-17, post-Slack-pivot 2026-05-18). The worker
    # posts high-urgency approval notifications to a single Slack channel via
    # chat.postMessage. The bot token is sourced at deploy time from
    # /docker/hermes-shared/config/secrets.json (key EKKO_SLACK_BOT_TOKEN) and
    # surfaced into the container as SLACK_BOT_TOKEN. The channel ID is the
    # static Slack ID for #mkt-dept-updates. Both values are optional at boot —
    # when either is missing the fan-out helper logs and gracefully skips
    # the Slack step (push still fires).
    slack_bot_token: str | None = None
    slack_approval_channel_id: str | None = None
    # Base URL the Slack "Open in dashboard" CTA links to. The worker never
    # serves this URL itself; the link resolves on the operator's browser.
    dashboard_base_url: str = "https://dashboard.voxhorizon.com"

    # === Ops alerting (E5.6 / #526) ===
    # The observability tick detects operational problems (a growing/over-
    # threshold outbox dead-letter pile, an open circuit breaker, cost over its
    # cap) and DELIVERs a Slack alert to a SEPARATE ops channel -- distinct from
    # the approval channel so on-call noise never drowns the approval queue, and
    # vice versa. The bot token is the same ``SLACK_BOT_TOKEN`` the approval
    # fan-out uses; only the channel differs. Optional at boot: when
    # ``SLACK_OPS_CHANNEL_ID`` (or the token) is unset the alert tick logs
    # ``ops_alert_skipped_no_channel`` and the structured log lines still emit
    # (log-based alerting keeps working). The ops-alert delivery is best-effort
    # -- it never raises and never blocks the supervised scheduler loop.
    slack_ops_channel_id: str | None = None
    # SLO targets / alert thresholds. Each is the boundary at which the matching
    # condition flips to "bad" and (on transition into bad, throttled) pages the
    # ops channel. Conservative defaults far above a healthy steady state so a
    # slow-but-alive system is never paged; see docs/observability.md.
    #
    #   * outbox dead-letter count: a dead-letter pile (status='dead' + 'failed')
    #     at/above this is a delivery-SLO breach worth paging on.
    ops_alert_outbox_dead_letter_threshold: int = 1  # SLO: zero dead letters
    #   * outbox depth: pending+inflight backlog at/above this is a drain-SLO
    #     breach (the relay is falling behind).
    ops_alert_outbox_depth_threshold: int = 100  # SLO: depth < 100
    # Throttle: once an alert kind has paged, the SAME kind is suppressed for
    # this many seconds (re-arming only after a return to healthy, OR after the
    # window lapses) so a persistent bad state pages on transition, not every
    # tick. Default one hour mirrors the notifications dedupe window.
    ops_alert_throttle_s: float = 3_600.0

    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v

    @property
    def broll_local_root_path(self) -> Path:
        """Expanded absolute path for the local b-roll pool."""
        return Path(self.broll_local_root).expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return Settings()  # type: ignore[call-arg]
