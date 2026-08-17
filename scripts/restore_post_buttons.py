"""Restore the inline quality buttons on a distribution post whose buttons were
WIPED by an earlier caption edit.

Background: Senku's "Edit caption" used to call editMessageCaption WITHOUT
re-supplying the inline keyboard, so Telegram dropped the post's quality buttons.
The handler bug is fixed going forward, but posts edited before the fix (e.g.
Akudama Drive) still show no buttons. Their ``button_data`` is intact in
``bot_content_posts`` (the caption edit only overwrote ``caption``), so this
rebuilds the keyboard from that stored payload and re-applies it in place.

    # by the exact post link (restores just that message) — dry run first
    python scripts/restore_post_buttons.py "https://t.me/c/1699000000/42" --dry-run
    python scripts/restore_post_buttons.py "https://t.me/c/1699000000/42" --yes

    # by REQ code or anime_doc_id (restores EVERY tracked post of that channel)
    python scripts/restore_post_buttons.py REQ-1079 --dry-run
    python scripts/restore_post_buttons.py 153406  --yes

Why a dedicated Senku client: the posts were authored by Senku (its bot token),
and Telegram only lets a bot edit a message it authored (or one its admin rights
allow). A bare Container has no ``admin_client``, and that client is the wrong
author anyway. So this starts a client on ``DISTRIBUTION_BOT_TOKEN`` (Senku) —
matching the owner's "it has to be Senku mode to add the link buttons".

It NEVER edits captions/images/text and NEVER reposts — only the reply markup,
rebuilt from ``button_data`` via the same ``build_audio_keyboard`` the publisher
uses. A post with no stored ``button_data`` is skipped (nothing to restore).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# ── ``kurosoden`` namespace bootstrap (mirrors relink_channel_buttons.py) ─────
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


async def _start_senku_client(env):
    """Start a bare Pyrogram client on Senku's token — the post author."""
    from pyrogram import Client

    token = env.distribution_bot_token
    if not token:
        raise RuntimeError("DISTRIBUTION_BOT_TOKEN is not set — cannot start Senku")
    client = Client(
        name="restore-buttons-senku",
        api_id=env.telegram_api_id, api_hash=env.telegram_api_hash,
        bot_token=token, no_updates=True, in_memory=True,
        workdir=str(env.session_path),
    )
    await client.start()
    return client


async def _resolve_doc_id(container, target: str) -> str | None:
    """Turn a REQ code into its anime_doc_id; pass a raw doc id through."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import Request as Req
    from nekofetch.infrastructure.database.postgres.session import session_scope

    if not target.upper().startswith("REQ-"):
        return target
    async with session_scope(container.pg_sessionmaker) as session:
        req = (await session.execute(
            select(Req).where(Req.code == target.upper())
        )).scalar_one_or_none()
        if req is None:
            return None
        fd = req.franchise_data or {}
        return req.anime_doc_id or (str(fd.get("anilist_id")) if fd.get("anilist_id") else None)


async def _posts_for_link(container, client, link: str):
    """Resolve ``(chat_id, [BotContentPost])`` for a single post link.

    Returns the ONE tracked post that matches the link's (channel, message id),
    or ``(chat_id, [])`` when the channel/post isn't tracked."""
    from sqlalchemy import select

    from bots.senku.handlers.post_caption_edit import _parse_post_link
    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
    )
    from nekofetch.infrastructure.database.postgres.session import session_scope

    parsed = _parse_post_link(link)
    if parsed is None:
        return None, []
    chat_ref, tg_message_id = parsed
    chat = await client.get_chat(chat_ref)
    chat_id = int(chat.id)
    async with session_scope(container.pg_sessionmaker) as session:
        bot = (await session.execute(
            select(DistributionBot).where(
                DistributionBot.is_channel.is_(True),
                DistributionBot.chat_id == chat_id,
            ).order_by(DistributionBot.id.desc())
        )).scalars().first()
        if bot is None:
            return chat_id, []
        rows = (await session.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot.id,
                BotContentPost.tg_message_id == tg_message_id,
            )
        )).scalars().all()
        return chat_id, [(bot.chat_id, r.tg_message_id, r.button_data, r.post_type)
                         for r in rows]


