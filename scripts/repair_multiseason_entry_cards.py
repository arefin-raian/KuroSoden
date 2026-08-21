"""Repair multi-season distribution ENTRY CARDS whose thumbnail baked season-1
metadata onto a later season (Bug D — My Dress-Up Darling).

Background
----------
``render_entry`` used to enrich every season card from the franchise ROOT
``anilist.json["search"]`` blob, so season 2's card showed season 1's romaji /
native title / rating / year / runtime (synopsis + episodes were already
per-entry). The renderer now takes the entry's own ``anilist_id`` and reads that
installment's node from the cached franchise walk (with a live *resilient*
fallback). This one-off re-renders the ALREADY-PUBLISHED entry cards with the
corrected per-entry data and pushes them onto the live distribution posts.

It is SCOPED to a single request (default: My Dress-Up Darling) — no blind sweep.
Default is a DRY RUN: it prints old→new per-entry values (romaji / native / score
/ year·cert·runtime) and touches nothing. ``--apply`` re-renders and edits the
live cards, using Senku (the card author) — never the NekoFetch admin bot.

Only entries whose render-affecting fields actually CHANGE are re-rendered, so a
correct card (e.g. the root/season-1 entry) is left untouched.

Usage (Windows venv from WSL)::

    # dry run — prints planned changes, writes/edits nothing
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/repair_multiseason_entry_cards.py

    # apply after reviewing the dry run (Senku edits the live cards)
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/repair_multiseason_entry_cards.py --apply --yes

    # target a different title / an unambiguous request
    ... repair_multiseason_entry_cards.py --title "My Dress-Up Darling"
    ... repair_multiseason_entry_cards.py --code REQ-1094 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# ── Standalone ``kurosoden`` namespace bootstrap (same as the other scripts) ──
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))

# The report prints romaji/native titles that include Japanese script; a Windows
# console defaults to cp1252 and would crash on them. Force UTF-8 with a safe
# fallback so the script runs identically on any console/codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 — older/redirected streams lack reconfigure
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

DEFAULT_TITLE = "My Dress-Up Darling"


async def _resolve_request(container, *, code: str | None, title: str):
    """Find the target request; refuse to guess on an ambiguous title."""
    from sqlalchemy import func, select
    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        if code:
            req = (await session.execute(
                select(Request).where(Request.code == code.strip())
            )).scalar_one_or_none()
            if req is None:
                raise LookupError(f"no request with code {code!r}")
            return req.code, req.anime_doc_id, dict(req.franchise_data or {})
        rows = list((await session.execute(
            select(Request).where(func.lower(Request.anime_title) == title.casefold())
            .order_by(Request.id.desc())
        )).scalars().all())
        if not rows:
            rows = list((await session.execute(
                select(Request).where(Request.anime_title.ilike(f"%{title}%"))
                .order_by(Request.id.desc())
            )).scalars().all())
        if not rows:
            raise LookupError(f"no request matched {title!r}")
        if len(rows) != 1:
            for r in rows:
                print(f"  {r.code} · {r.anime_title!r} · doc={r.anime_doc_id or '—'}")
            raise RuntimeError("ambiguous title; rerun with --code REQ-XXXX")
        return rows[0].code, rows[0].anime_doc_id, dict(rows[0].franchise_data or {})


def _entries_from_franchise(franchise: dict) -> list[dict]:
    """The per-installment list with anilist_id / season_number / title."""
    ents = franchise.get("entries") or []
    out = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        out.append({
            "anilist_id": e.get("anilist_id"),
            "season_number": e.get("season_number"),
            "title": e.get("title"),
        })
    return out


async def _start_senku_client(env):
    """Start a bare Pyrogram client on Senku's token — the card author."""
    from pyrogram import Client

    token = env.distribution_bot_token
    if not token:
        raise RuntimeError("DISTRIBUTION_BOT_TOKEN is not set — cannot start Senku")
    client = Client(
        name="repair-entry-cards-senku",
        api_id=env.telegram_api_id, api_hash=env.telegram_api_hash,
        bot_token=token, no_updates=True, in_memory=True,
        workdir=str(env.session_path),
    )
    await client.start()
    return client


def _summary(fields: dict) -> dict:
    """The render-affecting per-entry fields we report + compare on."""
    return {
        "romaji": fields.get("romaji_title") or "",
        "native": fields.get("native_title") or "",
        "score": fields.get("anilist_score"),
        "meta": fields.get("meta_label") or "",
    }


