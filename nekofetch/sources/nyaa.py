"""Nyaa.si source — torrent-based anime releases.

Unlike the streaming sources, Nyaa is a torrent index. The flow:

  search   ──> nyaa.si RSS, Anime category, sorted by seeders desc.
               Prefer "Dual Audio" releases (fuzzy match), then highest seeders.
  episodes ──> download the .torrent, parse its file list (bencode), detect the
               per-release naming pattern and order EP1..EPN (seasons / movies /
               specials handled).
  variants ──> the file(s) in the torrent, resolution parsed from the name.
  download ──> fast multi-connection torrent fetch (aria2c), selective files,
               then ffmpeg transcodes (720p / 480p / recompressed 1080p).

We scrape nyaa.si directly (its RSS exposes seeders/size/infohash cleanly) rather
than depend on a third-party hosted API instance.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import AudioType
from nekofetch.sources._torrent import order_episodes, torrent_files
from nekofetch.sources.base import (
    AnimeDetails,
    AnimeSource,
    AnimeStub,
    Episode,
    ProgressCallback,
    VideoVariant,
)

log = get_logger(__name__)

BASE_URL = "https://nyaa.si"
NYAA_NS = "{https://nyaa.si/xmlns/nyaa}"

# Anime categories on nyaa.si: 1_0 all, 1_2 English-translated, 1_3 non-English,
# 1_4 raw, 1_1 AMV. English-translated is where Dual Audio releases live.
CAT_ANIME_ENG = "1_2"
CAT_ANIME_ALL = "1_0"

# "dual" and "audio" adjacent with any separator: "Dual Audio", "Dual-Audio",
# "Dual_Audio", "[DualAudio]", "DUAL.AUDIO", etc.
_DUAL_AUDIO_RE = re.compile(r"dual[\s._\-\[\]()]*audio", re.IGNORECASE)
_MULTI_AUDIO_RE = re.compile(r"multi[\s._\-\[\]()]*audio", re.IGNORECASE)

# Language indicators (word-boundary) used when the title doesn't say "dual".
_LANG_HINTS = {
    "en": re.compile(r"\b(english|eng|en|dub|dubbed)\b", re.IGNORECASE),
    "ja": re.compile(r"\b(japanese|jpn|jap|jp)\b", re.IGNORECASE),
    "hi": re.compile(r"\b(hindi|hin)\b", re.IGNORECASE),
}


def is_dual_audio(title: str) -> bool:
    return bool(_DUAL_AUDIO_RE.search(title or ""))


def detect_languages(*texts: str) -> set[str]:
    """Infer audio languages present from any text fields (title/description)."""
    blob = " ".join(t for t in texts if t)
    return {lang for lang, rx in _LANG_HINTS.items() if rx.search(blob)}


def classify_audio(title: str, description: str = "") -> dict:
    """Best-effort audio classification from title + description.

    Returns ``{audio: 'dual'|'multi'|'single', langs: set, dual_audio: bool}``.
    Explicit "Dual/Multi Audio" wins; otherwise infer from language indicators
    (2+ languages including both ja & en ⇒ effectively dual).
    """
    explicit_multi = bool(_MULTI_AUDIO_RE.search(title))
    explicit_dual = is_dual_audio(title)
    langs = detect_languages(title, description)
    if explicit_multi or len(langs) >= 3:
        return {"audio": "multi", "langs": langs, "dual_audio": True}
    if explicit_dual or {"ja", "en"} <= langs:
        return {"audio": "dual", "langs": langs, "dual_audio": True}
    return {"audio": "single", "langs": langs, "dual_audio": False}


def _text(item: ET.Element, tag: str, *, ns: bool = False) -> str:
    el = item.find(f"{NYAA_NS}{tag}" if ns else tag)
    return el.text if el is not None and el.text else ""


def _size_to_bytes(text: str) -> int:
    m = re.match(r"([\d.]+)\s*(\w+)", text.strip())
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).lower()
    mult = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
            "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}.get(unit, 1)
    return int(val * mult)


class NyaaSource(AnimeSource):
    name = "nyaa"

    def __init__(self, base_url: str = BASE_URL, category: str = CAT_ANIME_ENG) -> None:
        self.base_url = base_url.rstrip("/")
        self.category = category
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _rss(self, query: str) -> list[dict]:
        """Fetch nyaa RSS for a query (Anime category, seeders desc)."""
        params = {
            "page": "rss", "f": "0", "c": self.category,
            "q": query, "s": "seeders", "o": "desc",
        }
        resp = await self.http.get(f"{self.base_url}/", params=params)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items: list[dict] = []
        for it in root.iter("item"):
            title = _text(it, "title")
            items.append({
                "title": title,
                "torrent_url": _text(it, "link"),
                "view_url": _text(it, "guid"),
                "info_hash": _text(it, "infoHash", ns=True),
                "seeders": int(_text(it, "seeders", ns=True) or 0),
                "leechers": int(_text(it, "leechers", ns=True) or 0),
                "downloads": int(_text(it, "downloads", ns=True) or 0),
                "size_text": _text(it, "size", ns=True),
                "size_bytes": _size_to_bytes(_text(it, "size", ns=True)),
                "category_id": _text(it, "categoryId", ns=True),
                "trusted": _text(it, "trusted", ns=True) == "Yes",
                "dual_audio": is_dual_audio(title),
                "audio": classify_audio(title)["audio"],
                "langs": sorted(classify_audio(title)["langs"]),
            })
        return items

    @staticmethod
    def _rank(items: list[dict]) -> list[dict]:
        """Dual-audio releases first, each group sorted by seeders desc.

        Trusted uploaders break ties — they tend to be faster/more reliable.
        """
        return sorted(
            items,
            key=lambda r: (r["dual_audio"], r["seeders"], r["trusted"]),
            reverse=True,
        )

    async def search(self, query: str) -> list[AnimeStub]:
        slug_match = re.match(r"slug:(.+)", query)
        if slug_match:
            # slug form: a packed release ref -> single stub
            try:
                info = json.loads(slug_match.group(1))
                return [self._stub(info)]
            except (json.JSONDecodeError, KeyError):
                return []

        try:
            items = await self._rss(query)
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("nyaa.search.failed", error=str(exc))
            return []

        # Fallback hierarchy: if NO release advertises Dual Audio in its title,
        # inspect the descriptions of the top seeded candidates for language
        # indicators (ja+en ⇒ dual) before settling for a subbed release.
        if not any(r["dual_audio"] for r in items) and items:
            await self._refine_with_descriptions(items[:6])

        return [self._stub(r) for r in self._rank(items)]

    async def _refine_with_descriptions(self, items: list[dict]) -> None:
        """Fetch view pages for ambiguous releases and re-classify audio."""
        async def one(r: dict) -> None:
            try:
                resp = await self.http.get(r["view_url"])
                desc = ""
                m = re.search(r'id="torrent-description"[^>]*>(.*?)</div>',
                              resp.text, re.DOTALL)
                if m:
                    desc = m.group(1)
                cls = classify_audio(r["title"], desc)
                r["dual_audio"] = cls["dual_audio"]
                r["audio"] = cls["audio"]
                r["langs"] = sorted(cls["langs"])
            except httpx.HTTPError:
                pass

        await asyncio.gather(*(one(r) for r in items if r.get("view_url")))

    def _stub(self, r: dict) -> AnimeStub:
        flag = " [Dual Audio]" if r.get("dual_audio") else ""
        return AnimeStub(
            source_ref=json.dumps(r),
            title=f"{r['title']}{flag} · {r['seeders']}S · {r.get('size_text','')}",
        )

    async def get_details(self, source_ref: str) -> AnimeDetails:
        r = json.loads(source_ref)
        return AnimeDetails(
            source_ref=source_ref,
            title=r["title"],
            synopsis=(f"Nyaa release · {r['seeders']} seeders / {r['leechers']} leechers "
                      f"· {r.get('size_text','')} · "
                      f"{'Dual Audio' if r.get('dual_audio') else 'single audio'}"),
        )

    async def get_episodes(self, source_ref: str) -> list[Episode]:
        """Download the .torrent, parse its file list, order EP1..EPN."""
        r = json.loads(source_ref)
        try:
            if r.get("torrent_path"):
                raw = Path(r["torrent_path"]).read_bytes()
            elif r.get("magnet"):
                raw = await self._resolve_magnet(r["magnet"])
            else:
                resp = await self.http.get(r["torrent_url"])
                resp.raise_for_status()
                raw = resp.content
            _name, files = torrent_files(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("nyaa.torrent.parse_failed", error=str(exc))
            return []

        ordered = order_episodes(files, prefer_resolution=1080)
        episodes: list[Episode] = []
        ep_ref_base = {}
        if r.get("torrent_url"):
            ep_ref_base["torrent_url"] = r["torrent_url"]
        if r.get("magnet"):
            ep_ref_base["magnet"] = r["magnet"]
        if r.get("torrent_path"):
            ep_ref_base["torrent_path"] = r["torrent_path"]
        ep_ref_base["info_hash"] = r.get("info_hash", "")

        # Robustly derive audio. Signals, most-authoritative first:
        #   1. An explicit upstream kind the caller already resolved and stored on
        #      the ref (``audio_kind`` / ``audio`` = 'dual'|'multi', or the legacy
        #      ``dual_audio`` bool). The admin magnet/paste handlers run
        #      classify_audio on the magnet's OWN dn= release name — which carries
        #      "Dual Audio" even when the request title and the torrent's internal
        #      name don't — and stash the verdict here. It must WIN.
        #   2. Fresh classification of every name we can see: the release title,
        #      the magnet dn (``release_name``), the torrent's internal name, and
        #      the per-file names.
        # Previously step 2 ran on the REQUEST title only and step 1 checked just
        # ``dual_audio`` — so a BDRip whose internal name says only "x265" (not
        # "Dual Audio") was stamped Sub despite the dn clearly saying Dual Audio.
        upstream = (r.get("audio_kind") or r.get("audio") or "").lower()
        release_name = r.get("release_name") or r.get("dn") or ""
        file_blob = " ".join(f.get("name", "") for f in files[:40])
        cls = classify_audio(
            f"{r.get('title', '')} {release_name} {_name} {file_blob}")
        audio_kind = cls["audio"]  # 'dual' | 'multi' | 'single'
        # Upstream verdict overrides a fresh 'single' miss (never downgrades a
        # freshly-detected multi/dual).
        if upstream in ("dual", "multi") and audio_kind == "single":
            audio_kind = upstream
        elif r.get("dual_audio") and audio_kind == "single":
            audio_kind = "dual"  # legacy explicit flag
        ep_ref_base["dual_audio"] = audio_kind in ("dual", "multi")
        ep_ref_base["audio_kind"] = audio_kind
        # Preserve the release name so any downstream re-parse keeps the signal.
        if release_name:
            ep_ref_base["release_name"] = release_name

        for e in ordered:
            label = {"movie": "Movie", "ova": "OVA", "special": "Special",
                     "extra": "Extra"}.get(e["kind"], f"Ep. {e.get('episode') or e['seq']}")
            episodes.append(
                Episode(
                    source_ref=json.dumps({
                        **ep_ref_base,
                        "file_index": e["index"],
                        "path": e["path"],
                        "name": e["name"],
                        "length": e["length"],
                        "resolution": e.get("resolution"),
                        "kind": e["kind"],
                        "season": e["season"],
                    }),
                    season=e["season"],
                    number=e["seq"],
                    title=f"{label} — {e['name']}",
                )
            )
        return episodes

    async def get_variants(self, episode_ref: str) -> list[VideoVariant]:
        """One variant: the torrent file itself (transcodes happen post-download)."""
        e = json.loads(episode_ref)
        # Map the derived audio kind to the domain enum. ``audio_kind`` is set by
        # get_episodes (multi/dual/single); fall back to the legacy dual_audio
        # flag for refs built before that field existed.
        kind = e.get("audio_kind")
        if kind == "multi":
            audio = AudioType.MULTI
        elif kind == "dual" or e.get("dual_audio"):
            audio = AudioType.DUAL_AUDIO
        else:
            audio = AudioType.SUBBED
        return [
            VideoVariant(
                source_ref=episode_ref,
                resolution=e.get("resolution") or "1080p",
                audio=audio,
                container=Path(e["name"]).suffix.lstrip("."),
                size_bytes=e.get("length"),
            )
        ]

    async def download(
        self,
        variant: VideoVariant,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        resume_state: dict | None = None,
    ) -> dict:
        from nekofetch.sources._torrentdl import download_torrent_file

        info = json.loads(variant.source_ref)
        return await download_torrent_file(
            info, dest, on_progress=on_progress,
        )

    async def _resolve_magnet(self, magnet_uri: str) -> bytes:
        """Resolve a magnet URI to .torrent metadata bytes via aria2c.

        Uses the same well-seeded public trackers + DHT config as the downloader
        so a magnet with no baked-in trackers can still find peers. aria2c writes
        the fetched metadata as ``<info-hash>.torrent`` in ``--dir``; we return
        its bytes. On failure the real aria2c output is surfaced in the error so
        the admin sees *why* (no peers, DHT blocked, bad magnet) instead of a
        generic "did not produce a .torrent".
        """
        import asyncio
        import tempfile

        from nekofetch.sources._torrentdl import _EXTRA_TRACKERS, find_aria2

        aria2 = find_aria2()
        if not aria2:
            raise RuntimeError("aria2c not found")

        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                aria2, "--dir", tmp,
                "--bt-metadata-only=true", "--bt-save-metadata=true",
                "--bt-stop-timeout=90", "--seed-time=0",
                "--enable-dht=true",
                "--dht-listen-port=6881-6999",
                "--listen-port=6881-6999",
                "--bt-max-peers=200",
                f"--bt-tracker={_EXTRA_TRACKERS}",
                "--console-log-level=warn",
                "--summary-interval=0",
                magnet_uri,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            except (TimeoutError, asyncio.TimeoutError):
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    "magnet metadata resolution timed out after 180s "
                    "(no peers found — the magnet may be dead or DHT is blocked)"
                ) from None

            torrents = list(Path(tmp).glob("*.torrent"))
            if not torrents:
                tail = (out or b"").decode(errors="replace").strip()
                tail = tail[-400:] if tail else "(no output)"
                raise RuntimeError(
                    f"aria2c did not produce a .torrent from the magnet — {tail}"
                )
            return torrents[0].read_bytes()
