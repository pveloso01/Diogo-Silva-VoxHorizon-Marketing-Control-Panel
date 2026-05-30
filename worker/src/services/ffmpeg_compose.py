"""Local ffmpeg video compose.

Assembles a finished vertical (9:16) ad MP4 entirely in-container with the
ffmpeg that ships in the worker image (worker/Dockerfile): concatenate N
generated clips (each normalized to the target frame), lay the voiceover audio
track (optionally with a ducked background-music bed), optionally overlay a
brand logo, and encode to a Meta-ready H.264 MP4. No hosted compose vendor and
no spend -- the clips come from the kie video client (services.kie_video); this
module just stitches them.

Two halves, split so the command is testable without running ffmpeg:

  * :func:`build_compose_argv` -- a PURE function that returns the exact ffmpeg
    argv (inputs + filter_complex + maps + encode flags). Unit-tested directly.
  * :func:`compose` -- the async runner: locate ffmpeg, validate inputs, shell
    out via ``asyncio.create_subprocess_exec`` (mirrors
    services.image_compositor), and verify the output landed. Failures raise
    :class:`ComposeError`; a missing ffmpeg raises ``RuntimeError`` (route -> 503).

Captions are NOT burned in here -- that is the next step (VID-4, a Whisper ->
ASS/SRT -> ffmpeg burn-in pass). This module is dormant until the video.py
substage chain wires it (VID-5).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog


log = structlog.get_logger(__name__)


# 9:16 vertical, 1080x1920, 30fps is the short-form-ad default.
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30
# Background music sits well under the voiceover; -12 dB is a safe default bed.
DEFAULT_MUSIC_GAIN_DB = -12.0


class ComposeError(RuntimeError):
    """Raised when ffmpeg exits non-zero or writes no output."""


@dataclass(frozen=True)
class ComposeResult:
    """Outcome of one :func:`compose` call."""

    output_path: Path
    raw_stderr: str


def build_compose_argv(
    *,
    clips: Sequence[str | Path],
    output: str | Path,
    voiceover: str | Path | None = None,
    music: str | Path | None = None,
    logo: str | Path | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    music_gain_db: float = DEFAULT_MUSIC_GAIN_DB,
    ffmpeg_bin: str = "ffmpeg",
    overwrite: bool = True,
) -> list[str]:
    """Build the ffmpeg argv to compose ``clips`` (+ audio/logo) into one MP4.

    Each clip is scaled to fit inside ``width`` x ``height`` preserving its aspect
    ratio, padded to the exact frame (letterbox), SAR-normalized and fps-locked,
    then the N normalized streams are concatenated. Audio: voiceover alone passes
    through; voiceover + music mixes the music ducked by ``music_gain_db`` under
    the voiceover; music alone is laid at the reduced gain. An optional ``logo``
    is overlaid top-right. Output is H.264 / yuv420p / AAC, faststart, and
    ``-shortest`` when there is an audio track so video and audio end together.

    Pure: builds and returns the argv, runs nothing. Raises :class:`ComposeError`
    only on invalid inputs (no clips).
    """
    clip_list = list(clips)
    if not clip_list:
        raise ComposeError("compose requires at least one clip")

    argv: list[str] = [ffmpeg_bin or "ffmpeg"]
    if overwrite:
        argv.append("-y")

    # Inputs: clips first (indices 0..n-1), then optional audio/logo inputs.
    for c in clip_list:
        argv += ["-i", str(c)]
    n = len(clip_list)
    idx = n
    vo_idx = music_idx = logo_idx = None
    if voiceover is not None:
        argv += ["-i", str(voiceover)]
        vo_idx = idx
        idx += 1
    if music is not None:
        argv += ["-i", str(music)]
        music_idx = idx
        idx += 1
    if logo is not None:
        argv += ["-i", str(logo)]
        logo_idx = idx
        idx += 1

    parts: list[str] = []

    # Normalize + concat the video clips.
    vlabels: list[str] = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps}[v{i}]"
        )
        vlabels.append(f"[v{i}]")
    parts.append("".join(vlabels) + f"concat=n={n}:v=1:a=0[vcat]")

    video_out = "[vcat]"
    if logo_idx is not None:
        parts.append(f"[vcat][{logo_idx}:v]overlay=W-w-40:40[vout]")
        video_out = "[vout]"

    # Audio: always resolve to a single [aout] label so the map is uniform.
    audio_out: str | None = None
    if vo_idx is not None and music_idx is not None:
        parts.append(f"[{music_idx}:a]volume={music_gain_db}dB[mduck]")
        parts.append(
            f"[{vo_idx}:a][mduck]amix=inputs=2:duration=longest:"
            f"dropout_transition=2[aout]"
        )
        audio_out = "[aout]"
    elif vo_idx is not None:
        parts.append(f"[{vo_idx}:a]anull[aout]")
        audio_out = "[aout]"
    elif music_idx is not None:
        parts.append(f"[{music_idx}:a]volume={music_gain_db}dB[aout]")
        audio_out = "[aout]"

    argv += ["-filter_complex", ";".join(parts), "-map", video_out]
    if audio_out is not None:
        argv += ["-map", audio_out, "-c:a", "aac", "-b:a", "192k"]
    argv += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-movflags",
        "+faststart",
    ]
    if audio_out is not None:
        argv.append("-shortest")
    argv.append(str(output))
    return argv


async def compose(
    *,
    clips: Sequence[str | Path],
    output: str | Path,
    voiceover: str | Path | None = None,
    music: str | Path | None = None,
    logo: str | Path | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    music_gain_db: float = DEFAULT_MUSIC_GAIN_DB,
    ffmpeg_bin: str | None = None,
    timeout_s: float = 600.0,
) -> ComposeResult:
    """Compose ``clips`` (+ optional voiceover / music / logo) into one MP4.

    Locates ffmpeg (``ffmpeg_bin`` or PATH), validates the inputs exist, runs the
    :func:`build_compose_argv` command, and confirms the output was written.

    Raises:
        RuntimeError: ffmpeg not found (the worker image ships it; this fires
            only in a stripped environment -- route translates to 503).
        ComposeError: no clips, a missing input file, ffmpeg exited non-zero, or
            ffmpeg reported success but wrote nothing.
    """
    resolved_bin = ffmpeg_bin or shutil.which("ffmpeg")
    if not resolved_bin:
        raise RuntimeError(
            "ffmpeg not found on PATH -- it ships in the worker image; install "
            "ffmpeg to compose locally."
        )

    clip_list = list(clips)
    if not clip_list:
        raise ComposeError("compose requires at least one clip")
    for src in [*clip_list, voiceover, music, logo]:
        if src is not None and not Path(src).exists():
            raise ComposeError(f"compose input does not exist: {src}")

    argv = build_compose_argv(
        clips=clip_list,
        output=output,
        voiceover=voiceover,
        music=music,
        logo=logo,
        width=width,
        height=height,
        fps=fps,
        music_gain_db=music_gain_db,
        ffmpeg_bin=resolved_bin,
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as e:
        # A wedged ffmpeg would otherwise hang the awaiting work_item forever and
        # keep burning CPU. Kill it + reap before surfacing the failure.
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001 -- best-effort reap
            pass
        raise ComposeError(f"ffmpeg timed out after {timeout_s:.0f}s") from e
    stderr = err_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        log.warning(
            "ffmpeg_compose_failed",
            returncode=proc.returncode,
            clips=len(clip_list),
            stderr_tail=stderr[-800:],
        )
        raise ComposeError(
            f"ffmpeg exited {proc.returncode}: "
            f"{stderr.strip()[-500:] or 'no stderr'}"
        )

    out_path = Path(output)
    if not out_path.exists():
        raise ComposeError(
            f"ffmpeg reported success but {out_path} was not written"
        )

    log.info(
        "ffmpeg_compose_ok",
        output=str(out_path),
        clips=len(clip_list),
        has_voiceover=voiceover is not None,
        has_music=music is not None,
        bytes=out_path.stat().st_size,
    )
    return ComposeResult(output_path=out_path, raw_stderr=stderr)
