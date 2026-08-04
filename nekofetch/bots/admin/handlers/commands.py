from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand, Message

from nekofetch.bots.fsm import FSM
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import Role
from nekofetch.localization import messages as messages_mod
from nekofetch.localization.messages import LANG_DIR, M, t
from nekofetch.ui.typography import bq, rule

log = get_logger(__name__)

# Slash commands registered with Telegram. Descriptions come from the catalog so
# editing en.json updates the in-app command menu too.
_COMMAND_KEYS = (
    ("start", M.CMD_START),
    ("help", M.CMD_HELP),
    ("cancel", M.CMD_CANCEL),
    ("batch", M.CMD_BATCH),
    ("reload", M.CMD_RELOAD),
    ("checkbans", M.CMD_CHECKBANS),
    ("cleardownloads", M.CMD_CLEARDOWNLOADS),
    ("resetoverrides", M.CMD_RESETOVERRIDES),
    ("checkupdates", M.CMD_CHECKUPDATES),
    ("recovermain", M.CMD_RECOVERMAIN),
    ("recoverindex", M.CMD_RECOVERINDEX),
)


def admin_commands() -> list[BotCommand]:
    return [BotCommand(name, t(key)) for name, key in _COMMAND_KEYS]


# Back-compat constant + helper (older imports referenced these).
ADMIN_COMMANDS = admin_commands()


async def publish_admin_commands(client: Client) -> None:
    await client.set_bot_commands(admin_commands())


def _help_text(role: Role) -> str:
    blocks = [
        t(M.HELP_TITLE),
        t(M.HELP_INTRO),
        f"<i>{rule()}</i>",
        t(M.HELP_H_COMMANDS),
        bq(t(M.HELP_CMD_START)),
        bq(t(M.HELP_CMD_HELP)),
        bq(t(M.HELP_CMD_CANCEL)),
        t(M.HELP_H_EVERYONE),
        bq(t(M.HELP_CAP_REQUEST)),
        bq(t(M.HELP_CAP_MYREQ)),
    ]
    if role in (Role.STAFF, Role.ADMIN):
        blocks += [
            t(M.HELP_H_STAFF),
            bq(t(M.HELP_CMD_BATCH)),
            bq(t(M.HELP_CAP_REVIEW)),
            bq(t(M.HELP_CAP_QUEUE)),
            bq(t(M.HELP_CAP_APPROVALS)),
        ]
    if role is Role.ADMIN:
        blocks += [t(M.HELP_H_ADMIN), bq(t(M.HELP_CAP_ADMIN))]
    return "\n\n".join(blocks)


