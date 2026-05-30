"""Tests for the local ffmpeg compose service.

The argv builder is pure, so it is asserted directly. The async runner is driven
with a mocked ``asyncio.create_subprocess_exec`` + ``shutil.which`` so no real
ffmpeg binary is needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.services import ffmpeg_compose as comp_mod
from src.services.ffmpeg_compose import (
    ComposeError,
    build_compose_argv,
    compose,
)


# ---------------------------------------------------------------------------
# argv helpers
# ---------------------------------------------------------------------------


def _arg_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _inputs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]


def _maps(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]


# ---------------------------------------------------------------------------
# build_compose_argv (pure)
# ---------------------------------------------------------------------------


def test_build_argv_clips_only() -> None:
    argv = build_compose_argv(clips=["a.mp4", "b.mp4"], output="out.mp4")
    assert argv[0] == "ffmpeg"
    assert "-y" in argv
    assert _inputs(argv) == ["a.mp4", "b.mp4"]
    fc = _arg_after(argv, "-filter_complex")
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in fc
    assert "concat=n=2:v=1:a=0[vcat]" in fc
    assert _maps(argv) == ["[vcat]"]
    assert "libx264" in argv
    assert "+faststart" in argv
    assert "-shortest" not in argv  # no audio track
    assert argv[-1] == "out.mp4"


def test_build_argv_with_voiceover() -> None:
    argv = build_compose_argv(clips=["a.mp4"], output="o.mp4", voiceover="vo.m4a")
    assert _inputs(argv) == ["a.mp4", "vo.m4a"]
    fc = _arg_after(argv, "-filter_complex")
    assert "[1:a]anull[aout]" in fc
    assert _maps(argv) == ["[vcat]", "[aout]"]
    assert "aac" in argv
    assert "-shortest" in argv


def test_build_argv_with_voiceover_and_music() -> None:
    argv = build_compose_argv(
        clips=["a.mp4"], output="o.mp4", voiceover="vo.m4a", music="m.mp3"
    )
    # inputs: clip(0), voiceover(1), music(2)
    assert _inputs(argv) == ["a.mp4", "vo.m4a", "m.mp3"]
    fc = _arg_after(argv, "-filter_complex")
    assert "[2:a]volume=-12.0dB[mduck]" in fc
    assert "[1:a][mduck]amix=inputs=2:duration=longest:dropout_transition=2[aout]" in fc
    assert _maps(argv) == ["[vcat]", "[aout]"]


def test_build_argv_with_logo_overlay() -> None:
    argv = build_compose_argv(clips=["a.mp4"], output="o.mp4", logo="logo.png")
    assert _inputs(argv) == ["a.mp4", "logo.png"]
    fc = _arg_after(argv, "-filter_complex")
    assert "[vcat][1:v]overlay=W-w-40:40[vout]" in fc
    assert _maps(argv) == ["[vout]"]


def test_build_argv_empty_clips_raises() -> None:
    with pytest.raises(ComposeError, match="at least one clip"):
        build_compose_argv(clips=[], output="o.mp4")


def test_build_argv_custom_dims_and_fps() -> None:
    argv = build_compose_argv(
        clips=["a.mp4"], output="o.mp4", width=720, height=1280, fps=24
    )
    fc = _arg_after(argv, "-filter_complex")
    assert "scale=720:1280:force_original_aspect_ratio=decrease" in fc
    assert "fps=24" in fc
    assert _arg_after(argv, "-r") == "24"


def test_build_argv_threads_ffmpeg_bin() -> None:
    argv = build_compose_argv(
        clips=["a.mp4"], output="o.mp4", ffmpeg_bin="/usr/bin/ffmpeg"
    )
    assert argv[0] == "/usr/bin/ffmpeg"


# ---------------------------------------------------------------------------
# compose (async runner, mocked subprocess)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)


def test_compose_runs_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    out = tmp_path / "out.mp4"

    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    captured: dict = {}

    async def fake_exec(*argv, **kwargs):  # noqa: ANN002, ANN003
        captured["argv"] = list(argv)
        out.write_bytes(b"VIDEO")  # simulate ffmpeg writing the output
        return _FakeProc(0, b"")

    monkeypatch.setattr(comp_mod.asyncio, "create_subprocess_exec", fake_exec)

    res = asyncio.run(compose(clips=[clip], output=out))
    assert res.output_path == out
    assert out.read_bytes() == b"VIDEO"
    # The resolved ffmpeg binary was threaded into the argv.
    assert captured["argv"][0] == "/usr/bin/ffmpeg"


def test_compose_raises_when_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        asyncio.run(compose(clips=[clip], output=tmp_path / "o.mp4"))


def test_compose_nonzero_exit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    async def fake_exec(*argv, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(1, b"boom: bad filter")

    monkeypatch.setattr(comp_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(ComposeError, match="exited 1"):
        asyncio.run(compose(clips=[clip], output=tmp_path / "o.mp4"))


def test_compose_kills_ffmpeg_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    state: dict = {"killed": False, "waited": False}

    class _HangProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return (b"", b"")

        def kill(self) -> None:
            state["killed"] = True

        async def wait(self) -> int:
            state["waited"] = True
            return -9

    async def fake_exec(*argv, **kwargs):  # noqa: ANN002, ANN003
        return _HangProc()

    monkeypatch.setattr(comp_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(ComposeError, match="timed out"):
        asyncio.run(
            compose(clips=[clip], output=tmp_path / "o.mp4", timeout_s=0.01)
        )
    # A wedged ffmpeg must be killed + reaped, not left hanging the work_item.
    assert state["killed"] is True
    assert state["waited"] is True


def test_compose_missing_input_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    missing = tmp_path / "nope.mp4"  # never created
    with pytest.raises(ComposeError, match="does not exist"):
        asyncio.run(compose(clips=[missing], output=tmp_path / "o.mp4"))


def test_compose_success_but_no_output_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(comp_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    async def fake_exec(*argv, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(0, b"")  # exits 0 but writes nothing

    monkeypatch.setattr(comp_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(ComposeError, match="not written"):
        asyncio.run(compose(clips=[clip], output=tmp_path / "out.mp4"))
