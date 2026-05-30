"""Shared fixtures for the operator-daemon test suite.

Pytest-asyncio is configured with ``asyncio_mode = "auto"`` in
``pyproject.toml`` so every ``async def test_*`` is auto-marked. We do
NOT call into the network anywhere; tests inject fakes (respx for
httpx, hand-rolled doubles for docker / queue / hermes_exec).
"""

from __future__ import annotations

from voxhorizon_daemon.settings import Settings, get_settings


def make_settings(**overrides: object) -> Settings:
    """Construct a Settings model with sane test defaults.

    Settings normally read from the environment; instead, we hand-craft
    the model via kwargs so tests stay hermetic and CI doesn't need any
    real WORKER_URL / WORKER_SHARED_SECRET to be set.
    """
    base: dict[str, object] = {
        "worker_url": "http://worker.test",
        "worker_shared_secret": "test-secret",
        "consumer_id": "operator-daemon-test",
        # hermes_container_name is bound to the HERMES_OPERATOR_CONTAINER_NAME
        # validation alias, so it must be supplied under that key (not the
        # field name) when constructing the model directly.
        "HERMES_OPERATOR_CONTAINER_NAME": "hermes-agent-operator",
        "consumer_heartbeat_s": 5,
        "work_item_heartbeat_s": 2,
        "claim_poll_interval_s": 1,
        "chat_timeout_s": 60,
        "chat_max_turns": 10,
        "healthz_port": 9001,
        "startup_auth_probe": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def clear_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = ["clear_settings_cache", "make_settings"]
