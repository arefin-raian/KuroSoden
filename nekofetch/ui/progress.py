from __future__ import annotations

import asyncio
import html

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from nekofetch.core.constants import BAR_EMPTY, BAR_FILLED
from nekofetch.localization.messages import M, t


def bar(percent: float, *, width: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(percent / 100 * width)
    return f"{BAR_FILLED * filled}{BAR_EMPTY * (width - filled)} {int(percent)}%"


def labeled(label: str, percent: float, *, width: int = 10) -> str:
    return f"{label}\n\n{bar(percent, width=width)}"


def labeled_html(label: str, percent: float, *, width: int = 10) -> str:
    return (
        f"<blockquote><b>{label}</b>\n\n"
        f"<b>{bar(percent, width=width)}</b></blockquote>"
    )


async def loading_animation(msg: Message, label: str, steps: int = 3, delay: float = 0.35) -> None:
    for i in range(1, steps + 1):
        try:
            await msg.edit_text(f"<b>{label}{'!' * i}</b>", parse_mode=ParseMode.HTML)
        except MessageNotModified:
            pass
        await asyncio.sleep(delay)


SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPINNER_DONE = "✓"  # settled frame so the loader never freezes mid-spin


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def animate_until(
    msg: Message,
    awaitable,
    render,
    *,
    cadence: float = 0.12,
):
    """Keep ``msg`` visibly alive (cycling spinner) until ``awaitable`` resolves.

    ``render(frame)`` returns the HTML caption for a given spinner frame. The
    awaited result is returned. Telegram flood/edit errors are swallowed — the
    animation is cosmetic and must never break the actual operation.

    The first frame is painted immediately (so even a sub-cadence operation shows
    the loader), the spinner ticks quickly, and on completion a settled ``✓`` frame
    is painted in a ``finally`` so the message never freezes on a random mid-cycle
    glyph if the awaitable finishes early, errors, or is cancelled.
    """
    task = asyncio.ensure_future(awaitable)
    frame = 0
    await _safe_edit(msg, render(SPINNER[0]))   # paint immediately
    try:
        while not task.done():
            await asyncio.sleep(cadence)
            frame += 1
            await _safe_edit(msg, render(SPINNER[frame % len(SPINNER)]))
        return await task
    finally:
        await _safe_edit(msg, render(SPINNER_DONE))


async def staged_loading(msg: Message, stages: list[str], delay_per_stage: float = 0.4) -> None:
    for stage in stages:
        for dots in range(1, 4):
            try:
                await msg.edit_text(f"<b>{stage}{'!' * dots}</b>", parse_mode=ParseMode.HTML)
            except MessageNotModified:
                pass
            await asyncio.sleep(delay_per_stage / 3)


def queue_block_html(
    *,
    anime_title: str,
    status: str,
    progress: float,
    speed_bps: float,
    eta_seconds: int | None,
    current_episode: int | None = None,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    job_id: int | None = None,
) -> str:
    bar_str = bar(progress)
    ep_line = f"\n<b>episode:</b> <b>S{current_episode:02d}</b>" if current_episode else ""
    size_line = ""
    if total_bytes > 0:
        size_line = (f"\n<b>size:</b> {human_bytes(downloaded_bytes)} / "
                     f"{human_bytes(total_bytes)}")
    id_line = f"  #{job_id}" if job_id else ""

    return (
        f"<blockquote>"
        f"📥 <b>{anime_title}</b>{id_line}"
        f"{ep_line}\n"
        f"<b>status:</b> {status}\n"
        f"<b>progress:</b> <b>{bar_str}</b>\n"
        f"<b>speed:</b> {human_speed(speed_bps)}\n"
        f"<b>eta:</b> {human_eta(eta_seconds)}"
        f"{size_line}"
        f"</blockquote>"
    )


def human_bytes(num: float, *, floor_unit: str = "MB") -> str:
    """Format byte count with a minimum display unit.

    By default the smallest unit shown is MB — raw B/KB values are promoted to
    ``0.0 MB`` so progress cards never read "16.0 B" when the actual figure is
    negligible or the value hasn't populated yet.
    """
    _UNITS = ("B", "KB", "MB", "GB", "TB")
    floor_idx = _UNITS.index(floor_unit) if floor_unit in _UNITS else 2
    for i, unit in enumerate(_UNITS):
        if abs(num) < 1024.0 or i == len(_UNITS) - 1:
            if i < floor_idx:
                # Value is below the floor unit — convert to the floor unit.
                while i < floor_idx:
                    num /= 1024.0
                    i += 1
                return f"{num:.1f} {_UNITS[floor_idx]}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_speed(bps: float) -> str:
    """Format speed with a minimum display unit of MB/s."""
    return f"{human_bytes(bps, floor_unit='MB')}/s"


def human_eta(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_elapsed(seconds: int | None) -> str:
    """Compact elapsed clock: MM:SS, or HH:MM:SS once past an hour."""
    if seconds is None or seconds < 0:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def download_card_html(
    *,
    title: str,
    job_id: int,
    status: str,
    progress: float = 0.0,
    stage: str | None = None,
    season: int | None = None,
    season_part: int | None = None,
    current_episode: int | None = None,
    episode_index: int | None = None,
    total_episodes: int | None = None,
    resolution: str | None = None,
    audio: str | None = None,
    speed_bps: float = 0.0,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    eta_seconds: int | None = None,
    elapsed_seconds: int | None = None,
    retry_attempt: int = 0,
    retry_max: int = 0,
    retry_reason: str | None = None,
    low_disk: bool = False,
) -> str:
    """Live Levi transfer card for download, retry, processing, and upload stages.

    Adapts the detail block based on the current stage:
    - **Download / Upload / Encoding**: full Transfer panel (speed, size, ETA, elapsed)
    - **Processing stages** (branding, watermarking, metadata, etc.): compact
      Progress panel (just progress bar + elapsed)
    """
    title_bits = [_esc(title)]
    if current_episode is not None:
        part = f"P{int(season_part):02d}" if season_part is not None else ""
        title_bits.append(
            f"[S{(season or 1):02d}{part}E{int(current_episode):02d}]"
        )
    if resolution:
        title_bits.append(f"[{_esc(resolution)}]")
    if audio:
        # Normalize underscores → spaces for display (e.g. DUAL_AUDIO → DUAL AUDIO)
        title_bits.append(f"[{_esc(audio.upper().replace('_', ' '))}]")

    if retry_attempt and retry_max:
        reason = f" · {_esc(retry_reason)}" if retry_reason else ""
        stage_label = f"Retrying {retry_attempt}/{retry_max}{reason}"
    elif stage and stage.lower() not in ("downloading", "download"):
        stage_label = _esc(stage).replace("_", " ").title()
    else:
        stage_label = "Downloading"

    elapsed = human_elapsed(elapsed_seconds)
    percent = int(max(0.0, min(100.0, progress)))
    filled = round(percent / 100 * 16)
    cells = "■" * filled + "□" * (16 - filled)
    warning = f"\n\n<blockquote>{t(M.DL_CARD_LOW_DISK)}</blockquote>" if low_disk else ""

    header = (
        f"<blockquote><b>📺 {' '.join(title_bits)} @AniXWeebs</b></blockquote>\n"
        f"<blockquote><b><u>‣ Status : </u></b><i><u>{stage_label}</u></i>\n"
        f"<b>[{cells}] {percent}%</b></blockquote>\n"
    )

    # Stages that have meaningful transfer stats (speed, size, ETA).
    _TRANSFER_STAGES = {
        "downloading", "download", "uploading", "upload",
        "encoding", "transcoding", "transcode", "compressing",
    }
    stage_key = (stage or "downloading").strip().lower()

    if stage_key in _TRANSFER_STAGES:
        # Full transfer panel
        if total_bytes > 0:
            total_str = human_bytes(total_bytes)
            total_val, total_unit = total_str.rsplit(" ", 1)
            dl_in_total_unit = downloaded_bytes
            for u in ("B", "KB", "MB", "GB", "TB"):
                if u == total_unit:
                    break
                dl_in_total_unit /= 1024.0
            size = f"{dl_in_total_unit:.1f} / {total_val} {total_unit}"
        elif downloaded_bytes > 0:
            size = human_bytes(downloaded_bytes)
        else:
            size = "—"
        speed = human_speed(speed_bps) if speed_bps > 0 else "—"
        eta = human_eta(eta_seconds) if eta_seconds is not None else "—"

        # During upload the transferred bytes are *sent*, not *downloaded* — flip
        # the label (and icon) accordingly. "Uploaded" is shorter than "Downloaded",
        # so it carries extra trailing spaces to keep the value column aligned with
        # the Speed/ETA/Elapsed rows below it.
        if stage_key in ("uploading", "upload"):
            xfer_row = f"<b>├─ 📤 Uploaded       </b> {size}\n"
        else:
            xfer_row = f"<b>├─ 📂 Downloaded  </b> {size}\n"

        detail = (
            f"<blockquote><b><u>Transfer</u>\n"
            f"├─ ⚡ Speed             </b> {speed}\n"
            f"{xfer_row}"
            f"<b>├─ ⌛ ETA                 </b> {eta}\n"
            f"<b>└─ ⏱️ Elapsed          </b> {elapsed}</blockquote>"
        )
    else:
        # Compact panel for processing stages (branding, watermarking, metadata,
        # verifying, thumbnail, rename, store, etc.)
        ep_info = ""
        if episode_index is not None and total_episodes is not None:
            ep_info = f"<b>├─ 📁 File                  </b> {episode_index} / {total_episodes}\n"
        detail = (
            f"<blockquote><b><u>Progress</u></b>\n"
            f"{ep_info}"
            f"<b>└─ ⏱️ Elapsed          </b> {elapsed}</blockquote>"
        )

    return f"{header}{detail}{warning}"
