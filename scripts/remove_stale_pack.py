"""Surgical cleanup for a single stale StoragePack + its orphaned MediaFile rows.

Used for REQ-1073 (Sabikui Bisco 360p subbed pack whose channel messages were
already manually deleted but whose DB rows survived). Read-only by default; pass
--apply to commit the deletion.

Usage (Windows venv):
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \\
        scripts/remove_stale_pack.py --doc 130591 --resolution 360p --audio subbed
    … --apply      # actually delete after confirming the dry-run output
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))


async def main(args) -> None:
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nekofetch.core.config import get_env
    from nekofetch.infrastructure.database.postgres.models import (
        DownloadJob,
        MediaFile,
        StoragePack,
    )
    from nekofetch.domain.enums import AudioType

    doc = args.doc
    resolution = args.resolution
    try:
        audio = AudioType[args.audio.upper()]
    except KeyError:
        print(f"!! Unknown audio type {args.audio!r}. "
              f"Valid values: {[e.name for e in AudioType]}")
        raise SystemExit(1)

    engine = create_async_engine(get_env().postgres_dsn, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            # ── 1. Locate the StoragePack row ──────────────────────────────────
            pack = (await session.execute(
                select(StoragePack).where(
                    StoragePack.anime_doc_id == doc,
                    StoragePack.resolution == resolution,
                    StoragePack.audio == audio,
                )
            )).scalar_one_or_none()

            if pack is None:
                print(f"No StoragePack found for doc={doc!r} "
                      f"resolution={resolution!r} audio={audio!r}. Nothing to do.")
                return

            print("=" * 72)
            print(f"StoragePack to remove:")
            print(f"  id          : {pack.id}")
            print(f"  anime       : {pack.anime_title!r}  ({doc})")
            print(f"  season      : {pack.season}  part={pack.season_part}")
            print(f"  resolution  : {pack.resolution}  audio={pack.audio}")
            print(f"  enabled     : {pack.enabled}")
            print(f"  channel     : {pack.channel_id}")
            print(f"  messages    : hdr={pack.header_message_id} "
                  f"range={pack.start_message_id}..{pack.end_message_id} "
                  f"files={pack.file_count}")
            print(f"  file_ids    : {pack.file_message_ids}")
            print("=" * 72)

            # ── 2. Locate orphaned MediaFile rows that belong to the same
            #       (anime_doc_id, resolution, audio) with no Telegram reference
            #       (tg_message_id IS NULL → never uploaded to a live channel). ──
            media_rows = (await session.execute(
                select(MediaFile).where(
                    MediaFile.anime_doc_id == doc,
                    MediaFile.resolution == resolution,
                    MediaFile.audio == audio,
                )
            )).scalars().all()

            # Separate "safe to delete" (no tg reference) from "has live tg ref"
            # (only safe to delete when they're already deleted from the channel).
            safe_delete: list[MediaFile] = []
            has_tg_ref: list[MediaFile] = []
            for mf in media_rows:
                if mf.tg_message_id is not None and mf.tg_chat_id is not None:
                    has_tg_ref.append(mf)
                else:
                    safe_delete.append(mf)

            print(f"MediaFile rows for {resolution}/{audio.value}:")
            print(f"  No TG ref (orphaned) : {len(safe_delete)} row(s) "
                  f"— will delete")
            if has_tg_ref:
                print(f"  Has TG ref           : {len(has_tg_ref)} row(s) "
                      f"— will also delete (channel messages already removed "
                      f"manually)")
            all_media = safe_delete + has_tg_ref
            for mf in all_media:
                print(f"    [file id={mf.id}] S{mf.season}E{mf.episode} "
                      f"tg_chat={mf.tg_chat_id} tg_msg={mf.tg_message_id} "
                      f"name={mf.final_name!r}")

            # ── 3. Find jobs that ONLY have these 360p MediaFiles (fully orphaned)
            #       so we can decide whether to delete the job row too. ──
            job_ids_affected = {mf.job_id for mf in all_media if mf.job_id}
            jobs_fully_orphaned: list[int] = []
            for jid in job_ids_affected:
                all_files_for_job = (await session.execute(
                    select(MediaFile).where(MediaFile.job_id == jid)
                )).scalars().all()
                ids_in_job = {mf.id for mf in all_files_for_job}
                ids_to_delete = {mf.id for mf in all_media}
                if ids_in_job.issubset(ids_to_delete):
                    jobs_fully_orphaned.append(jid)

            print(f"\nDownloadJob rows fully orphaned: {jobs_fully_orphaned}")
            print()

            if not args.apply:
                print("DRY RUN — pass --apply to commit these deletions.")
                return

            # ── 4. Delete ──────────────────────────────────────────────────────
            media_ids = [mf.id for mf in all_media]
            if media_ids:
                await session.execute(
                    delete(MediaFile).where(MediaFile.id.in_(media_ids))
                )
            await session.delete(pack)
            # Only delete a DownloadJob row when ALL its files are in this batch,
            # so we don't orphan files belonging to other resolutions.
            if jobs_fully_orphaned:
                await session.execute(
                    delete(DownloadJob).where(
                        DownloadJob.id.in_(jobs_fully_orphaned)
                    )
                )
            await session.commit()

            print(f"Deleted:")
            print(f"  StoragePack id={pack.id}")
            print(f"  {len(media_ids)} MediaFile row(s)")
            if jobs_fully_orphaned:
                print(f"  DownloadJob row(s): {jobs_fully_orphaned}")
            print()
            print("Done. The guide will now list only the remaining packs "
                  "when content is next regenerated.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", required=True,
                    help="anime_doc_id (e.g. 130591)")
    ap.add_argument("--resolution", required=True,
                    help="resolution to remove (e.g. 360p)")
    ap.add_argument("--audio", required=True,
                    help="audio type (e.g. subbed, dual_audio, dubbed, multi)")
    ap.add_argument("--apply", action="store_true",
                    help="actually commit the deletion (default is dry-run)")
    asyncio.run(main(ap.parse_args()))
