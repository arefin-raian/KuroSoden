"""Repair main-channel posts that were damaged by an earlier caption edit.

Two things can be wrong with a live main post:
  * its Index/Download buttons were STRIPPED (an ``edit_message_caption`` without
    ``reply_markup`` — the bug that hit 4 posts during the language-fix run), and/or
  * its language line still uses the old 3-way " & " join
    ("English & Japanese & Hindi" instead of "English, Japanese & Hindi").

The code is fixed (``main_channel_service`` re-passes the keyboard and uses the
Oxford join), so any FUTURE publish/redo is correct. This repairs ALREADY-POSTED
ones by making Gojo (the main-channel author) re-render + edit them in place from
current facts — which restores the buttons AND applies the Oxford join in one go.

    # DEFAULT: scan every live main post, list only the ACTUALLY-broken ones
    #   (missing buttons or bad " & " join). Edits nothing.
    python scripts/fix_main_caption.py --dry-run

    # REPAIR only the broken posts (re-render in place via MainChannelService)
    python scripts/fix_main_caption.py --yes

    # REPAIR one title only (anime_doc_id or REQ code)
    python scripts/fix_main_caption.py --doc 116006 --yes
    python scripts/fix_main_caption.py --code REQ-1088 --yes

    # BLUNT: force re-render EVERY live main post (no inspection — rarely needed;
    #   this also touches posts you may have hand-edited)
    python scripts/fix_main_caption.py --all-published --yes

The default no longer trusts a stored-caption heuristic: it reads each LIVE
message and only targets the ones missing a keyboard or still showing the bad
join, so healthy/hand-edited posts are left untouched. Uses the ADMIN bot client
(the identity that authors the main-channel post) — editing with the wrong
client silently no-ops.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

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

# A caption with the OLD bug: three (or more) languages joined only by " & ",
# e.g. "English & Japanese & Hindi". The fixed form has a comma before the
# penultimate language, so this pattern no longer matches a corrected caption.
_BAD_JOIN = re.compile(r"[A-Z][a-z]+ & [A-Z][a-z]+ & [A-Z][a-z]+")


async def _start_admin_client(env):
    """Start a bare client on the ADMIN token — the main-channel post author."""
    from pyrogram import Client

    token = (getattr(env, "admin_bot_token", "") or "").strip()
    label = "admin"
    if not token:  # fall back to the publisher (Gojo) token
        token = (getattr(env, "publisher_bot_token", "") or "").strip()
        label = "gojo"
    if not token:
        raise RuntimeError("neither ADMIN_BOT_TOKEN nor PUBLISHER_BOT_TOKEN is set")
    client = Client(
        name=f"fixcap-{label}",
        api_id=env.telegram_api_id, api_hash=env.telegram_api_hash,
        bot_token=token, no_updates=True, in_memory=True,
        workdir=str(env.session_path),
    )
    await client.start()
    return client, label


async def _resolve_doc(container, code: str | None, doc: str | None) -> str | None:
    if doc:
        return doc
    if not code:
        return None
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import Request
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        req = (await session.execute(
            select(Request).where(Request.code == code.upper())
        )).scalar_one_or_none()
        return req.anime_doc_id if req is not None else None


async def _all_main_docs(container) -> list[tuple[str, str]]:
    """Every live main-channel post — for a full forced re-render (rarely needed;
    prefer the default live-damage scan). Kept behind --all-published."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import ChannelPost
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        posts = (await session.execute(
            select(ChannelPost).where(ChannelPost.main_message_id.is_not(None))
        )).scalars().all()
    return [(p.anime_doc_id, "(forced re-render)") for p in posts]


