"""Unit test for the scheduler's temp-dir reaper (post-incident disk hygiene).

``run_temp_reaper_once`` deletes stale ``vox-*`` temp entries left by the
yt-dlp / ffmpeg / probe subprocess helpers so a long-lived worker can't slowly
fill the host disk. Recent entries (possibly in active use) are kept; non-``vox``
entries are never touched.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from src.config import Settings
from src.services import scheduler


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "worker_shared_secret": "test",
        "scheduler_temp_reaper_max_age_s": 3600,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


def test_temp_reaper_removes_stale_vox_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "vox-broll-old"
    stale.mkdir()
    (stale / "clip.mp4").write_bytes(b"x")
    fresh = tmp_path / "vox-compose-new"
    fresh.mkdir()
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()

    # Age the stale dir well past the 1h max; fresh + unrelated stay at ~now.
    old = time.time() - 7200
    os.utime(stale, (old, old))

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    result = asyncio.run(scheduler.run_temp_reaper_once(_settings()))

    assert result["removed"] == 1
    assert not stale.exists()  # stale vox-* removed
    assert fresh.exists()  # recent vox-* kept (may be in active use)
    assert unrelated.exists()  # non-vox entry untouched


def test_temp_reaper_noop_when_nothing_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vox-fresh").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    result = asyncio.run(scheduler.run_temp_reaper_once(_settings()))
    assert result["removed"] == 0
    assert (tmp_path / "vox-fresh").exists()