def register(client: Client, container: Container) -> None:
    from nekofetch.services.auth_service import AuthService

    fsm = FSM(container.redis, bot="admin")
    auth = AuthService(container)

    def _role(message: Message) -> Role:
        user = getattr(message, "nf_user", None)
        return Role(user.role) if user else Role.USER

    def _is_owner(message: Message) -> bool:
        return auth.is_owner(getattr(message, "nf_user", None))

    @client.on_message(filters.command("help"))
    async def _help(_: Client, message: Message) -> None:
        await message.reply(_help_text(_role(message)), parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("cancel"))
    async def _cancel(_: Client, message: Message) -> None:
        await fsm.clear(message.from_user.id)
        await message.reply(t(M.CANCELLED), parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("reload"))
    async def _reload(_: Client, message: Message) -> None:
        # Owner-only: re-read en.json from disk so text edits apply without a
        # restart. Shows the exact file path + key count so you can confirm the
        # bot is reading the file you think it is.
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        messages_mod.reload()
        count = len(messages_mod.localizer._catalogs.get("en", {}))
        await message.reply(
            t(M.RELOAD_DONE, count=count, path=str(LANG_DIR / "en.json")),
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command("cleardownloads"))
    async def _clear_downloads(_: Client, message: Message) -> None:
        # Owner-only: wipe stale/active download state — cancels every
        # queued/running/orphaned job and clears live progress so ACTIVE TASKS
        # reflects reality. Use when a ghost download is stuck showing as active.
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        from nekofetch.services.queue_service import QueueService

        n = await QueueService(container).cancel_all_active()
        from nekofetch.services.log_channel_service import LogChannelService
        await LogChannelService(container).refresh_active()
        await message.reply(t(M.DOWNLOADS_CLEARED, count=n), parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("checkupdates"))
    async def _checkupdates(_: Client, message: Message) -> None:
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        from nekofetch.services.update_check_service import UpdateCheckService

        status = await message.reply(
            "🔍 <b>Checking all published anime for new entries…</b>",
            parse_mode=ParseMode.HTML,
        )
        results = await UpdateCheckService(container).check_all()
        total_new = sum(len(r.new_entries) for r in results)
        failed = [r for r in results if r.error]

        lines = [f"<b>✅ Update check complete</b>"]
        lines.append(f"<b>Checked:</b> {len(results)} anime")
        if total_new:
            lines.append(f"<b>New entries found:</b> {total_new}")
            for r in results:
                for ne in r.new_entries:
                    label = f"Season {ne.season_number:02d}" if ne.season_number else ne.format
                    lines.append(f"  • {r.title} — {label} ({ne.english_title})")
        else:
            lines.append("<b>No new entries found.</b>")
        if failed:
            lines.append(f"<b>Errors:</b> {len(failed)} anime")
            for f in failed:
                lines.append(f"  ⚠️ {f.title}: {f.error}")
        try:
            await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
        log.info("checkupdates.done", checked=len(results), new=total_new)

    @client.on_message(filters.command("checkbans"))
    async def _checkbans(_: Client, message: Message) -> None:
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        mgr = getattr(container, "bot_manager", None)
        if mgr is None:
            await message.reply("Bot manager not available.", parse_mode=ParseMode.HTML)
            return
        status = await message.reply("Checking all entities", parse_mode=ParseMode.HTML)
        async def _progress(name: str, state: str) -> None:
            emoji = {"checking": "", "healthy": "OK", "banned": "BAN", "recreated": "NEW", "failed": "FAIL"}
            e = emoji.get(state, "?")
            try:
                await status.edit_text(f"{e} {name}  [{state}]", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        result = await mgr.check_all_entities(alert=True, on_progress=_progress)
        await status.edit_text(
            f"Checked: {result['checked']}  Healthy: {result['healthy']}  "
            f"Banned: {result['banned']}  Recreated: {result['recreated']}  "
            f"Failed: {result['failed']}",
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command("recovermain"))
    async def _recover_main(_: Client, message: Message) -> None:
        """Restore the main channel onto a fresh channel after a ban.

        Usage: /recovermain <new_channel_id>
        The operator creates the new channel first, then passes its numeric id
        (e.g. -1001234567890) here. The command restores every backed-up post
        verbatim, re-points config, and triggers a re-publish so Download invite
        links refresh to the new channel.
        """
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "<b>Usage:</b> <code>/recovermain &lt;new_channel_id&gt;</code>\n\n"
                "Restores every backed-up main-channel post onto the fresh channel, "
                "re-points config, and refreshes Download invite links.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            new_id = int(parts[1])
        except ValueError:
            await message.reply("Channel id must be numeric (e.g. -1001234567890).")
            return

        from nekofetch.services.backup_service import BackupService

        status = await message.reply("🔄 Restoring main channel…", parse_mode=ParseMode.HTML)
        try:
            stats = await BackupService(container).restore_to_channel(new_id)
            await status.edit_text(
                f"✅ <b>Main channel restored</b>\n\n"
                f"<b>Total:</b> {stats.total}\n"
                f"<b>Restored:</b> {stats.restored}\n"
                f"<b>Failed:</b> {stats.failed}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.error("recovermain.failed", error=str(exc))
            await status.edit_text(f"❌ Restore failed: {str(exc)[:300]}", parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("recoverindex"))
    async def _recover_index(_: Client, message: Message) -> None:
        """Restore the index channel onto a fresh channel after a ban.

        Usage: /recoverindex <new_channel_id> [<new_username>]
        The operator creates the new channel first (with a public @handle for
        deep-links), then passes its numeric id here. The command restores every
        index section in order, remaps section message ids, and re-points config.
        """
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "<b>Usage:</b> <code>/recoverindex &lt;new_channel_id&gt; [&lt;username&gt;]</code>\n\n"
                "Restores the index channel onto the fresh channel, remaps section "
                "message ids, and re-points config.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            new_id = int(parts[1])
        except ValueError:
            await message.reply("Channel id must be numeric (e.g. -1001234567890).")
            return
        new_username = parts[2].lstrip("@") if len(parts) > 2 else None

        from nekofetch.services.backup_service import BackupService

        status = await message.reply("🔄 Restoring index channel…", parse_mode=ParseMode.HTML)
        try:
            stats = await BackupService(container).restore_index(new_id, new_username=new_username)
            await status.edit_text(
                f"✅ <b>Index channel restored</b>\n\n"
                f"<b>Total:</b> {stats.total}\n"
                f"<b>Restored:</b> {stats.restored}\n"
                f"<b>Failed:</b> {stats.failed}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.error("recoverindex.failed", error=str(exc))
            await status.edit_text(f"❌ Restore failed: {str(exc)[:300]}", parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("resetoverrides"))
    async def _reset_overrides(_: Client, message: Message) -> None:
        # Owner-only: clear Mongo runtime overrides that shadow config.yaml.
        if not _is_owner(message):
            await message.reply(t(M.OWNER_ONLY), parse_mode=ParseMode.HTML)
            return
        from nekofetch.services.settings_service import SettingsService

        cleared = await SettingsService(container).clear_overrides()
        await message.reply(t(M.OVERRIDES_CLEARED, count=cleared), parse_mode=ParseMode.HTML)
