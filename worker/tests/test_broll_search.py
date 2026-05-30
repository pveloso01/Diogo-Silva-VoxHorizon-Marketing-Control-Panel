"""Tests for the yt-dlp b-roll scraper wrapper.

yt-dlp itself is mocked via ``asyncio.create_subprocess_exec``; we stage a
temp directory with fake ``.info.json`` sidecars and ``.mp4`` files so
``collect_candidates`` runs against realistic on-disk inputs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("WORKER_SHARED_SECRET", "x")
    monkeypatch.setenv("WORKER_CORS_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("WORKER_PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("BROLL_STORE_BACKEND", "local")
    monkeypatch.setenv("BROLL_LOCAL_ROOT", str(tmp_path))
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stage_candidate(
    root: Path, *, video_id: str, ext: str = "mp4", **info: object
) -> tuple[Path, Path]:
    """Drop ``{id}.mp4`` + ``{id}.info.json`` into a temp dir."""
    video = root / f"{video_id}.{ext}"
    sidecar = root / f"{video_id}.info.json"
    video.write_bytes(b"FAKEVIDEO")
    payload = {
        "id": video_id,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "duration": 30,
        "width": 1080,
        "height": 1920,
        **info,
    }
    sidecar.write_text(json.dumps(payload))
    return video, sidecar


# ---------------------------------------------------------------------------
# collect_candidates — pure
# ---------------------------------------------------------------------------


def test_collect_candidates_pairs_info_json_with_video(tmp_path: Path) -> None:
    from src.services.broll_search import collect_candidates

    _stage_candidate(tmp_path, video_id="abc")
    _stage_candidate(tmp_path, video_id="def")

    out = collect_candidates(tmp_path)
    assert len(out) == 2
    ids = sorted(c.local_path.stem for c in out)
    assert ids == ["abc", "def"]
    a = next(c for c in out if c.local_path.stem == "abc")
    assert a.source_url == "https://www.youtube.com/watch?v=abc"
    assert a.duration_s == 30.0
    assert a.dimensions == "1080x1920"
    assert a.video_id == "abc"


def test_collect_candidates_prefers_mp4_over_webm(tmp_path: Path) -> None:
    from src.services.broll_search import collect_candidates

    # Stage one id with both .mp4 AND .webm; sidecar names the id.
    vid = tmp_path / "x.mp4"
    vid.write_bytes(b"M")
    webm = tmp_path / "x.webm"
    webm.write_bytes(b"W")
    (tmp_path / "x.info.json").write_text(
        json.dumps({"id": "x", "webpage_url": "u", "duration": 10, "width": 1, "height": 1})
    )

    out = collect_candidates(tmp_path)
    assert len(out) == 1
    assert out[0].local_path.suffix == ".mp4"


def test_collect_candidates_skips_orphans(tmp_path: Path) -> None:
    """Sidecar without a video file is skipped silently."""
    from src.services.broll_search import collect_candidates

    (tmp_path / "lonely.info.json").write_text(json.dumps({"id": "lonely"}))
    out = collect_candidates(tmp_path)
    assert out == []


def test_collect_candidates_skips_invalid_json(tmp_path: Path) -> None:
    from src.services.broll_search import collect_candidates

    (tmp_path / "broken.info.json").write_text("not json")
    out = collect_candidates(tmp_path)
    assert out == []


# ---------------------------------------------------------------------------
# scrape_yt_shorts — subprocess invocation
# ---------------------------------------------------------------------------


def test_scrape_yt_shorts_raises_when_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import broll_search

    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(broll_search.scrape_yt_shorts("hello"))
    assert "yt-dlp" in str(exc.value)


def test_scrape_yt_shorts_invokes_yt_dlp_with_search_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import broll_search

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        # Drop a single candidate in the configured tmp dir so the result
        # collection step has something to find.
        target_dir = Path(args[args.index("-o") + 1]).parent
        _stage_candidate(target_dir, video_id="seg0")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    out = asyncio.run(
        broll_search.scrape_yt_shorts(
            "texas roof drone", count=3, tmp_root=tmp_path / "scrape"
        )
    )
    assert len(out) == 1
    assert out[0].source_url.endswith("v=seg0")

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/yt-dlp"
    # The search target is the last arg.
    assert cmd[-1] == "ytsearch3:texas roof drone"
    # We always write info json.
    assert "--write-info-json" in cmd
    # Output template is parameterized with tmp dir.
    assert "-o" in cmd


def test_scrape_yt_shorts_returns_empty_when_count_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short-circuit before invoking the binary."""
    from src.services import broll_search

    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    async def boom(*_a, **_k):
        raise AssertionError("should not invoke yt-dlp when count=0")

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", boom)
    out = asyncio.run(broll_search.scrape_yt_shorts("query", count=0))
    assert out == []


