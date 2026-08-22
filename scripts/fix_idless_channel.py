"""Repair an id-less request that generated a WRONG-franchise channel.

Shadows House (REQ-1083) was accepted id-less; a prior backfill set
``franchise_data.anime_doc_id="REQ-1083"``. The distribution content builder does
``anilist.search(anime_doc_id)`` when the doc-id isn't numeric — so it text-searched
the literal "REQ-1083" and AniList fuzzy-matched the WRONG series (DearS, id 63),
producing a Shadows-House-named channel full of DearS cards.

Fix: tear down the wrong distribution channel's DB rows, RE-KEY the request +
its packs/files/thumbnails from the code fallback to the REAL numeric AniList id
(so ``_walk_franchise`` resolves by id, not a fuzzy text search), and reassign the
Senku stage so the channel can be rebuilt correctly via the wizard.

DRY-RUN by default. Snapshots everything to a .bak before writing. Guarded: aborts
if the target AniList id is already in use by a different request.

Usage (Windows venv from WSL):
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/fix_idless_channel.py --code REQ-1083 --anilist-id 125038
    ... --apply --yes    # after reviewing the dry run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass
_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _m = types.ModuleType(f"kurosoden.{_sub}")
        _m.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _m


async def main(args) -> None:
    from sqlalchemy import select, update, func
    from sqlalchemy.orm.attributes import flag_modified

    from nekofetch.core.config import AppConfig, get_env
    from nekofetch.core.container import Container
    from nekofetch.infrastructure.database.postgres.session import session_scope
    from nekofetch.infrastructure.database.postgres.models import (
        DistributionBot, MediaFile, Request, StoragePack, ThumbnailSource,
    )
    from nekofetch.services.request_service import RequestService
    from kurosoden.shared.admin_assignment import AdminAssignment, OPEN_STATUSES
    from kurosoden.shared.distribution_cache import DistributionCache

    code = args.code.strip()
    new_id = str(args.anilist_id).strip()
    _cfg = AppConfig.load()
    _env = get_env()
    owner = int(args.admin_id
                or int(getattr(_cfg.security, "owner_id", 0) or 0)
                or int(getattr(_env, "owner_id", 0) or 0))

    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401

    try:
        async with session_scope(container.pg_sessionmaker) as s:
            req = (await s.execute(select(Request).where(Request.code == code))).scalar_one_or_none()
            if req is None:
                raise SystemExit(f"ERROR: no request {code!r}")
            old_doc = req.anime_doc_id
            from nekofetch.services.download_service import _safe_anime_doc_id
            packs_key = _safe_anime_doc_id(req)   # what packs are keyed under

            async def _count(model):
                return (await s.execute(select(func.count()).select_from(model)
                        .where(model.anime_doc_id == packs_key))).scalar()
            counts = {m.__name__: await _count(m)
                      for m in (StoragePack, MediaFile, ThumbnailSource)}

            # Guard: target id must not already belong to a DIFFERENT request.
            clash = (await s.execute(select(Request).where(
                Request.anime_doc_id == new_id, Request.code != code))).scalars().first()
            clash_packs = (await s.execute(select(func.count()).select_from(StoragePack)
                           .where(StoragePack.anime_doc_id == new_id))).scalar()
            bot = (await s.execute(select(DistributionBot).where(
                DistributionBot.anime_doc_id == packs_key))).scalars().first()

        print("=" * 72)
        print(f"Request {code}: anime_doc_id(col)={old_doc!r}  packs_key={packs_key!r}")
        print(f"Re-key {packs_key!r} -> {new_id!r}")
        print(f"  rows to re-key: {counts}")
        print(f"  distribution bot to purge: "
              + (f"id={bot.id} name={bot.name!r} chat_id={bot.chat_id}" if bot else "none"))
        if clash or (clash_packs and packs_key != new_id):
            raise SystemExit(f"ABORT: AniList id {new_id!r} already in use "
                             f"(request={getattr(clash,'code',None)}, packs={clash_packs}).")

        if not args.apply:
            print("\nDRY RUN — nothing changed. Re-run with --apply --yes to:")
            print(f"  1) purge the wrong distribution channel rows (bot + posts + layout + backup)")
            print(f"  2) re-key {packs_key!r} -> {new_id!r} on request + packs + files + thumbnails")
            print(f"  3) reassign the Senku stage to owner {owner} so you can rebuild the channel")
            print("  Then on the VPS: delete the TG channel + rm -rf "
                  f"data/storage/metadata/{packs_key}")
            return
        if not args.yes:
            if input("\nType 'yes' to apply: ").strip().casefold() != "yes":
                print("aborted"); return

        # ── Snapshot ──
        bak = _HERE / f"{code}.idless-fix.bak.json"
        bak.write_text(json.dumps({
            "code": code, "old_anime_doc_id": old_doc, "packs_key": packs_key,
            "new_id": new_id, "franchise_data": req.franchise_data,
            "counts": counts,
            "bot": ({"id": bot.id, "name": bot.name, "chat_id": bot.chat_id}
                    if bot else None),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"snapshot -> {bak}")

        rs = RequestService(container)
        # 1) Teardown the wrong distribution channel (keys on the OLD doc).
        async with session_scope(container.pg_sessionmaker) as s:
            await rs._purge_channel_rows(s, packs_key, [code])
        await rs._clear_distribution_cache([code])
        try:
            await DistributionCache(container).clear(code)
        except Exception as exc:  # noqa: BLE001
            print(f"  (cache clear note: {type(exc).__name__}: {str(exc)[:80]})")
        print("purged distribution rows + cache")

        # 2) Re-key everything from the code fallback to the numeric AniList id.
        async with session_scope(container.pg_sessionmaker) as s:
            for model in (StoragePack, MediaFile, ThumbnailSource):
                res = await s.execute(update(model)
                    .where(model.anime_doc_id == packs_key)
                    .values(anime_doc_id=new_id))
                print(f"  re-keyed {model.__name__}: {res.rowcount}")
            row = (await s.execute(select(Request).where(Request.code == code))).scalar_one()
            row.anime_doc_id = new_id
            data = dict(row.franchise_data or {})
            data["anime_doc_id"] = new_id
            data["anilist_id"] = int(new_id) if new_id.isdigit() else new_id
            row.franchise_data = data
            flag_modified(row, "franchise_data")
        print(f"re-keyed request {code} -> anime_doc_id={new_id}")

        # 3) Reassign the Senku stage (fresh 'assigned' row unless one is open).
        async with session_scope(container.pg_sessionmaker) as s:
            existing = (await s.execute(select(AdminAssignment).where(
                AdminAssignment.request_code == code,
                AdminAssignment.stage == "senku",
                AdminAssignment.status.in_(tuple(OPEN_STATUSES)),
            ))).scalars().first()
            if existing is not None:
                print(f"  senku assignment already open (id={existing.id}) — left as-is")
            else:
                s.add(AdminAssignment(
                    admin_telegram_id=owner, request_code=code, stage="senku",
                    status="assigned", assignment_mode="fallback",
                    decision_reason="idless_channel_rebuild",
                ))
                print(f"  created senku assignment for owner {owner}")

        print("\nDONE. Now on the VPS: delete the TG channel @Shadows_House_axw and "
              f"`rm -rf data/storage/metadata/{packs_key}`, then open Senku → the "
              f"{code} task → run the channel wizard. It will walk AniList id {new_id} "
              "and build the correct channel with working buttons.")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", required=True, help="request code, e.g. REQ-1083")
    ap.add_argument("--anilist-id", required=True, help="the REAL numeric AniList id, e.g. 125038")
    ap.add_argument("--admin-id", type=int, help="Senku task owner (default: configured owner_id)")
    ap.add_argument("--apply", action="store_true", help="write changes; else read-only")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    asyncio.run(main(ap.parse_args()))