async def _posts_for_doc(container, anime_doc_id: str):
    """Resolve ``(chat_id, [posts])`` for every tracked post of a channel."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        BotContentPost,
        DistributionBot,
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
            return None, []
        rows = (await session.execute(
            select(BotContentPost).where(
                BotContentPost.bot_id == bot.id,
                BotContentPost.tg_message_id.is_not(None),
            ).order_by(BotContentPost.order)
        )).scalars().all()
        return bot.chat_id, [
            (bot.chat_id, r.tg_message_id, r.button_data, r.post_type) for r in rows
        ]


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
        is_link = "t.me/" in target or "telegram.me/" in target
        client = await _start_senku_client(env)
        me = await client.get_me()
        print(f"editing as @{me.username} (Senku, id {me.id})\n")

        if is_link:
            chat_id, posts = await _posts_for_link(container, client, target)
        else:
            doc_id = await _resolve_doc_id(container, target)
            if not doc_id:
                print(f"could not resolve an anime_doc_id from {target!r} — aborting.")
                return
            print(f"anime_doc_id = {doc_id}")
            chat_id, posts = await _posts_for_doc(container, doc_id)

        if chat_id is None:
            print("could not resolve the target post/channel — aborting.")
            return
        restorable = [p for p in posts if p[2]]  # button_data present
        print(f"  channel chat_id : {chat_id}")
        print(f"  tracked posts   : {len(posts)}  (with stored buttons: {len(restorable)})")
        for _cid, mid, bdata, ptype in posts:
            n = _button_count(bdata)
            print(f"    msg={mid} type={ptype} buttons={n}"
                  + ("" if n else "  (nothing to restore)"))

        if not restorable:
            print("\nNo posts with stored button_data — nothing to restore.")
            return
        if dry_run:
            print("\ndry run — no edits made. Re-run with --yes to restore.")
            return
        if not assume_yes:
            ans = input(f"\nRestore buttons on {len(restorable)} post(s)? Type 'yes': ")
            if ans.strip().lower() != "yes":
                print("aborted")
                return

        from nekofetch.services.bot_render import build_audio_keyboard

        fmt = container.config.post_format
        restored = failed = 0
        for cid, mid, bdata, ptype in restorable:
            try:
                markup = build_audio_keyboard(bdata, fmt)
                if markup is None:
                    print(f"  msg={mid}: button_data produced no keyboard — skipped")
                    continue
                await client.edit_message_reply_markup(cid, mid, reply_markup=markup)
                restored += 1
                print(f"  msg={mid} type={ptype}: restored "
                      f"{_button_count(bdata)} button(s)")
            except Exception as exc:  # noqa: BLE001 — report + continue
                failed += 1
                print(f"  msg={mid} type={ptype}: FAILED — {exc}")

        print(f"\nDone. restored={restored} failed={failed}")
        if failed:
            print("  Some edits failed — if the channel was created via a userbot "
                  "(not Senku), those posts must be restored with that userbot "
                  "session instead.")
    finally:
        if client is not None:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        await container.shutdown()


def _button_count(button_data) -> int:
    """Best-effort count of URL buttons a payload would render (for reporting)."""
    if not isinstance(button_data, dict):
        return 0
    if button_data.get("type") == "custom":
        return len(button_data.get("buttons") or [])
    links = button_data.get("links") or {}
    return len(links)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Restore wiped inline buttons on a distribution post.")
    ap.add_argument("target",
                    help="post link (https://t.me/c/…/42), REQ code (REQ-1079), "
                         "or anime_doc_id (153406)")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be restored, edit nothing")
    args = ap.parse_args()
    asyncio.run(main(args.target.strip(), args.yes, args.dry_run))