def test_scrape_yt_shorts_raises_when_yt_dlp_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import broll_search

    async def fake_exec(*_a, **_k):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"network down"))
        return proc

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            broll_search.scrape_yt_shorts("q", count=1, tmp_root=tmp_path)
        )
    assert "network down" in str(exc.value)


def test_scrape_yt_shorts_raises_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import broll_search

    captured: dict[str, MagicMock] = {}

    async def fake_exec(*_a, **_k):
        proc = MagicMock()
        proc.returncode = 0

        async def slow(*_a, **_k):
            await asyncio.sleep(10)
            return (b"", b"")

        proc.communicate = slow
        proc.wait = AsyncMock()
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            broll_search.scrape_yt_shorts(
                "q", count=1, tmp_root=tmp_path, timeout_s=0.01
            )
        )
    assert "timed out" in str(exc.value)
    # The orphaned yt-dlp must be killed + reaped, not left running.
    captured["proc"].kill.assert_called_once()
    captured["proc"].wait.assert_awaited_once()


# ---------------------------------------------------------------------------
# BrollCandidate property edge cases
# ---------------------------------------------------------------------------


def test_broll_candidate_duration_none_when_missing(tmp_path: Path) -> None:
    """If `duration` isn't an int/float, the property returns None."""
    from src.services.broll_search import BrollCandidate

    cand = BrollCandidate(
        source_url="u",
        local_path=tmp_path / "x.mp4",
        info={"duration": "huh", "width": 1, "height": 2},
    )
    assert cand.duration_s is None


def test_broll_candidate_dimensions_none_when_missing(tmp_path: Path) -> None:
    """If width/height aren't ints, the property returns None."""
    from src.services.broll_search import BrollCandidate

    cand = BrollCandidate(
        source_url="u",
        local_path=tmp_path / "x.mp4",
        info={"width": "1080", "height": 1920},
    )
    assert cand.dimensions is None


def test_broll_candidate_video_id_none_for_non_string(tmp_path: Path) -> None:
    from src.services.broll_search import BrollCandidate

    cand = BrollCandidate(
        source_url="u",
        local_path=tmp_path / "x.mp4",
        info={"id": 42},
    )
    assert cand.video_id is None


def test_scrape_yt_shorts_uses_provided_tmp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `tmp_root` is honoured (not a fresh mkdtemp)."""
    from src.services import broll_search

    captured: dict = {}
    custom_dir = tmp_path / "my-custom-dir"

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        target_dir = Path(args[args.index("-o") + 1]).parent
        # Verify the worker honoured our tmp_root by checking the -o template.
        captured["target_dir"] = target_dir
        _stage_candidate(target_dir, video_id="x")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    out = asyncio.run(
        broll_search.scrape_yt_shorts(
            "needle", count=1, tmp_root=custom_dir
        )
    )
    assert len(out) == 1
    # The dir was created at the resolved location.
    assert captured["target_dir"].resolve() == custom_dir.resolve()


def test_collect_candidates_skips_non_dict_json(tmp_path: Path) -> None:
    """An `info.json` whose contents is a scalar (not a dict) is skipped."""
    from src.services.broll_search import collect_candidates

    (tmp_path / "scalar.info.json").write_text("[1, 2, 3]")  # valid JSON, not a dict
    out = collect_candidates(tmp_path)
    assert out == []


def test_scrape_yt_shorts_creates_tmp_root_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the caller omits tmp_root, we call mkdtemp() to allocate one."""
    from src.services import broll_search

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        target_dir = Path(args[args.index("-o") + 1]).parent
        captured["target_dir"] = target_dir
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr(broll_search.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(broll_search.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    # Intercept tempfile.mkdtemp so the test doesn't litter /tmp.
    custom_dir = str(tmp_path / "synthetic-tmp")
    Path(custom_dir).mkdir()
    monkeypatch.setattr(broll_search.tempfile, "mkdtemp", lambda prefix=None: custom_dir)

    out = asyncio.run(broll_search.scrape_yt_shorts("query", count=1))
    assert out == []  # no files staged
    assert captured["target_dir"] == Path(custom_dir)
