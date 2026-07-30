"""FFmpeg transcoding: derive 720p / 480p and recompress oversized 1080p.

Quality is preserved via CRF (constant-quality) rather than fixed bitrates, and
every transcode keeps **all** original audio tracks (so dual-audio survives) and
subtitles. "Oversized" is judged per-minute so movies and long episodes get
proportionally larger budgets instead of a single hard cap.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.sources._hls import find_ffmpeg, find_ffprobe

log = get_logger(__name__)

# Derived-resolution CRFs (x264). Lower = better quality / bigger file.
_CRF = {1080: 21, 720: 21, 480: 22}

# 1080p "too big" budget. The example (≈23 min, >370 MB) ≈ 16 MB/min; we treat
# anything above this per-minute rate as oversized and recompress.
MB_PER_MIN_1080 = 16.0
OVERSIZE_FACTOR = 1.0  # recompress when size > budget (budget = rate * minutes)


def probe_duration_s(path: Path) -> float:
    ffprobe = find_ffprobe()
    if not ffprobe:
        return 0.0
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def is_oversized_1080(size_bytes: int, duration_s: float) -> bool:
    """True if a 1080p file exceeds its duration-scaled size budget."""
    if duration_s <= 0:
        return size_bytes > 370 * 1024 * 1024  # fall back to the flat example
    minutes = duration_s / 60
    budget = MB_PER_MIN_1080 * minutes * 1024 * 1024
    return size_bytes > budget * OVERSIZE_FACTOR


async def _run_ffmpeg(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.decode(errors='replace')[-300:]}")


async def _encode(src: Path, out: Path, height: int | None, crf: int,
                  preset: str = "medium") -> Path:
    """Re-encode video (x264 CRF); copy ALL audio + subtitles + attachments."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
        "-map", "0", "-c", "copy",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
    ]
    if height:
        cmd += ["-vf", f"scale=-2:{height}"]
    # MKV carries attachments fine; only drop data streams ffmpeg can't copy.
    cmd += ["-map", "-0:d?", str(out)]
    await _run_ffmpeg(cmd)
    return out


async def transcode_renditions(
    src: Path, out_dir: Path, stem: str, *, source_resolution: str | None = None,
    preset: str = "medium",
) -> dict:
    """Produce 720p + 480p, and a recompressed 1080p if the source is oversized.

    ``preset`` trades speed for compression efficiency (use a faster preset for
    tests, "medium"/"slow" in production). Returns a manifest of the outputs plus
    the oversize decision. The original file is left untouched.
    """
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    duration = probe_duration_s(src)
    size = src.stat().st_size  # noqa: ASYNC240
    src_h = None
    if source_resolution:
        try:
            src_h = int(source_resolution.rstrip("p"))
        except ValueError:
            src_h = None
    if not src_h:
        # detect height via ffprobe
        ffprobe = find_ffprobe()
        if ffprobe:
            r = subprocess.run(  # noqa: ASYNC221 - quick metadata probe
                [ffprobe, "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True,
            )
            try:
                src_h = int(r.stdout.strip())
            except (ValueError, AttributeError):
                src_h = 1080

    outputs: list[dict] = []
    mb_per_min = (size / 1048576) / (duration / 60) if duration else None

    async def make(height: int, label: str, crf: int) -> None:
        out = out_dir / f"{stem}.{label}.mkv"
        try:
            await _encode(src, out, height if height != src_h else None, crf, preset)
            outputs.append({"label": label, "height": height, "crf": crf,
                            "path": str(out), "size_mb": round(out.stat().st_size / 1048576, 1)})
        except Exception as exc:  # noqa: BLE001
            outputs.append({"label": label, "height": height, "error": str(exc)})

    # 720p + 480p always (only if source is taller).
    if (src_h or 1080) > 720:
        await make(720, "720p", _CRF[720])
    if (src_h or 1080) > 480:
        await make(480, "480p", _CRF[480])

    # Recompress 1080p only when the source is 1080p-ish AND oversized.
    oversized = (src_h or 0) >= 1080 and is_oversized_1080(size, duration)
    if oversized:
        out = out_dir / f"{stem}.1080p.x264.mkv"
        try:
            await _encode(src, out, None, _CRF[1080], preset)
            outputs.append({"label": "1080p-recompress", "height": 1080, "crf": _CRF[1080],
                            "path": str(out), "size_mb": round(out.stat().st_size / 1048576, 1)})
        except Exception as exc:  # noqa: BLE001
            outputs.append({"label": "1080p-recompress", "error": str(exc)})

    return {
        "source_height": src_h,
        "duration_s": round(duration, 1),
        "source_size_mb": round(size / 1048576, 1),
        "mb_per_min": round(mb_per_min, 2) if mb_per_min else None,
        "oversized_1080": oversized,
        "renditions": outputs,
    }
