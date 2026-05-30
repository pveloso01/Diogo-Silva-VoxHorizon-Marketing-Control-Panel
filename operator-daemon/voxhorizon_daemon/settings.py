"""Environment-backed settings for the operator daemon.

Mirrors the worker's ``clean_env`` pattern (whitespace stripped, empty
strings collapse to ``None``) via a pydantic-settings model. The cached
singleton means import order does not matter and tests can clear the
cache to inject env between cases.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env the daemon reads. Required values raise on first access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Worker REST surface ---------------------------------------------------
    # The base URL the queue_client hits. Production: the worker service inside
    # the docker network (e.g. ``http://worker:8000``). Dev: ``http://localhost:8000``.
    worker_url: str = Field(..., min_length=1)

    # Bearer token; the same secret every other worker route accepts. The
    # daemon presents it on EVERY call (verify_secret is unconditional on
    # work_queue endpoints).
    worker_shared_secret: str = Field(..., min_length=1)

    # --- Consumer identity ---------------------------------------------------
    # Stable id the consumer writes to ``work_item_consumers.id`` and presents
    # on every claim. Single-process today; replicas will carry ``-1``, ``-2``
    # suffixes.
    consumer_id: str = Field("operator-daemon-1", min_length=1)

    # The image tag baked at build time. Recorded on the consumer row so the
    # dashboard can show "live: operator-daemon@v0.1.0". Best-effort: the deploy
    # workflow injects it; local runs leave it empty.
    image_tag: str | None = None

    # Linux hostname the container resolves to. Stamped on the consumer row for
    # multi-host debugging.
    hostname: str | None = None

    # --- The Hermes container -------------------------------------------------
    # The sibling container the daemon execs into. Matches the live container
    # name on the VPS; tests override.
    #
    # Read from env ``HERMES_OPERATOR_CONTAINER_NAME`` (the var compose sets for
    # this service), NOT ``HERMES_CONTAINER_NAME`` -- the latter is the worker's
    # legacy-bridge var (= ``hermes-agent-ekko``) and lives in the SHARED
    # ``/opt/voxhorizon/.env`` the daemon also loads. Binding to the field name
    # made the daemon read that shared var and exec every dispatch into the
    # un-hardened legacy Ekko container instead of the operator. The explicit
    # alias decouples the two so the operator daemon always targets the operator.
    hermes_container_name: str = Field(
        "hermes-agent-operator",
        validation_alias="HERMES_OPERATOR_CONTAINER_NAME",
    )

    # Path inside the Hermes container that holds ``auth.json``. Hermes' canonical
    # token store is ``$HERMES_HOME/auth.json``; the operator's compose entry
    # mounts ``/docker/hermes-operator/data -> /opt/data``, so the in-container
    # path is ``/opt/data/auth.json`` and ``HERMES_HOME=/opt/data``.
    hermes_data_dir: str = "/opt/data"

    # --- Timings --------------------------------------------------------------
    # How often the daemon PATCHes ``work_item_consumers.last_seen_at``. The
    # dashboard's stale threshold is derived (2x this value) on the read side.
    consumer_heartbeat_s: int = Field(30, ge=1, le=600)

    # How often the per-claim heartbeat task PATCHes
    # ``/work/queue/{id}/heartbeat``. Must be << the worker watchdog's stale
    # threshold (90s today) so a brief network hiccup doesn't trigger rotation.
    work_item_heartbeat_s: int = Field(20, ge=1, le=120)

    # Sleep between empty claim() polls. Cheap; the worker's claim is a single
    # row UPDATE so 5s is fine.
    claim_poll_interval_s: int = Field(5, ge=1, le=60)

    # Max wall-clock for one ``hermes chat`` invocation. Anything longer is
    # almost certainly hung; the watchdog will rotate the token anyway, but the
    # daemon should cut its own loss locally first.
    chat_timeout_s: int = Field(1200, ge=30, le=7200)

    # ``hermes chat --max-turns`` value passed on every dispatch.
    chat_max_turns: int = Field(40, ge=1, le=200)

    # --- Healthz sidecar ------------------------------------------------------
    # Port the /healthz FastAPI server binds to. Compose's healthcheck probes
    # ``http://localhost:9001/healthz``.
    healthz_port: int = Field(9001, ge=1, le=65535)

    # --- Flags ----------------------------------------------------------------
    # Run the Hermes auth probe at startup. Off only for test harness setups
    # that pre-validate the container themselves.
    startup_auth_probe: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()  # type: ignore[call-arg]


__all__ = ["Settings", "get_settings"]
