"""Backfill an id-less request so the Senku wizard builds the FULL channel title.

Some requests (notably torrents accepted before AniList resolved) are stored with
``anime_doc_id=None`` and ``franchise_data.romaji=None``. The Senku channel-title
wizard calls ``build_channel_essentials(anime_doc_id=franchise.get("anime_doc_id"),
…)`` — with a None doc-id it SKIPS the ``_gather`` pack lookup entirely, so the
title has no audio/quality/language tags and (with no romaji) collapses to the
bare series name.

Everything needed for the real title already exists: the storage packs live under
``_safe_anime_doc_id(req)`` (the code-based fallback), and AniList resolves the
romaji via the resilient chain. This one-off resolves the romaji and writes it +
the packs' doc-id into the request's ``franchise_data``, then busts the Senku
distribution cache so the wizard reloads fresh data. Re-running the wizard then
builds e.g. ``Shadows House《 Dual Audio 》« English & Japanese » 480p 720p 1080p``.

DRY RUN by default: prints the current vs proposed ``franchise_data``, the packs
found, and the EXACT title ``build_channel_essentials`` would now produce — touching
nothing. ``--apply`` writes the single row (snapshotting the old JSON first) and
clears the cache. Scoped to one ``--code``.

Usage (Windows venv from WSL):
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/backfill_idless_request.py --code REQ-1083            # dry run
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/backfill_idless_request.py --code REQ-1083 --apply --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
from pathlib import Path

# ── Standalone ``kurosoden`` namespace bootstrap (same as the other scripts) ──
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))

# UTF-8 stdout so romaji/native titles never crash a cp1252 console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 — older / redirected streams
        pass

_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _shim = types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim
# ──────────────────────────────────────────────────────────────────────────────


async def _resolve_romaji(container, english: str) -> tuple[str, int | None]:
    """(romaji, anilist_id) via the resilient chain — best-effort, ('', None) on miss."""
    ani = getattr(container, "anilist", None)
    if ani is None or not english:
        return "", None
    try:
        media = await ani.search(english)
        if media is None:
            return "", None
        return (getattr(media, "romaji", None) or ""), getattr(media, "id", None)
    except Exception as exc:  # noqa: BLE001
        print(f"  (AniList resolve failed: {type(exc).__name__}: {str(exc)[:120]})")
        return "", None


async def main(args) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from nekofetch.core.container import Container
    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.services.download_service import _safe_anime_doc_id
    from kurosoden.shared.channel_essentials import build_channel_essentials
    from kurosoden.shared.distribution_cache import DistributionCache

    code = args.code.strip()
    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401 — register ORM tables

    try:
        # ── Load the request + compute the target values (read-only) ──
        async with session_scope(container.pg_sessionmaker) as session:
            req = (await session.execute(
                select(Request).where(Request.code == code)
            )).scalar_one_or_none()
            if req is None:
                raise SystemExit(f"ERROR: no request with code {code!r}")
            fr = dict(req.franchise_data or {})
            english = (fr.get("english") or req.anime_title or "").strip()
            packs_key = _safe_anime_doc_id(req)

        print("=" * 72)
        print(f"Request  : {code}  status={req.status}")
        print(f"  english        : {english!r}")
        print(f"  anime_doc_id   : {req.anime_doc_id!r}  (column)")
        print(f"  fr.anime_doc_id: {fr.get('anime_doc_id')!r}")
        print(f"  fr.romaji      : {fr.get('romaji')!r}")
        print(f"  fr.anilist_id  : {fr.get('anilist_id')!r}")
        print(f"  packs key      : {packs_key!r}  (_safe_anime_doc_id — where packs live)")

        romaji, anilist_id = await _resolve_romaji(container, english)
        print(f"\nResolved via AniList: romaji={romaji!r}  anilist_id={anilist_id!r}")

        # Proposed franchise_data (only the three keys we touch).
        proposed = dict(fr)
        if romaji:
            proposed["romaji"] = romaji
        if anilist_id is not None:
            proposed["anilist_id"] = anilist_id
        proposed["anime_doc_id"] = packs_key    # so the wizard invokes _gather

        # ── Preview the EXACT title the wizard would now build ──
        # build_channel_essentials(anime_doc_id=<packs_key>) → _gather finds the
        # packs (audio/qualities/languages); romaji comes from the proposed dict.
        try:
            ess = await build_channel_essentials(
                container, anime_doc_id=packs_key, franchise=proposed)
            preview_title = ess.title
        except Exception as exc:  # noqa: BLE001
            preview_title = f"<preview failed: {type(exc).__name__}: {str(exc)[:100]}>"

        print("\nProposed franchise_data changes:")
        for k in ("anime_doc_id", "romaji", "anilist_id"):
            if fr.get(k) != proposed.get(k):
                print(f"    {k}: {fr.get(k)!r}  ->  {proposed.get(k)!r}")
        print(f"\nResulting channel title:\n    {preview_title}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply --yes to persist,"
                  " then re-run the Senku wizard title step for this title.")
            return
        if not args.yes:
            ans = input("\nWrite these franchise_data changes + bust the cache? Type 'yes': ")
            if ans.strip().casefold() != "yes":
                print("aborted")
                return

        # ── Snapshot the old franchise_data (rollback record) then write ──
        bak = _HERE / f"{code}.franchise_data.bak.json"
        bak.write_text(json.dumps(fr, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSnapshot of OLD franchise_data written to: {bak}")

        async with session_scope(container.pg_sessionmaker) as session:
            row = (await session.execute(
                select(Request).where(Request.code == code)
            )).scalar_one()
            data = dict(row.franchise_data or {})
            if romaji:
                data["romaji"] = romaji
            if anilist_id is not None:
                data["anilist_id"] = anilist_id
            data["anime_doc_id"] = packs_key
            row.franchise_data = data
            flag_modified(row, "franchise_data")   # JSONB in-place change tracking

        # ── Bust the Senku distribution cache so ensure() reloads fresh data ──
        try:
            await DistributionCache(container).clear(code)
            print("Senku distribution cache cleared for", code)
        except Exception as exc:  # noqa: BLE001 — cache bust is best-effort
            print(f"  (cache clear failed: {type(exc).__name__}: {str(exc)[:120]}) —"
                  " clear it manually or it may serve stale data")

        print("\nAPPLIED. Now re-run the Senku wizard title step for this title;"
              " it will set the full decorated channel title.")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", required=True, help="request code, e.g. REQ-1083")
    ap.add_argument("--apply", action="store_true",
                    help="write the row + bust the cache; without it the script is read-only")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (only with --apply)")
    asyncio.run(main(ap.parse_args()))