async def main(args) -> None:
    from nekofetch.core.config import get_env
    from nekofetch.core.container import Container

    env = get_env()
    print(f"Postgres : {env.postgres_db} @ {env.postgres_host}")

    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401 — register ORM tables

    from bots.senku.handlers.thumbnail_edit_senku import _entry_fields
    from nekofetch.services.thumbnail_service import (
        ThumbnailRenderService, gather_thumbnail_fields, persist_thumbnail_source,
        render_fields,
    )
    from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService

    client = None
    try:
        code, doc, franchise = await _resolve_request(
            container, code=args.code, title=args.title)
        print(f"Request  : {code} · doc={doc or '—'}")
        if not doc:
            raise RuntimeError("request has no anime_doc_id — cannot key thumbnails")

        entries = _entries_from_franchise(franchise)
        if not entries:
            raise RuntimeError("no franchise entries recorded on this request")
        print(f"Entries  : {len(entries)}  (mode={'APPLY' if args.apply else 'DRY RUN'})\n")

        # Plan: for each entry with a saved card, compute corrected per-entry
        # fields and compare against what's stored.
        plans: list[dict] = []
        for e in entries:
            aid = e.get("anilist_id")
            if aid is None:
                print(f"  · season {e.get('season_number')} {e.get('title')!r}: "
                      "no anilist_id (torrent-mapped) — skipping")
                continue
            aid = int(aid)
            stored = await _entry_fields(container, doc, aid)
            if stored is None:
                print(f"  · [{aid}] {e.get('title')!r}: no saved thumbnail — skipping")
                continue
            title = stored.get("title") or e.get("title") or ""
            fresh = await gather_thumbnail_fields(
                container, title, doc, prefer_anilist_synopsis=True, anilist_id=aid)
            merged = {**stored, **fresh}
            changed = render_fields(stored) != render_fields(merged)
            old, new = _summary(stored), _summary(merged)
            flag = "CHANGED" if changed else "unchanged"
            print(f"  · [{aid}] S{e.get('season_number')} {title!r} — {flag}")
            for key in ("romaji", "native", "score", "meta"):
                if old[key] != new[key]:
                    print(f"        {key:6}: {old[key]!r}  ->  {new[key]!r}")
            if changed:
                plans.append({"aid": aid, "title": title, "merged": merged})

        if not plans:
            print("\nNothing to repair — every saved card already matches its "
                  "per-entry AniList data.")
            return
        if not args.apply:
            print(f"\nDRY RUN — {len(plans)} card(s) would be re-rendered + pushed "
                  "live. Re-run with --apply --yes.")
            return
        if not args.yes:
            ans = input(f"\nRe-render + push {len(plans)} live card(s) as Senku? "
                        "Type 'yes': ")
            if ans.strip().casefold() != "yes":
                print("aborted")
                return

        # Senku authors the distribution cards — wire it as the client the
        # ThumbnailChannelService resolves (pipeline_manager.senku → _client).
        client = await _start_senku_client(env)
        me = await client.get_me()
        print(f"\nediting as @{me.username} (Senku, id {me.id})")
        container.pipeline_manager = types.SimpleNamespace(senku=client, lelouch=None)

        renderer = ThumbnailRenderService()
        svc = ThumbnailChannelService(container)
        try:
            for p in plans:
                aid, merged = p["aid"], p["merged"]
                image_path = await renderer.render_thumbnail(**render_fields(merged))
                if not image_path:
                    print(f"  [{aid}] render returned no image — skipped")
                    continue
                await persist_thumbnail_source(container, doc, aid, merged,
                                               image_path=image_path)
                ok = await svc.refresh_published_thumbnail(doc, aid, str(image_path))
                print(f"  [{aid}] {p['title']!r}: "
                      + ("live card refreshed" if ok else
                         "persisted, but NO live card found to refresh"))
        finally:
            await renderer.close()
    finally:
        if client is not None:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", default=DEFAULT_TITLE,
                    help=f"exact title to repair (default: {DEFAULT_TITLE!r})")
    ap.add_argument("--code", help="unambiguous request code, e.g. REQ-1094")
    ap.add_argument("--apply", action="store_true",
                    help="re-render + edit the live cards; without it the script is read-only")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (only with --apply)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args))
    except (LookupError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
