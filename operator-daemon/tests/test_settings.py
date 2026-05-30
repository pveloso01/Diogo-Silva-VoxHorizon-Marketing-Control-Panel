"""Settings env-binding tests.

The load-bearing case: the daemon must read the operator container name from
``HERMES_OPERATOR_CONTAINER_NAME`` and IGNORE ``HERMES_CONTAINER_NAME`` -- the
latter is the worker's legacy-bridge var (``hermes-agent-ekko``) and lives in
the shared ``/opt/voxhorizon/.env`` the daemon also loads. Binding to the field
name silently routed every operator dispatch into the un-hardened Ekko
container; these tests pin the decoupling.
"""

from __future__ import annotations

import pytest

from voxhorizon_daemon.settings import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_URL", "http://worker.test")
    monkeypatch.setenv("WORKER_SHARED_SECRET", "test-secret")
    monkeypatch.delenv("HERMES_OPERATOR_CONTAINER_NAME", raising=False)
    monkeypatch.delenv("HERMES_CONTAINER_NAME", raising=False)


def test_reads_operator_container_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("HERMES_OPERATOR_CONTAINER_NAME", "hermes-agent-operator")
    assert Settings().hermes_container_name == "hermes-agent-operator"  # type: ignore[call-arg]


def test_ignores_worker_container_var(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the worker's var is set (the shared-.env reality). The daemon must
    # NOT pick it up -- it falls back to the safe operator default.
    _base_env(monkeypatch)
    monkeypatch.setenv("HERMES_CONTAINER_NAME", "hermes-agent-ekko")
    assert Settings().hermes_container_name == "hermes-agent-operator"  # type: ignore[call-arg]


def test_operator_var_wins_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact live collision: both vars present. The operator var must win so
    # the daemon execs into the hardened operator, never the legacy Ekko agent.
    _base_env(monkeypatch)
    monkeypatch.setenv("HERMES_CONTAINER_NAME", "hermes-agent-ekko")
    monkeypatch.setenv("HERMES_OPERATOR_CONTAINER_NAME", "hermes-agent-operator")
    assert Settings().hermes_container_name == "hermes-agent-operator"  # type: ignore[call-arg]


def test_default_is_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    assert Settings().hermes_container_name == "hermes-agent-operator"  # type: ignore[call-arg]
