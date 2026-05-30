"""B-roll candidate scraper.

V2-4 ships this as the thin wrapper around ``yt-dlp`` that the
``/work/video/broll-search`` route calls. The upstream marketing-dept repo
has ``scrape_broll.py`` with yt-dlp + Apify fallback; we deliberately keep
the worker version minimal (yt-dlp only) and let operators reach for the
fallback by re-running with a different ``broll_query``.

Returned :class:`BrollCandidate` objects are NOT stored anywhere by this
module — the route picks each one up and feeds it through ``LocalBrollStore``
which handles content-hash dedup, sidecar metadata, and signed URLs.

This module never touches Supabase, the BrollStore, or the queue. It's a
pure scrape transport.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Cap on candidates per query. We pick ~5 in v1 (matches the operator UI
# in V2-18). Higher counts blow yt-dlp's rate limit budget fast.
DEFAULT_PER_QUERY = 5

# Output template used by yt-dlp. ``--write-info-json`` drops a sidecar
# JSON file beside each download so we can read duration / dimensions
# without spawning ffprobe.
_OUTPUT_TEMPLATE = "%(id)s.%(ext)s"


@dataclass(frozen=True)
class BrollCandidate:
    """One scraped clip ready to feed into the BrollStore.

    ``source_url`` is the public watch URL (YouTube / TikTok) we scraped
    from; ``local_path`` is the MP4 file on disk. ``info`` is the raw
    yt-dlp ``-J`` payload so the route can persist whatever metadata it
    wants without re-running yt-dlp.
    """

    source_url: str
    local_path: Path
    info: dict[str, Any]

    @property
    def duration_s(self) -> float | None:
        d = self.info.get("duration")
        if isinstance(d, (int, float)):
            return float(d)
        return None

    @property
    def dimensions(self) -> str | None:
        w = self.info.get("width")
        h = self.info.get("height")
        if isinstance(w, int) and isinstance(h, int):
            return f"{w}x{h}"
        return None

    @property
    def video_id(self) -> str | None:
        v = self.info.get("id")
        return str(v) if isinstance(v, str) else None


# ---------------------------------------------------------------------------
# yt-dlp invocation
# ---------------------------------------------------------------------------


def _resolve_yt_dlp_binary(yt_dlp_binary: str | None = None) -> str:
    """Locate ``yt-dlp``. Raise loudly if missing."""
    binary = yt_dlp_binary or shutil.which("yt-dlp")
    if binary is None:
        raise RuntimeError(
            "yt-dlp not found on PATH — b-roll scraping is unavailable. "
            "Install with `pip install yt-dlp` (or `brew install yt-dlp`)."
        )
    return binary


async def scrape_yt_shorts(
    query: str,
    *,
    count: int = DEFAULT_PER_QUERY,
    tmp_root: Path | None = None,
    yt_dlp_binary: str | None = None,
    timeout_s: float = 120.0,
) -> list[BrollCandidate]:
    """Scrape up to ``count`` short clips matching ``query`` and return
    :class:`BrollCandidate` objects (one per downloaded clip).

    Uses the ``ytsearch<N>:<query>`` syntax built into yt-dlp; downloads
    the top-N results into a fresh temp dir, and pairs each MP4 with its
    sidecar ``.info.json`` for metadata.

    The function is async because it shells out via
    ``asyncio.create_subprocess_exec`` — the FastAPI event loop stays
    responsive while the scraper runs.
    """
    if count <= 0:
        return []

    binary = _resolve_yt_dlp_binary(yt_dlp_binary)

    if tmp_root is None:
        # Fresh dir per call. Caller is responsible for moving files into
        # the BrollStore — we don't clean up here so debugging is easy.
        tmp_root = Path(tempfile.mkdtemp(prefix="vox-broll-"))
    else:
        tmp_root = Path(tmp_root).expanduser().resolve()
        tmp_root.mkdir(parents=True, exist_ok=True)

    search_target = f"ytsearch{count}:{query}"
    cmd: list[str] = [
        binary,
        "--no-warnings",
        "--no-playlist",
        "-f",
        # mp4 first, then any progressive download we can transcode-free.
        "best[ext=mp4]/best",
        "-o",
        str(tmp_root / _OUTPUT_TEMPLATE),
        "--write-info-json",
        # Cap clip length to 90s — Shorts are usually <60s anyway.
        "--match-filter",
        "duration<=90",
        search_target,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError as e:
        # Kill the orphaned yt-dlp (and its ffmpeg / network children) before
        # raising. Without this the process keeps running detached after we stop
        # awaiting it; repeated scrape timeouts then pile up processes + FDs and
        # (with no host-side PID cap) can exhaust the box.
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001 -- best-effort reap; never mask the timeout
            pass
        raise RuntimeError(
            f"yt-dlp timed out after {timeout_s:.0f}s for query: {query!r}"
        ) from e

    if proc.returncode != 0:
        err = err_b.decode("utf-8", errors="replace")
        out = out_b.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"yt-dlp exited {proc.returncode} for query {query!r}: "
            f"{err.strip() or out.strip()}"
        )

    return collect_candidates(tmp_root)


def collect_candidates(tmp_dir: Path) -> list[BrollCandidate]:
    """Pair every ``*.info.json`` with its sibling video file.

    Exported separately so tests can stage a directory by hand.
    """
    tmp_dir = Path(tmp_dir)
    candidates: list[BrollCandidate] = []

    for info_path in sorted(tmp_dir.glob("*.info.json")):
        try:
            info = json.loads(info_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(info, dict):
            continue

        # Find the matching video file — yt-dlp drops .info.json next to
        # ``<id>.<ext>`` so we strip the ``.info.json`` suffix and look
        # for any extension on disk.
        stem = info_path.name.removesuffix(".info.json")
        video_files = sorted(p for p in tmp_dir.glob(f"{stem}.*") if p.suffix != ".json")
        if not video_files:
            continue
        # Prefer mp4 if both .mp4 and .webm exist.
        mp4s = [p for p in video_files if p.suffix.lower() == ".mp4"]
        local_path = mp4s[0] if mp4s else video_files[0]

        source_url = info.get("webpage_url") or info.get("original_url") or ""
        candidates.append(
            BrollCandidate(
                source_url=str(source_url),
                local_path=local_path,
                info=info,
            )
        )
    return candidates


__all__ = [
    "BrollCandidate",
    "DEFAULT_PER_QUERY",
    "scrape_yt_shorts",
    "collect_candidates",
]
