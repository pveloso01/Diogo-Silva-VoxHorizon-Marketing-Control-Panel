"""Tests for :mod:`voxhorizon_daemon.hermes_exec`.

The Docker SDK is mocked with hand-rolled doubles: we never spin up a
real container. The aim is to exercise the two public methods
(:meth:`HermesExec.auth_probe`, :meth:`HermesExec.chat`) plus the
:meth:`HermesExec._classify_error` table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import docker.errors  # type: ignore[import-untyped]
import pytest

from voxhorizon_daemon.hermes_exec import HermesExec


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeExecResult:
    exit_code: int
    output: tuple[bytes | None, bytes | None]


class FakeContainer:
    """Mimics :class:`docker.models.containers.Container` for our needs."""

    def __init__(
        self,
        *,
        status: str = "running",
        exec_results: list[FakeExecResult] | None = None,
    ) -> None:
        self.status = status
        self._exec_queue: list[FakeExecResult] = list(exec_results or [])
        self.exec_calls: list[list[str]] = []

    def reload(self) -> None:
        return None

    def exec_run(self, argv, demux: bool = False, **_kwargs):  # noqa: ANN001
        self.exec_calls.append(list(argv))
        if not self._exec_queue:
            return FakeExecResult(exit_code=0, output=(b"", b""))
        return self._exec_queue.pop(0)


class FakeDockerClient:
    """Mimics :class:`docker.client.DockerClient` for our needs."""

    def __init__(
        self, *, container: FakeContainer | None = None, raise_on_get: Exception | None = None
    ) -> None:
        self._container = container or FakeContainer()
        self._raise_on_get = raise_on_get

    @property
    def containers(self) -> "FakeDockerClient":
        return self

    def get(self, name: str) -> FakeContainer:
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._container


def _ok_diag(exp_seconds_in_future: int = 3600) -> bytes:
    import time as _t

    diag = {"reason": "ok", "exp": _t.time() + exp_seconds_in_future}
    return (json.dumps(diag) + "\n").encode("utf-8")


def _expired_diag() -> bytes:
    diag = {"reason": "expired", "exp": 1.0, "now": 1_000_000.0}
    return (json.dumps(diag) + "\n").encode("utf-8")


def _missing_diag() -> bytes:
    return (json.dumps({"reason": "auth_file_missing", "path": "/opt/data/auth.json"}) + "\n").encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# auth_probe
# ---------------------------------------------------------------------------


async def test_auth_probe_ok_when_exit_0():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=0, output=(_ok_diag(), b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)

    result = await hx.auth_probe()
    assert result.ok is True
    assert result.detail.get("reason") == "ok"


async def test_auth_probe_fails_when_auth_expired():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=2, output=(_expired_diag(), b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)

    result = await hx.auth_probe()
    assert result.ok is False
    assert result.detail.get("reason") == "expired"


async def test_auth_probe_fails_when_auth_file_missing():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=2, output=(_missing_diag(), b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)

    result = await hx.auth_probe()
    assert result.ok is False
    assert result.detail.get("reason") == "auth_file_missing"


async def test_auth_probe_fails_when_container_missing():
    client = FakeDockerClient(
        raise_on_get=docker.errors.NotFound("not_found")
    )
    hx = HermesExec(container_name="op", client=client)
    result = await hx.auth_probe()
    assert result.ok is False
    assert result.detail.get("reason") == "container_not_found"


async def test_auth_probe_handles_docker_error():
    client = FakeDockerClient(raise_on_get=RuntimeError("socket gone"))
    hx = HermesExec(container_name="op", client=client)
    result = await hx.auth_probe()
    assert result.ok is False
    assert result.detail.get("reason") == "docker_error"


async def test_auth_probe_handles_exec_api_error():
    class _BoomContainer(FakeContainer):
        def exec_run(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise docker.errors.APIError("exec boom")

    client = FakeDockerClient(container=_BoomContainer())
    hx = HermesExec(container_name="op", client=client)
    result = await hx.auth_probe()
    assert result.ok is False
    assert result.detail.get("reason") == "exec_failed"


async def test_auth_probe_handles_unparseable_stdout():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=0, output=(b"not json", b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.auth_probe()
    # exit 0 but garbage stdout: still ok=True, with the raw stdout in detail
    assert result.ok is True
    assert "raw" in result.detail


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


async def test_chat_success_returns_no_error_kind():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=0, output=(b"hello world", b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("instruct", session_id="sess-1", max_turns=5, timeout_s=5)
    assert result.exit_code == 0
    assert result.error_kind is None
    assert "hello world" in result.stdout_tail
    # argv carries the expected CLI shape. On the operator container's Hermes
    # build ``--pass-session-id`` is a BARE flag (no value): the session id must
    # NOT be appended as a CLI arg or argparse rejects it as an unrecognized
    # positional and exits 2. The id reaches the operator via the instruction.
    argv = container.exec_calls[0]
    assert argv[:3] == ["hermes", "chat", "-q"]
    assert "--pass-session-id" in argv
    assert "sess-1" not in argv
    assert "--max-turns" in argv and "5" in argv


async def test_chat_classifies_auth_expired():
    stderr = b"openai.AuthenticationError: status 401 from upstream\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=1, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "auth_expired"


async def test_chat_classifies_llm_4xx():
    stderr = b"upstream returned status 429 too many\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=1, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "llm_4xx"


async def test_chat_classifies_llm_5xx():
    stderr = b"upstream returned 502 bad gateway\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=1, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "llm_5xx"


async def test_chat_classifies_skill_missing():
    stderr = b"error: skill not found: pipeline-operator\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=1, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "skill_missing"


async def test_chat_classifies_hermes_crashed_on_traceback():
    stderr = b"Traceback (most recent call last):\n  File ...\nValueError: x\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=1, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "hermes_crashed"


async def test_chat_classifies_hermes_crashed_on_sigkill_exit_code():
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=137, output=(b"", b""))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "hermes_crashed"


async def test_chat_falls_through_to_unknown():
    stderr = b"something weird went wrong\n"
    container = FakeContainer(
        exec_results=[FakeExecResult(exit_code=99, output=(b"", stderr))]
    )
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "unknown"


async def test_chat_handles_container_not_found():
    client = FakeDockerClient(raise_on_get=docker.errors.NotFound("nope"))
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.exit_code == -1
    assert result.error_kind == "docker_exec_failed"


async def test_chat_handles_docker_exception():
    client = FakeDockerClient(raise_on_get=RuntimeError("boom"))
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "docker_exec_failed"


async def test_chat_handles_api_error_mid_exec():
    class _Boom(FakeContainer):
        def exec_run(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise docker.errors.APIError("api boom")

    client = FakeDockerClient(container=_Boom())
    hx = HermesExec(container_name="op", client=client)
    result = await hx.chat("i", session_id="s")
    assert result.error_kind == "docker_exec_failed"


async def test_chat_timeout_classifies_as_hermes_crashed(monkeypatch: pytest.MonkeyPatch):
    """A wall-clock timeout becomes hermes_crashed (the process is hung)."""

    async def _sleep_forever(*_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        import asyncio as _a

        await _a.sleep(10)
        return FakeExecResult(exit_code=0, output=(b"", b""))

    class _SlowContainer(FakeContainer):
        def exec_run(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            # Synchronous call inside to_thread -- block briefly.
            import time as _t

            _t.sleep(2)
            return FakeExecResult(exit_code=0, output=(b"", b""))

    client = FakeDockerClient(container=_SlowContainer())
    hx = HermesExec(container_name="op", client=client)
    # timeout_s=0 forces the wait_for to time out immediately
    result = await hx.chat("i", session_id="s", timeout_s=0)
    assert result.error_kind == "hermes_crashed"


async def test_chat_timeout_kills_orphaned_exec(monkeypatch: pytest.MonkeyPatch):
    """On timeout the daemon best-effort pkills the wedged `hermes chat` so a
    runaway render can't keep burning the operator container's resources."""

    class _HangThenRecord(FakeContainer):
        def exec_run(self, argv, demux: bool = False, **_kw):  # noqa: ANN001
            self.exec_calls.append(list(argv))
            if "pkill" in argv:  # the cleanup call returns fast
                return FakeExecResult(exit_code=0, output=(b"", b""))
            import time as _t  # the chat call hangs past the timeout

            _t.sleep(1)
            return FakeExecResult(exit_code=0, output=(b"", b""))

    container = _HangThenRecord()
    hx = HermesExec(container_name="op", client=FakeDockerClient(container=container))
    result = await hx.chat("i", session_id="s", timeout_s=0)
    assert result.error_kind == "hermes_crashed"
    assert any("pkill" in call for call in container.exec_calls)


# ---------------------------------------------------------------------------
# container_status
# ---------------------------------------------------------------------------


def test_container_status_returns_status():
    container = FakeContainer(status="running")
    client = FakeDockerClient(container=container)
    hx = HermesExec(container_name="op", client=client)
    assert hx.container_status() == "running"


def test_container_status_not_found():
    client = FakeDockerClient(raise_on_get=docker.errors.NotFound("x"))
    hx = HermesExec(container_name="op", client=client)
    assert hx.container_status() == "not_found"


def test_container_status_error_path():
    client = FakeDockerClient(raise_on_get=RuntimeError("socket"))
    hx = HermesExec(container_name="op", client=client)
    assert hx.container_status().startswith("error:")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_quote_argv_quotes_shell_special():
    quoted = HermesExec.quote_argv(["hermes", "chat", "hello world"])
    assert "hello world" in quoted or "'hello world'" in quoted


def test_unpack_demux_handles_non_demux():
    class _R:
        output = b"plain bytes"

    out, err = HermesExec._unpack_demux(_R())
    assert out == b"plain bytes"
    assert err is None


def test_unpack_demux_handles_none():
    class _R:
        output = None

    out, err = HermesExec._unpack_demux(_R())
    assert out is None and err is None
