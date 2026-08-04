"""Rebuild / inventory StoragePack rows by walking the storage channel.

Disaster-recovery tool for the case the operator worried about: *the database
gets banned/lost, but the storage channel survives*. Every uploaded pack is
self-describing in the channel —

    header caption            (human-readable, from build_pack_caption)
      ➠ TAKOPI'S ORIGINAL SIN : SEASON 1
      ➠ 480p [DUAL ∽ ENG + JPN]
    file 1 … file N           (branded document file_name per file)
    end sticker               (pack terminator)

so the pack boundaries + resolution + audio can be recovered from Telegram alone,
without any Postgres row. Line-1 (title/season) is UPPERCASED and shortened by the
caption builder, so it can't always round-trip to the exact ``anime_doc_id`` — the
inventory therefore prints the recovered header verbatim + each file's on-disk
name so a human can identify the series, and (with ``--reindex``) re-persists what
IS unambiguous via :meth:`StorageChannelService.index_pack`.

Usage (Windows venv):
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/rebuild_storage_index.py                       # inventory only
    …/python.exe scripts/rebuild_storage_index.py --json out.json
    …/python.exe scripts/rebuild_storage_index.py --from 2 --to 5000
    …/python.exe scripts/rebuild_storage_index.py --reindex --anime "anilist:1"

    --from / --to     message-id range to scan (default: 1 .. newest).
    --json PATH       also write the full manifest as JSON.
    --reindex         re-persist recovered packs into StoragePack (needs --anime;
                      re-indexes packs whose resolution+audio parsed cleanly).
    --anime DOC_ID    the anime_doc_id to attach when --reindex is set. Required
                      for reindex because the header can't reliably carry it.

Read-only by default: without --reindex it NEVER writes to Postgres or Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path

# ── ``kurosoden`` namespace bootstrap (mirrors requeue_senku.py) ──────────────
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))

_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _shim = types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim
# ──────────────────────────────────────────────────────────────────────────────

# Line-2 of a pack header: "➠ 480p [DUAL ∽ ENG + JPN]" (or "➠ 480p" bare).
_RES_RE = re.compile(r"➠\s*([0-9]{3,4}p)\s*(?:\[([A-Z]+)\s*∽)?", re.UNICODE)
# Line-1 season token, best-effort: "… : SEASON 3 PART 2" / "… : S3" / "… : MOVIE".
_SEASON_RE = re.compile(r":\s*(?:SEASON\s*(\d+)|S(\d+))", re.IGNORECASE)

# Reverse of _AUDIO_CAPTION's TAG → AudioType (parsed from line-2).
_TAG_TO_AUDIO = {
    "DUAL": "DUAL_AUDIO", "MULTI": "MULTI", "SUB": "SUBBED", "DUB": "DUBBED",
}


def _parse_header(caption: str) -> dict:
    """Best-effort recover {title_line, resolution, audio_tag, season} from a header."""
    text = caption or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: dict = {
        "raw": text, "title_line": lines[0] if lines else "",
        "resolution": None, "audio_tag": None, "season": None,
    }
    m = _RES_RE.search(text)
    if m:
        out["resolution"] = m.group(1)
        out["audio_tag"] = m.group(2)  # may be None for a bare "➠ 480p"
    s = _SEASON_RE.search(lines[0] if lines else "")
    if s:
        out["season"] = int(s.group(1) or s.group(2))
    return out


def _file_name(msg) -> str | None:
    doc = getattr(msg, "document", None) or getattr(msg, "video", None) \
        or getattr(msg, "audio", None)
    return getattr(doc, "file_name", None) if doc else None


async def _walk_channel(client, channel_id: int, start: int, end: int) -> list[dict]:
    """Group channel messages into packs: header text → files → end sticker."""
    packs: list[dict] = []
    cur: dict | None = None

    def _flush():
        nonlocal cur
        if cur and cur["file_ids"]:
            packs.append(cur)
        cur = None

    for mid in range(start, end + 1):
        try:
            msg = await client.get_messages(channel_id, mid)
        except Exception:  # noqa: BLE001 — deleted / missing id
            continue
        if msg is None or getattr(msg, "empty", False):
            continue

        is_file = bool(msg.document or msg.video or msg.audio)
        is_sticker = bool(getattr(msg, "sticker", None))
        text = msg.text or msg.caption or ""

        if is_sticker:
            # End marker: close the current pack.
            if cur:
                cur["end_message_id"] = mid
                _flush()
            continue
        if is_file:
            if cur is None:
                # A file with no preceding header (indexed/legacy). Start a pack
                # anchored on the first file so nothing is lost.
                cur = {"header_message_id": None, "header": None,
                       "start_message_id": mid, "end_message_id": mid,
                       "file_ids": [], "file_names": []}
            cur["file_ids"].append(mid)
            cur["file_names"].append(_file_name(msg))
            cur["end_message_id"] = mid
            if cur.get("start_message_id") is None:
                cur["start_message_id"] = mid
            continue
        if text.strip():
            # A text message starts a NEW pack header — flush any open one first.
            _flush()
            cur = {"header_message_id": mid, "header": _parse_header(text),
                   "start_message_id": None, "end_message_id": mid,
                   "file_ids": [], "file_names": []}

    _flush()  # trailing pack with no sticker terminator
    return packs


def _print_manifest(packs: list[dict]) -> None:
    print(f"\n=== Storage channel inventory: {len(packs)} pack(s) recovered ===\n")
    for i, p in enumerate(packs, start=1):
        hdr = p.get("header") or {}
        title = hdr.get("title_line") or "(no header — indexed/legacy pack)"
        res = hdr.get("resolution") or "?"
        aud = hdr.get("audio_tag") or "?"
        season = hdr.get("season")
        print(f"[{i:>3}] {title}")
        print(f"      {res} · audio={aud} · season={season}")
        print(f"      msgs {p['start_message_id']}..{p['end_message_id']} "
              f"({len(p['file_ids'])} files)")
        for fn in p["file_names"][:3]:
            if fn:
                print(f"        • {fn}")
        if len(p["file_names"]) > 3:
            print(f"        … +{len(p['file_names']) - 3} more")
        print()


async def main(args) -> None:
    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()
    try:
        from nekofetch.services.storage_channel_service import (
            PackKey,
            StorageChannelService,
        )
        from nekofetch.domain.enums import AudioType

        svc = StorageChannelService(container)
        client = svc._client
        channel_id = svc.cfg.channel_id
        print(f"  Storage channel : {channel_id}")

        end = args.to
        if end is None:
            # Newest message id: send+delete a probe, or fall back to a large scan.
            try:
                async for m in client.get_chat_history(channel_id, limit=1):
                    end = m.id
                    break
            except Exception:  # noqa: BLE001
                end = 100000
        start = max(1, args.from_)
        print(f"  Scanning range  : {start} .. {end}\n")

        packs = await _walk_channel(client, channel_id, start, end)
        _print_manifest(packs)

        if args.json:
            Path(args.json).write_text(
                json.dumps(packs, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"manifest written → {args.json}")

        if args.reindex:
            if not args.anime:
                print("\n--reindex requires --anime <doc_id> (the header can't "
                      "carry it reliably). Aborting reindex.")
                return
            reindexed = 0
            skipped = 0
            for p in packs:
                hdr = p.get("header") or {}
                res = hdr.get("resolution")
                tag = hdr.get("audio_tag")
                if not res or tag not in _TAG_TO_AUDIO:
                    skipped += 1
                    continue
                key = PackKey(
                    anime_doc_id=args.anime,
                    season=hdr.get("season"),
                    resolution=res,
                    audio=AudioType[_TAG_TO_AUDIO[tag]],
                )
                title = (hdr.get("title_line") or args.anime).lstrip("➠ ").strip()
                await svc.index_pack(
                    key, title=title,
                    start_message_id=p.get("header_message_id")
                    or p["start_message_id"],
                    end_message_id=p["end_message_id"],
                    channel_id=channel_id,
                )
                reindexed += 1
            print(f"\nreindex: {reindexed} pack(s) re-persisted, "
                  f"{skipped} skipped (unparseable header).")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="from_", type=int, default=1,
                    help="first message id to scan (default 1)")
    ap.add_argument("--to", dest="to", type=int, default=None,
                    help="last message id to scan (default: newest)")
    ap.add_argument("--json", dest="json", default=None,
                    help="write the full manifest to this JSON path")
    ap.add_argument("--reindex", action="store_true",
                    help="re-persist recovered packs into StoragePack")
    ap.add_argument("--anime", dest="anime", default=None,
                    help="anime_doc_id to attach when --reindex is set")
    asyncio.run(main(ap.parse_args()))