async def _damaged_docs(container, client) -> list[tuple[str, str]]:
    """Only the main posts that are ACTUALLY broken — inspected LIVE.

    A post needs repair when its live message either (a) has NO inline keyboard
    (buttons were stripped by an earlier caption edit) or (b) still shows the
    3-way ' & ' language join. Everything else — including posts you hand-edited —
    is left untouched. Requires a connected client (reads the live message)."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import ChannelPost
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        posts = (await session.execute(
            select(ChannelPost).where(ChannelPost.main_message_id.is_not(None))
        )).scalars().all()
        rows = [(p.anime_doc_id, p.main_channel_id, p.main_message_id) for p in posts]

    out: list[tuple[str, str]] = []
    main_cfg_id = container.config.main_channel.channel_id
    for doc, chan, mid in rows:
        chat_id = chan or main_cfg_id
        if not chat_id or not mid:
            continue
        try:
            live = await client.get_messages(int(chat_id), int(mid))
        except Exception as exc:  # noqa: BLE001 — can't read → skip, don't touch
            print(f"  doc={doc}: (skipped — couldn't read live msg: {exc})")
            continue
        has_buttons = bool(getattr(live, "reply_markup", None))
        cap = getattr(getattr(live, "caption", None), "html", None) \
            or (getattr(live, "caption", None) or getattr(live, "text", None) or "")
        cap = str(cap)
        bad_join = bool(_BAD_JOIN.search(cap))
        if not has_buttons and bad_join:
            out.append((doc, "no buttons + bad ' & ' join"))
        elif not has_buttons:
            out.append((doc, "missing buttons"))
        elif bad_join:
            out.append((doc, "bad ' & ' join"))
    return out


async def main(code: str | None, doc: str | None, assume_yes: bool,
               dry_run: bool, all_published: bool = False) -> None:
    from nekofetch.core.config import get_env

    env = get_env()
    print(f"  Target Postgres : {env.postgres_db} @ {env.postgres_host}\n")

    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()
    import kurosoden.shared.models  # noqa: F401

    client = None
    try:
        # Explicit single-target modes don't need a live scan — resolve up front.
        explicit: list[tuple[str, str]] | None = None
        if code or doc:
            one = await _resolve_doc(container, code, doc)
            if not one:
                print(f"could not resolve an anime_doc_id from "
                      f"{code or doc!r} — aborting.")
                return
            explicit = [(one, "(explicit)")]

        # Attach as the main-post author FIRST. The default scan reads each live
        # message to decide what's actually broken, and every mode edits as this
        # identity — editing with the wrong client silently no-ops.
        client, label = await _start_admin_client(env)
        me = await client.get_me()
        print(f"editing as @{me.username} ({label}, id {me.id})\n")
        container.admin_client = client  # type: ignore[attr-defined]

        # Which docs to repair.
        if explicit is not None:
            targets = explicit
        elif all_published:
            # Blunt: EVERY live main post, no inspection. Rarely what you want —
            # it also re-renders posts you may have hand-edited. Prefer the
            # default live-damage scan.
            targets = await _all_main_docs(container)
        else:
            # DEFAULT: inspect each live post and target only the broken ones
            # (missing Index/Download buttons, or still-bad ' & ' join).
            print("scanning live main posts for damage "
                  "(missing buttons / bad ' & ' join)…")
            targets = await _damaged_docs(container, client)

        print(f"\nmain posts to fix: {len(targets)}")
        for d, sample in targets:
            print(f"  doc={d}   {sample}")
        if not targets:
            print("nothing to fix — every live main post has its buttons and a "
                  "correct language line.")
            return
        if dry_run:
            print("\ndry run — no edits. Re-run with --yes to repair.")
            return
        if not assume_yes:
            ans = input(f"\nRe-edit {len(targets)} main-channel post(s)? "
                        "Type 'yes': ")
            if ans.strip().lower() != "yes":
                print("aborted")
                return

        from nekofetch.services.main_channel_service import MainChannelService
        svc = MainChannelService(container)
        fixed = failed = 0
        for d, _sample in targets:
            try:
                ok = await svc.refresh_caption(d)
            except Exception as exc:  # noqa: BLE001
                ok = False
                print(f"  doc={d}: ERROR {exc}")
            if ok:
                fixed += 1
                print(f"  doc={d}: caption + buttons re-rendered ✓")
            else:
                failed += 1
                print(f"  doc={d}: FAILED (no live post, or edit rejected)")
        print(f"\nDone. fixed={fixed} failed={failed}")
    finally:
        if client is not None:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        await container.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Re-render main-channel posts (fix language separator + "
                    "rebuild Index/Download buttons).")
    ap.add_argument("--code", help="target one request code (e.g. REQ-1088)")
    ap.add_argument("--doc", help="target one anime_doc_id (e.g. 116006)")
    ap.add_argument("--all-published", action="store_true",
                    help="BLUNT: force re-render EVERY live main post without "
                         "inspection (also touches hand-edited posts; prefer the "
                         "default live-damage scan)")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan + list only the broken posts, edit nothing")
    args = ap.parse_args()
    asyncio.run(main(args.code, args.doc, args.yes, args.dry_run,
                     args.all_published))
