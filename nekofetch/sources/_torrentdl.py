"""Fast torrent download via aria2c (multi-connection BitTorrent + DHT).

aria2c is the fastest practical option without compiling libtorrent: a single
static binary that does parallel piece fetching across many peers, DHT, and
selective single-file downloads (so a test can grab just EP1 from a batch).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.sources.base import ProgressCallback

log = get_logger(__name__)

# Well-seeded public trackers added on top of the torrent's own — improves peer
# discovery and start-up speed for popular releases.
_EXTRA_TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "http://nyaa.tracker.wf:7777/announce",
])

_PROGRESS_RE = re.compile(r"\((\d+)%\)")


def find_aria2() -> str | None:
    found = shutil.which("aria2c") or shutil.which("aria2c.exe")
    if found:
        return found
    for base in (Path(__file__).resolve().parents[3], Path.cwd()):
        for name in ("aria2c.exe", "aria2c"):
            cand = base / "tools" / name
            if cand.exists():
                return str(cand)
    return None


async def download_torrent_file(
    info: dict,
    dest: Path,
    *,
    on_progress: ProgressCallback | None = None,
    max_seconds: int = 1800,
    stop_idle: int = 180,
) -> dict:
    """Download a single file from a torrent and place it at ``dest``.

    ``info`` carries ``torrent_url``, ``file_index`` (1-based, bencode order),
    ``path`` and ``name``. Returns the downloaded file path + stats. Raises on
    failure so callers can react.
    """
    aria2 = find_aria2()
    if not aria2:
        raise RuntimeError("aria2c not found (expected on PATH or in tools/)")

    work = dest.parent
    work.mkdir(parents=True, exist_ok=True)

    # Acquire the .torrent file — from a local path, a remote URL, or a magnet.
    torrent_path = work / ".release.torrent"
    magnet_uri = info.get("magnet")

    if info.get("torrent_path"):
        src = Path(info["torrent_path"])
        if src.exists():
            shutil.copy2(str(src), str(torrent_path))
        else:
            raise RuntimeError(f"provided .torrent not found: {src}")
    elif magnet_uri:
        pass  # magnet URIs are passed directly to aria2c (no .torrent file)
    else:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            tr = await c.get(info["torrent_url"])
            tr.raise_for_status()
        torrent_path.write_bytes(tr.content)

    # aria2c downloads to a temp name; we rename to `dest` after completion.
    out_name = info["name"]
    cmd = [
        aria2,
        "--dir", str(work),
        "--select-file", str(info["file_index"]),
        f"--index-out={info['file_index']}={out_name}",
        "--seed-time=0",
        f"--bt-stop-timeout={stop_idle}",
        "--max-connection-per-server=16",
        "--split=16",
        "--bt-max-peers=200",
        "--bt-request-peer-speed-limit=100M",
        "--enable-dht=true",
        "--dht-listen-port=6881-6999",
        "--listen-port=6881-6999",
        f"--bt-tracker={_EXTRA_TRACKERS}",
        "--summary-interval=2",
        "--console-log-level=warn",
        "--bt-save-metadata=false",
        "--allow-overwrite=true",
        magnet_uri if magnet_uri else str(torrent_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    last_pct = -1
    file_length = int(info.get("length") or 0)

    async def pump() -> None:
        nonlocal last_pct
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            m = _PROGRESS_RE.search(line)
            if m and on_progress:
                pct = int(m.group(1))
                if pct != last_pct:
                    last_pct = pct
                    if file_length:
                        await on_progress(pct * file_length // 100, file_length)
                    else:
                        await on_progress(pct, 100)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=max_seconds)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"torrent download timed out after {max_seconds}s") from None

    if proc.returncode != 0:
        raise RuntimeError(f"aria2c exited {proc.returncode}")

    # Locate the downloaded file — --index-out flattens to work/<name>, but
    # fall back to a recursive search if aria2c placed it in the torrent's
    # original directory structure instead.
    out = work / out_name
    if not out.exists():
        matches = list(work.rglob(info["name"]))
        if not matches:
            raise RuntimeError(f"downloaded file not found: {info['name']}")
        out = matches[0]

    # Move the file to the structured `dest` path that the download service
    # expects (e.g. S01E001_1080p_dual_audio.mkv).  This ensures the
    # MediaFile.local_path stored in the DB points to an existing file so the
    # processing pipeline can find it.
    if out != dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), str(dest))
        out = dest

    # Clean up aria2c artifacts.
    torrent_path.unlink(missing_ok=True)
    aria_ctrl = dest.with_name(dest.name + ".aria2")
    aria_ctrl.unlink(missing_ok=True)

    # Remove the torrent's original directory structure — aria2c creates it as
    # a side effect of piece-boundary alignment even with --select-file.  Walk
    # top-level subdirectories of the work folder; only remove dirs (never the
    # flat files that belong to other episodes).
    for child in work.iterdir():
        if child.is_dir() and child.name != ".":
            try:
                shutil.rmtree(child, ignore_errors=True)
            except Exception:
                pass

    size = dest.stat().st_size
    if on_progress:
        await on_progress(size, size)
    import hashlib
    sha = hashlib.sha256()
    sha.update(dest.read_bytes())
    return {
        "path": str(dest),
        "name": dest.name,
        "bytes": size,
        "checksum": sha.hexdigest(),
        "complete": True,
    }
