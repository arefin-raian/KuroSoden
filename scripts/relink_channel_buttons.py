"""Relink a published distribution channel's quality buttons to fresh packs.

Use after a REDO of an already-published title re-downloaded/re-encoded the
storage packs (so the old 480p/720p/1080p buttons now deep-link to deleted
files) but the channel + season cards were kept. This re-points every season/
movie card's buttons at the CURRENT packs, in place — captions, images and
layout are untouched.

    # dry run — show the channel/cards/packs it WOULD relink, edit nothing
    python scripts/relink_channel_buttons.py REQ-1079 --dry-run
    python scripts/relink_channel_buttons.py 153406  --dry-run   # anime_doc_id

    # actually relink
    python scripts/relink_channel_buttons.py REQ-1079 --yes

Why a dedicated Senku client: the season cards were POSTED by Senku (its bot
token), and Telegram only lets a bot edit a channel message it authored (or one
its admin rights explicitly allow it to edit-of-others). A bare Container from
``startup()`` has NO ``admin_client`` (that's set by the pipeline manager at bot
start), and even that client is NekoFetch/Gojo — the wrong author. So this
script starts a client on ``DISTRIBUTION_BOT_TOKEN`` (Senku) and passes it to
``relink_packs_in_place``; ``_resolve_relink_client`` then uses it directly
(no pipeline manager in a script → the passed client is the fallback). For a
userbot-scoped channel the relink acquires the userbot pool itself.

It NEVER deletes anything and NEVER reposts — a mis-privileged client or a
missing pack just logs and skips that card (``relinked`` counts the successes).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# ── ``kurosoden`` namespace bootstrap (mirrors requeue_senku.py) ─────────────
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


async def _resolve_doc_id(container, target: str) -> str | None:
    """Turn a REQ code or a raw anime_doc_id into the anime_doc_id to relink."""
    from sqlalchemy import select

    from kurosoden.shared.models import Request  # noqa: F401 — registers tables
    from nekofetch.infrastructure.database.postgres.models import Request as Req
    from nekofetch.infrastructure.database.postgres.session import session_scope

    if not target.upper().startswith("REQ-"):
        return target  # already an anime_doc_id
    async with session_scope(container.pg_sessionmaker) as session:
        req = (await session.execute(
            select(Req).where(Req.code == target.upper())
        )).scalar_one_or_none()
        if req is None:
            return None
        fd = req.franchise_data or {}
        return req.anime_doc_id or (str(fd.get("anilist_id")) if fd.get("anilist_id") else None)


async def _describe(container, anime_doc_id: str) -> None:
    """Print the channel anchor, season/movie cards and fresh packs (read-only)."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        ChannelLayout,
        DistributionBot,
        StoragePack,
    )
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        bot = (await session.execute(
            select(DistributionBot).where(
                DistributionBot.anime_doc_id == anime_doc_id,
                DistributionBot.is_channel.is_(True),
            ).order_by(DistributionBot.id.desc())
        )).scalars().first()
        if bot is None or not bot.chat_id:
            print(f"  channel   : NONE (no DistributionBot channel for {anime_doc_id})")
            return
        print(f"  channel   : chat_id={bot.chat_id} scope={bot.creation_scope} "
              f"userbot={bot.userbot_account}")
        cards = (await session.execute(
            select(ChannelLayout).where(
                ChannelLayout.channel_bot_id == bot.id,
                ChannelLayout.kind.in_(("season_card", "movie_card")),
                ChannelLayout.anilist_id.is_not(None),
                ChannelLayout.tg_message_id.is_not(None),
            ).order_by(ChannelLayout.seq)
        )).scalars().all()
        print(f"  cards     : {len(cards)} season/movie card(s) with a message id")
        for c in cards:
            print(f"    seq={c.seq} kind={c.kind} anilist_id={c.anilist_id} "
                  f"msg={c.tg_message_id}")
        packs = (await session.execute(
            select(StoragePack).where(StoragePack.anime_doc_id == anime_doc_id)
            .order_by(StoragePack.id)
        )).scalars().all()
        print(f"  packs     : {len(packs)} storage pack(s)")
        for p in packs:
            print(f"    pack id={p.id} res={p.resolution} entry={p.entry_id} "
                  f"season={p.season} files={len(p.file_message_ids or [])}")


async def _start_senku_client(env):
    """Start a bare Pyrogram client on Senku's token — the card author."""
    from pyrogram import Client

    token = env.distribution_bot_token
    if not token:
        raise RuntimeError("DISTRIBUTION_BOT_TOKEN is not set — cannot start Senku")
    client = Client(
        name="relink-senku",
        api_id=env.telegram_api_id, api_hash=env.telegram_api_hash,
        bot_token=token, no_updates=True, in_memory=True,
        workdir=str(env.session_path),
    )
    await client.start()
    return client


async def main(target: str, assume_yes: bool, dry_run: bool) -> None:
    from nekofetch.core.config import get_env

    env = get_env()
    print(f"  Target Postgres : {env.postgres_db} @ {env.postgres_host}")
    print(f"  Target          : {target}\n")

    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401 — register ORM tables

    client = None
    try:
        anime_doc_id = await _resolve_doc_id(container, target)
        if not anime_doc_id:
            print(f"could not resolve an anime_doc_id from {target!r} — aborting.")
            return
        print(f"anime_doc_id = {anime_doc_id}\n")

        print("current state:")
        await _describe(container, anime_doc_id)
        print()

        if dry_run:
            print("dry run — no edits made. Re-run with --yes to relink.")
            return

        if not assume_yes:
            ans = input(f"Relink the quality buttons for {anime_doc_id}? Type 'yes': ")
            if ans.strip().lower() != "yes":
                print("aborted")
                return

        client = await _start_senku_client(env)
        me = await client.get_me()
        print(f"editing as @{me.username} (Senku, id {me.id})\n")

        from kurosoden.shared.senku_publisher import SenkuPublisher

        result = await SenkuPublisher(container).relink_packs_in_place(
            client, anime_doc_id,
        )
        relinked = result.get("relinked", 0)
        print(f"relink complete: relinked={relinked} chat_id={result.get('chat_id')}")
        if relinked == 0:
            print("  NOTE relinked=0 — either no packs matched the cards, or the "
                  "edit was rejected. Check logs for 'senku.relink.*' entries.")
    finally:
        if client is not None:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Relink a published channel's quality buttons to fresh packs.")
    ap.add_argument("target", help="request code (REQ-1079) or anime_doc_id (153406)")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be relinked, edit nothing")
    args = ap.parse_args()
    asyncio.run(main(args.target.strip(), args.yes, args.dry_run))
