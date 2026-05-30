"""Docker-socket wrapper around the colocated ``hermes-agent-operator`` container.

The sidecar OWNS the work_item lifecycle but does NOT own the Hermes
process. It drives Hermes by ``docker exec``-ing into the operator
container the same way the live system has been driving it from the
dashboard. Two operations:

* :meth:`HermesExec.auth_probe` — startup-time validation that the
  operator's ChatGPT/Codex OAuth credentials are present and not
  expired. Implemented via ``docker exec`` of a short Python one-liner
  that reads ``$HERMES_HOME/auth.json`` and exits 0 on a valid token, 2
  on an expired/malformed one. We use the file-read approach rather
  than a hypothetical ``hermes auth status`` subcommand because the
  upstream Hermes CLI surface does not expose an auth-introspection
  command we could rely on across releases. The auth file is Hermes'
  canonical token store (see codex_render.py:147 and the operator's
  config: ``HERMES_HOME=/opt/data``).

* :meth:`HermesExec.chat` — one ``hermes chat`` invocation per
  ``work_item``. Returns the LAST 4 KB of stdout plus a classified
  ``error_kind`` derived from exit code + stderr patterns.

Error classification follows the daemon's closed enumeration in
:mod:`types`: ``auth_expired``, ``llm_4xx``, ``llm_5xx``,
``docker_exec_failed``, ``hermes_crashed``, ``skill_missing``,
``unknown``. Patterns are conservative — anything that does not match
falls through to ``unknown`` so the dashboard can still surface a clean
"failed" without us pretending to classify a novel failure.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import textwrap
from typing import Any

import docker  # type: ignore[import-untyped]
import docker.errors  # type: ignore[import-untyped]
import structlog

from .types import AuthProbeResult, ChatResult, DaemonErrorKind


log = structlog.get_logger(__name__)


# Tail size kept on stdout/stderr capture. Whatever exceeds this gets dropped
# from the head; failure diagnostics live in the tail.
_OUTPUT_TAIL_BYTES = 4096


# Error patterns matched on the combined stdout+stderr tail. Order matters:
# the first match wins, so the more specific patterns sit first.
#
# All comparisons are case-insensitive. Patterns derived from inspecting the
# operator's running config (config.yaml shows the codex/openai-codex provider
# with OAuth via auth.json) and the codex_render.py wrapper's own error
# messages (the "auth.json" pattern catches the operator's user-facing wording
# from skills/pipeline-operator/codex_render.py).
#
# New patterns can be added without breaking tests because the classifier
# returns ``unknown`` on no-match — never crashes.
_ERROR_PATTERNS: tuple[tuple[str, DaemonErrorKind], ...] = (
    ("auth.json", "auth_expired"),
    ("unauthorized", "auth_expired"),
    ("token has expired", "auth_expired"),
    ("oauth", "auth_expired"),
    ("invalid_grant", "auth_expired"),
    ("expired_token", "auth_expired"),
    ("status 401", "auth_expired"),
    ("status 403", "auth_expired"),
    ("status 400", "llm_4xx"),
    ("status 404", "llm_4xx"),
    ("status 422", "llm_4xx"),
    ("status 429", "llm_4xx"),
    ("status 5", "llm_5xx"),
    ("internal server error", "llm_5xx"),
    ("bad gateway", "llm_5xx"),
    ("gateway timeout", "llm_5xx"),
    ("skill not found", "skill_missing"),
    ("no such skill", "skill_missing"),
    ("modulenotfounderror", "hermes_crashed"),
    ("traceback (most recent call last)", "hermes_crashed"),
    ("segmentation fault", "hermes_crashed"),
)


class HermesExec:
    """Synchronous-Docker wrapper exposed through an async-friendly facade.

    The Docker SDK is itself synchronous; methods that touch it run under
    :func:`asyncio.to_thread` so the daemon's event loop stays responsive
    while a ``hermes chat`` invocation grinds. The class is otherwise
    stateless apart from the docker client instance.
    """

    def __init__(
        self,
        *,
        container_name: str,
        hermes_data_dir: str = "/opt/data",
        client: Any | None = None,
    ) -> None:
        self.container_name = container_name
        self.hermes_data_dir = hermes_data_dir
        # ``client`` is injected by tests; production lets ``docker.from_env()``
        # pick up DOCKER_HOST / /var/run/docker.sock.
        self._client = client if client is not None else docker.from_env()

    # ------------------------------------------------------------------
    # container introspection (sync; called inside to_thread)
    # ------------------------------------------------------------------

    def container_status(self) -> str:
        """Return the docker container status, or ``not_found`` / ``error:..``."""
        try:
            container = self._client.containers.get(self.container_name)
            container.reload()
            return str(container.status)
        except docker.errors.NotFound:
            return "not_found"
        except Exception as exc:  # noqa: BLE001 — surface as string
            return f"error:{exc}"

    # ------------------------------------------------------------------
    # auth_probe
    # ------------------------------------------------------------------

    # The probe script. Reads $HERMES_HOME/auth.json and exits:
    #   0 — valid (expiry absent OR expiry > now + 60s buffer)
    #   2 — invalid (missing file / parse error / expired)
    # The stdout JSON line carries diagnostics back to the daemon (the daemon
    # parses it). stderr stays empty on the happy path.
    _AUTH_PROBE_SCRIPT = textwrap.dedent(
        """
        import json, os, sys, time
        path = sys.argv[1]
        if not os.path.exists(path):
            print(json.dumps({"reason": "auth_file_missing", "path": path}))
            sys.exit(2)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"reason": "auth_file_unreadable", "error": str(exc)}))
            sys.exit(2)
        # Hermes' auth.json carries tokens under several layouts depending on
        # provider; the union we check covers chatgpt/codex OAuth where the
        # token block lives at the root or under "tokens".
        candidates = [data, data.get("tokens", {}) if isinstance(data, dict) else {}]
        exp = None
        for c in candidates:
            if not isinstance(c, dict):
                continue
            for key in ("expires_at", "exp", "expiry", "token_expires_at"):
                if key in c and isinstance(c[key], (int, float)):
                    exp = float(c[key])
                    break
            if exp is not None:
                break
        if exp is None:
            # No expiry field at all. Treat as present-and-trusted; Hermes will
            # surface a downstream auth failure at chat time if the token is
            # bad, and the daemon's chat() classifier picks that up.
            print(json.dumps({"reason": "no_expiry_field", "trusted": True}))
            sys.exit(0)
        if exp <= time.time() + 60:
            print(json.dumps({"reason": "expired", "exp": exp, "now": time.time()}))
            sys.exit(2)
        print(json.dumps({"reason": "ok", "exp": exp}))
        sys.exit(0)
        """
    ).strip()

    async def auth_probe(self) -> AuthProbeResult:
        """Probe whether the operator's Hermes auth file is valid.

        Implementation: ``docker exec hermes-agent-operator python -c <script>
        /opt/data/auth.json``. The script (above) writes a one-line JSON
        diagnostic to stdout and exits 0/2. The daemon parses the diagnostic
        and returns an :class:`AuthProbeResult` with ``ok`` set accordingly.

        Any docker-side failure (container missing, socket unreachable) is
        captured as ``ok=False`` with a structured ``detail`` so the startup
        path can surface it to the consumer row.
        """
        argv = [
            "python",
            "-c",
            self._AUTH_PROBE_SCRIPT,
            f"{self.hermes_data_dir.rstrip('/')}/auth.json",
        ]
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, self.container_name
            )
        except docker.errors.NotFound:
            return AuthProbeResult(
                ok=False, detail={"reason": "container_not_found", "name": self.container_name}
            )
        except Exception as exc:  # noqa: BLE001
            return AuthProbeResult(
                ok=False, detail={"reason": "docker_error", "error": str(exc)}
            )

        try:
            res = await asyncio.to_thread(
                container.exec_run, argv, demux=True
            )
        except docker.errors.APIError as exc:
            return AuthProbeResult(
                ok=False, detail={"reason": "exec_failed", "error": str(exc)}
            )

        exit_code = int(getattr(res, "exit_code", 0) or 0)
        stdout_bytes, _stderr_bytes = self._unpack_demux(res)
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        diag: dict[str, Any]
        try:
            diag = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except (json.JSONDecodeError, IndexError):
            diag = {"raw": stdout[-_OUTPUT_TAIL_BYTES:]}

        ok = exit_code == 0
        return AuthProbeResult(ok=ok, detail=diag)

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        instruction: str,
        session_id: str,
        *,
        max_turns: int = 40,
        timeout_s: int = 1200,
    ) -> ChatResult:
        """Run one ``hermes chat`` invocation inside the operator container.

        Argv: ``hermes chat -q "<instruction>" --pass-session-id
        --max-turns <N>``. On the operator container's Hermes build
        (``hermes-lua:src``) ``--pass-session-id`` is a BARE flag that takes
        NO value: it injects the run's session id into the agent's system
        prompt. The worker's ``hermes_bridge`` drives a DIFFERENT container
        whose CLI accepts ``--pass-session-id <id>``; copying that here made
        argparse reject the appended id as an unrecognized positional and
        exit 2, stalling every operator dispatch. The pipeline id the
        operator needs is already in ``instruction``, and each dispatch reads
        pipeline state from the DB, so no CLI-level session continuity is
        required.

        Returns a :class:`ChatResult` with the last 4 KB of stdout and a
        classified ``error_kind`` (``None`` on success).
        """
        log.info(
            "hermes_chat_invoke",
            session_id=session_id,
            max_turns=max_turns,
            timeout_s=timeout_s,
        )
        argv = [
            "hermes",
            "chat",
            "-q",
            instruction,
            "--pass-session-id",
            "--max-turns",
            str(max_turns),
        ]

        try:
            container = await asyncio.to_thread(
                self._client.containers.get, self.container_name
            )
        except docker.errors.NotFound:
            return ChatResult(
                exit_code=-1,
                stdout_tail="container_not_found",
                error_kind="docker_exec_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return ChatResult(
                exit_code=-1,
                stdout_tail=str(exc)[:_OUTPUT_TAIL_BYTES],
                error_kind="docker_exec_failed",
            )

        # exec_run is blocking; we cap it with asyncio.wait_for so a runaway
        # hermes chat does not pin the daemon. Cancellation propagates as a
        # CancelledError; the chat call inside the container is left running
        # because docker.from_env() does not support cancellation, but the
        # watchdog will rotate the claim_token and the next claim() will pick
        # up the next row.
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    argv,
                    demux=True,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            # The blocking exec_run can't be cancelled, so the `hermes chat`
            # keeps running inside the operator container after we stop awaiting
            # it -- a wedged render would otherwise burn CPU/RAM there and stack
            # with the next claim's chat. Best-effort kill it. Single-daemon
            # drains serially, so this dispatch's chat is the only `hermes chat`
            # process in the container; pkill -f targets exactly it. pkill
            # (procps-ng) ships in the operator image; failure is swallowed so
            # we never mask the timeout result.
            try:
                await asyncio.to_thread(
                    container.exec_run, ["pkill", "-TERM", "-f", "hermes chat"]
                )
            except Exception:  # noqa: BLE001 -- best-effort reap
                pass
            return ChatResult(
                exit_code=-1,
                stdout_tail=f"timeout after {timeout_s}s",
                error_kind="hermes_crashed",
            )
        except docker.errors.APIError as exc:
            return ChatResult(
                exit_code=-1,
                stdout_tail=str(exc)[:_OUTPUT_TAIL_BYTES],
                error_kind="docker_exec_failed",
            )

        exit_code = int(getattr(res, "exit_code", 0) or 0)
        stdout_bytes, stderr_bytes = self._unpack_demux(res)
        stdout_tail = (stdout_bytes or b"")[-_OUTPUT_TAIL_BYTES:].decode(
            "utf-8", errors="replace"
        )
        stderr_tail = (stderr_bytes or b"")[-_OUTPUT_TAIL_BYTES:].decode(
            "utf-8", errors="replace"
        )

        if exit_code == 0:
            return ChatResult(exit_code=0, stdout_tail=stdout_tail, error_kind=None)

        kind = self._classify_error(exit_code, stdout_tail, stderr_tail)
        # Combine stdout + a short stderr tail so the dashboard sees BOTH if
        # the failure wrote its message to stderr (Python tracebacks usually do).
        combined = stdout_tail
        if stderr_tail:
            combined = (combined + "\n[stderr]\n" + stderr_tail)[-_OUTPUT_TAIL_BYTES:]
        return ChatResult(exit_code=exit_code, stdout_tail=combined, error_kind=kind)

    # ------------------------------------------------------------------
    # classification + helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unpack_demux(res: Any) -> tuple[bytes | None, bytes | None]:
        """``exec_run(demux=True)`` returns ``(exit_code, (stdout, stderr))``.

        With ``demux=True`` the SDK puts the tuple in ``res.output``; older
        versions return a plain tuple. Normalise both.
        """
        output = getattr(res, "output", None)
        if output is None:
            return None, None
        if isinstance(output, tuple) and len(output) == 2:
            return output[0], output[1]
        # Non-demux fallback: SDK returned a single bytes blob.
        return output, None

    @staticmethod
    def _classify_error(
        exit_code: int, stdout_tail: str, stderr_tail: str
    ) -> DaemonErrorKind:
        """Map (exit_code, stdout, stderr) to a closed ``error_kind``.

        The classifier is conservative: only the patterns in
        ``_ERROR_PATTERNS`` (above) fire; anything else falls through to
        ``unknown``. New patterns can be added without changing the
        return type. Tests cover each pattern + the fallthrough.
        """
        haystack = (stdout_tail + "\n" + stderr_tail).lower()
        for needle, kind in _ERROR_PATTERNS:
            if needle in haystack:
                return kind
        # SIGSEGV / SIGKILL from the container layer surface as exit codes
        # outside the 0..255 normal range. Treat as hermes_crashed.
        if exit_code in (-9, 137, 139):
            return "hermes_crashed"
        return "unknown"

    @staticmethod
    def quote_argv(argv: list[str]) -> str:
        """Helper for logging: shell-quoted argv for ops-friendly diagnosis."""
        return " ".join(shlex.quote(a) for a in argv)


__all__ = ["HermesExec"]
