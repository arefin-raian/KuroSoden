"""READ-ONLY inspector for one request's storage footprint.

Prints the Request row plus every StoragePack / MediaFile / DistributionBot
tied to its anime_doc_id, so we can see exactly what quality records exist
before touching anything. Connects straight to Postgres (no Container startup,
no Telegram) so it can't conflict with running bots.

Usage (Windows venv):
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/inspect_req.py REQ-1073
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))


async def main(code: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nekofetch.core.config import get_env
    from nekofetch.infrastructure.database.postgres.models import (
        DistributionBot,
        MediaFile,
        Request,
        StoragePack,
    )

    code = code.strip()
    if code.isdigit():
        code = f"REQ-{code}"

    engine = create_async_engine(get_env().postgres_dsn, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            req = (await s.execute(
                select(Request).where(Request.code == code)
            )).scalar_one_or_none()
            if req is None:
                print(f"!! No request found with code {code!r}")
                return
            doc = req.anime_doc_id
            print("=" * 72)
            print(f"REQUEST {req.code}  (id={req.id})")
            print(f"  anime_title : {req.anime_title}")
            print(f"  anime_doc_id: {doc}")
            print(f"  source      : {req.source}")
            print(f"  season      : {req.season}   scope={req.scope}")
            print(f"  resolution  : {req.resolution}   audio={req.audio}")
            print(f"  status      : {req.status}")
            print("=" * 72)

            if not doc:
                print("!! request has no anime_doc_id — storage rows are keyed by it; "
                      "cannot map packs. Stop and investigate manually.")
                return

            packs = (await s.execute(
                select(StoragePack)
                .where(StoragePack.anime_doc_id == doc)
                .order_by(StoragePack.season, StoragePack.resolution)
            )).scalars().all()
            print(f"\nSTORAGE PACKS for {doc}: {len(packs)} row(s)")
            for p in packs:
                flag = "  <-- 360p" if p.resolution == "360p" else ""
                print(f"  [pack id={p.id}] S{p.season} part={p.season_part} "
                      f"{p.resolution} {p.audio}  enabled={p.enabled}  "
                      f"entry_id={p.entry_id}  ingest={p.ingest_method}{flag}")
                print(f"        channel={p.channel_id} hdr={p.header_message_id} "
                      f"msgs={p.start_message_id}..{p.end_message_id} "
                      f"files={p.file_count}")
                if p.resolution == "360p":
                    print(f"        file_message_ids={p.file_message_ids}")

            files = (await s.execute(
                select(MediaFile)
                .where(MediaFile.anime_doc_id == doc)
                .order_by(MediaFile.season, MediaFile.resolution, MediaFile.episode)
            )).scalars().all()
            by_res: dict[str, list] = {}
            for f in files:
                by_res.setdefault(f.resolution or "?", []).append(f)
            print(f"\nMEDIA FILES (files table) for {doc}: {len(files)} row(s)")
            for res, group in sorted(by_res.items()):
                print(f"  {res}: {len(group)} file(s)  "
                      f"seasons={sorted({g.season for g in group})}  "
                      f"published={sorted({g.published for g in group})}")
                if res == "360p":
                    for g in group:
                        print(f"        [file id={g.id}] S{g.season}E{g.episode} "
                              f"{g.audio} tg_chat={g.tg_chat_id} "
                              f"tg_msg={g.tg_message_id} name={g.final_name!r}")

            bots = (await s.execute(
                select(DistributionBot).where(DistributionBot.anime_doc_id == doc)
            )).scalars().all()
            print(f"\nDISTRIBUTION BOTS/CHANNELS bound to {doc}: {len(bots)}")
            for b in bots:
                print(f"  [bot id={b.id}] {b.name} @{b.username} "
                      f"is_channel={b.is_channel} chat_id={b.chat_id} "
                      f"enabled={b.enabled} rev={b.content_revision}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: inspect_req.py <REQ-code|number>")
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1]))
