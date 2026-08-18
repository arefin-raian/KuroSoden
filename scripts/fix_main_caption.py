"""Re-edit main-channel post captions that used the wrong language separator.

The multi-audio language line joined every language with " & "
("English & Japanese & Hindi") instead of the Oxford form
("English, Japanese & Hindi"). The code is fixed (main_channel_service.
_language_summary), so any FUTURE publish/redo is correct — this repairs the
ALREADY-POSTED captions by making Gojo (the main-channel author) re-render + edit
them in place from the current facts.

    # DRY-RUN: list main posts whose stored caption has the 3-way "A & B & C" join
    python scripts/fix_main_caption.py --dry-run

    # REPAIR every affected main post (re-edit in place via MainChannelService)
    python scripts/fix_main_caption.py --yes

    # REPAIR one title only (anime_doc_id or REQ code)
    python scripts/fix_main_caption.py --doc 116006 --yes
    python scripts/fix_main_caption.py --code REQ-1088 --yes

Uses the ADMIN bot client (the identity that authors/edits the main-channel post)
— editing with the wrong client silently no-ops. refresh_caption re-derives facts
from current packs (so the Oxford join applies), edits the live caption, and
updates the wipe-proof backup. Media + buttons are untouched.
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


async def _affected_docs(container) -> list[tuple[str, str]]:
    """Every main post whose BACKUP caption still has the 3-way ' & ' join.

    Returns ``(anime_doc_id, sample_line)``. The durable backup caption
    (PublishedPostBackup) is the reliable stored copy of what's live."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import (
        ChannelPost,
        PublishedPostBackup,
    )
    from nekofetch.infrastructure.database.postgres.session import session_scope

    out: list[tuple[str, str]] = []
    async with session_scope(container.pg_sessionmaker) as session:
        posts = (await session.execute(
            select(ChannelPost).where(ChannelPost.main_message_id.is_not(None))
        )).scalars().all()
        doc_ids = [p.anime_doc_id for p in posts]
        backups = {
            b.anime_doc_id: b for b in (await session.execute(
                select(PublishedPostBackup).where(
                    PublishedPostBackup.anime_doc_id.in_(doc_ids or [""]),
                )
            )).scalars().all()
        }
    for p in posts:
        cap = getattr(backups.get(p.anime_doc_id), "caption", None) or ""
        m = _BAD_JOIN.search(cap)
        if m:
            out.append((p.anime_doc_id, f"old: {m.group(0)!r}"))
    return out


async def _all_main_docs(container) -> list[tuple[str, str]]:
    """Every live main-channel post — for a full re-render (fixes the caption AND
    rebuilds the Index/Download buttons, e.g. after an edit stripped them)."""
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import ChannelPost
    from nekofetch.infrastructure.database.postgres.session import session_scope

    async with session_scope(container.pg_sessionmaker) as session:
        posts = (await session.execute(
            select(ChannelPost).where(ChannelPost.main_message_id.is_not(None))
        )).scalars().all()
    return [(p.anime_doc_id, "(re-render: caption + buttons)") for p in posts]


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
        # Which docs to repair.
        if code or doc:
            one = await _resolve_doc(container, code, doc)
            if not one:
                print(f"could not resolve an anime_doc_id from "
                      f"{code or doc!r} — aborting.")
                return
            targets = [(one, "(explicit)")]
        elif all_published:
            # Every live main post — used to REBUILD buttons after a caption edit
            # already stripped them (so the 3-way-'&' detector no longer matches).
            targets = await _all_main_docs(container)
        else:
            targets = await _affected_docs(container)

        print(f"main posts to fix: {len(targets)}")
        for d, sample in targets:
            print(f"  doc={d}   {sample}")
        if not targets:
            print("nothing to fix.")
            return
        if dry_run:
            print("\ndry run — no edits. Re-run with --yes to repair.")
            return
        if not assume_yes:
            ans = input(f"\nRe-edit {len(targets)} main-channel caption(s)? "
                        "Type 'yes': ")
            if ans.strip().lower() != "yes":
                print("aborted")
                return

        # Attach as the main-post author + hand the client to the container so
        # MainChannelService.refresh_caption edits with the right identity.
        client, label = await _start_admin_client(env)
        me = await client.get_me()
        print(f"\nediting as @{me.username} ({label}, id {me.id})\n")
        container.admin_client = client  # type: ignore[attr-defined]

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
                print(f"  doc={d}: caption re-edited ✓")
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
                    help="re-render EVERY live main post (restores buttons an "
                         "earlier caption edit stripped)")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the posts that would be re-rendered, edit nothing")
    args = ap.parse_args()
    asyncio.run(main(args.code, args.doc, args.yes, args.dry_run,
                     args.all_published))
