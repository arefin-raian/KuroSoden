"""Terminal UI — cyberpunk-styled console output using rich.

Provides:
- ``StartupTracker`` — accumulates startup events for a one-time dashboard.
- ``render_startup_dashboard()`` — renders a beautiful panel-based startup summary.
- ``rich_processor()`` — structlog processor that replaces the default
  key=value ConsoleRenderer with clean, human-readable styled lines.
- Error collection — warnings/errors buffered and displayed in a diagnostics panel.
"""

from __future__ import annotations

import traceback
from collections import defaultdict
from dataclasses import dataclass, field

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# ── colour palette ─────────────────────────────────────────────────────────
CYAN = "#00ffcc"
MAGENTA = "#ff00ff"
GREEN = "#39ff14"
YELLOW = "#ffd700"
RED = "#ff3333"
DIM = "dim"
BOLD = "bold"


# ── startup tracker ────────────────────────────────────────────────────────

@dataclass
class StartupTracker:
    """Mutable bag that services push updates into during boot."""

    version: str = ""
    build: str = ""

    channels: dict[str, dict] = field(default_factory=dict)
    # channel name → {title, id, ok: bool}

    bots_loaded: int = 0
    bots_failed: int = 0
    bot_names: list[str] = field(default_factory=list)

    services: dict[str, bool] = field(default_factory=dict)
    # service name → ok

    errors: list[str] = field(default_factory=list)

    def channel_ok(self, name: str, cid: int, title: str) -> None:
        self.channels[name] = {"id": cid, "title": title, "ok": True}

    def channel_fail(self, name: str, cid: int) -> None:
        if name not in self.channels:
            self.channels[name] = {"id": cid, "title": "?", "ok": False}

    def channel_disabled(self, name: str, cid: int = 0) -> None:
        """Record a configured-but-inactive channel (disabled or no id) so the
        dashboard reports it explicitly instead of silently omitting it."""
        if name not in self.channels:
            self.channels[name] = {"id": cid, "title": "—", "ok": None}

    def service_ok(self, name: str) -> None:
        self.services[name] = True

    def service_fail(self, name: str) -> None:
        self.services[name] = False

    def add_error(self, text: str) -> None:
        self.errors.append(text)


# ── startup dashboard ──────────────────────────────────────────────────────

def render_startup_dashboard(tracker: StartupTracker) -> None:
    """Print a cyberpunk-themed startup summary to the terminal."""

    # ── header ──
    header = Panel(
        Text.from_markup(
            f"[{MAGENTA} bold]◈  NEKOFETCH  [/{MAGENTA} bold]"
            f"[{CYAN}]{tracker.version}[/{CYAN}]  "
            f"[dim]·  build {tracker.build}  ·[/dim]\n"
            f"[dim]system cortex online[/dim]",
            justify="center",
        ),
        border_style=CYAN,
        padding=(1, 2),
    )
    console.print(header)
    console.print()

    # ── channels table ──
    _render_channels(tracker)
    console.print()

    # ── services table ──
    _render_services(tracker)

    # ── diagnostics (errors) ──
    if tracker.errors:
        _render_errors(tracker)
    else:
        console.print(
            Panel(
                f"[{GREEN} dim]✔ No anomalies detected.[/{GREEN} dim]",
                border_style="dim green",
                title="diagnostics",
                title_align="left",
            )
        )

    console.print()


def _render_channels(tracker: StartupTracker) -> None:
    if not tracker.channels:
        return

    # Friendly labels so the operator recognizes each channel's role — notably
    # "storage" is the database channel where packs are archived.
    labels = {
        "storage": "database",
        "main": "main",
        "index": "index",
        "log": "log",
        "thumbnail": "thumbnail",
    }

    table = Table(
        title="channels",
        title_style=f"{CYAN} bold",
        border_style=CYAN,
        show_header=True,
        header_style=f"{MAGENTA} bold",
        expand=True,
    )
    table.add_column("status", width=6)
    table.add_column("channel", style=CYAN, width=14)
    table.add_column("title", style="white")
    table.add_column("id", style=DIM)

    for name, info in tracker.channels.items():
        ok = info.get("ok")
        display = labels.get(name, name)
        if ok is True:
            status = f"[{GREEN}]●[/{GREEN}]"
            title = str(info.get("title", "?"))
        elif ok is False:
            status = f"[{RED}]✖[/{RED}]"
            title = str(info.get("title", "?"))
        else:
            status = f"[{DIM}]○[/{DIM}]"
            title = f"[{DIM}]disabled[/{DIM}]"
        table.add_row(
            status,
            f"[bold]{display}[/bold]",
            title,
            str(info.get("id", "?")),
        )

    console.print(table)


def _render_services(tracker: StartupTracker) -> None:
    table = Table(
        title="services",
        title_style=f"{CYAN} bold",
        border_style=CYAN,
        show_header=True,
        header_style=f"{MAGENTA} bold",
        expand=True,
    )
    table.add_column("status", width=6)
    table.add_column("service", style=CYAN)

    for name, ok in tracker.services.items():
        status = f"[{GREEN}]●[/{GREEN}]" if ok else f"[{RED}]✖[/{RED}]"
        table.add_row(status, _service_label(name))

    # Add distribution bots if any
    if tracker.bots_loaded > 0 or tracker.bots_failed > 0:
        total = tracker.bots_loaded + tracker.bots_failed
        bot_status = (
            f"[{GREEN}]{tracker.bots_loaded}/{total} loaded[/{GREEN}]"
            if tracker.bots_failed == 0
            else f"[{YELLOW}]{tracker.bots_loaded}/{total} loaded, "
                 f"[{RED}]{tracker.bots_failed} failed[/{RED}]"
        )
        table.add_row(
            f"[{CYAN}]◇[/{CYAN}]",
            f"Distribution bots · {bot_status}",
        )
        if tracker.bot_names:
            names = ", ".join(tracker.bot_names[:8])
            suffix = " …" if len(tracker.bot_names) > 8 else ""
            table.add_row("", f"[dim]{names}{suffix}[/dim]")

    console.print(table)


def _render_errors(tracker: StartupTracker) -> None:
    lines = "\n".join(
        f"[{RED}]⚠[/{RED}] [{YELLOW}]{e[:120]}[/{YELLOW}]"
        for e in tracker.errors[:8]
    )
    if len(tracker.errors) > 8:
        lines += f"\n[dim]… and {len(tracker.errors) - 8} more[/dim]"

    console.print(
        Panel(
            lines,
            border_style=RED,
            title=f"diagnostics  [{RED}]{len(tracker.errors)} events[/{RED}]",
            title_align="left",
        )
    )


def _service_label(name: str) -> str:
    """Human-readable label for internal service names."""
    return {
        "admin": "Admin Bot",
        "log_channel": "Log Channel",
        "thumbnail_channel": "Thumbnail Channel",
        "stats": "Stats Refresh",
        "download_worker": "Download Worker",
        "shift_afk": "Shift AFK Monitor",
        "temp_links": "Temp Links Sweep",
        "scheduler": "Task Scheduler",
        "conn_watchdog": "Connection Watchdog",
        "health_check": "Health Check",
        "full_check": "Entity Full Check",
        "update_check": "Update Check",
    }.get(name, name.replace("_", " ").title())


# ── runtime log rendering ──────────────────────────────────────────────────

_HUMAN_EVENTS: dict[str, str] = {
    # --- container / core ---
    "nekofetch.starting": "NekoFetch starting",
    "nekofetch.stopping": "NekoFetch stopping",
    "container.startup": "Database connections established",
    "container.shutdown": "Shutting down",
    # --- channels ---
    "bots.channel.ok": "channel resolved",
    "bots.channel.unreachable": "channel unreachable",
    "bots.channel.retrying": "retrying channel",
    "bots.channel.resolved": "channel recovered",
    "bots.channel.retry_exhausted": "channel retry exhausted",
    # --- admin / manager ---
    "bots.admin.started": "Admin bot online",
    "bots.log_channel.ready": "Log channel ready",
    "bots.thumbnail_channel.ready": "Thumbnail channel ready",
    "bots.scheduler.started": "Task scheduler started",
    "bots.conn_watchdog.started": "Connection watchdog started",
    "bots.health_check.started": "Health check started",
    "bots.full_check.scheduled": "Full entity check scheduled",
    "bots.temp_links_sweep.scheduled": "Temp links sweep scheduled",
    "bots.shift_afk.scheduled": "Shift AFK monitor scheduled",
    "update_check.scheduled": "Update check scheduled",
    # --- distribution ---
    "bots.distribution.started": "distribution bot started",
    "bots.distribution.failed": "distribution bot failed",
    "bots.distribution.loaded": "distribution bots loaded",
    "bots.distribution.added": "distribution bot added",
    # --- workers ---
    "worker.download.started": "Download worker started",
    "stats.refreshed_on_startup": "Stats refreshed",
    "stats.refresh.skipped_no_published_yet": "Stats skipped (no published yet)",
    "stats.refresh.failed": "Stats refresh failed",
    # --- downloads ---
    "download.worker.start": "Download worker started",
    "download.disk_space_chunking": "disk space check passed",
    "download.unit.failed": "download failed",
    "download.variants.failed": "download variants failed",
    # --- processing ---
    "processing.stage": "processing stage",
    "processing.complete": "processing complete",
    "processing.cancelled": "processing cancelled",
    # --- publishing ---
    "mainchannel.published": "published to main channel",
    "mainchannel.publish.failed": "publish failed",
    "storage.delivered": "storage delivered",
    "storage.pack.persisted": "pack persisted",
    "storage.cleanup.done": "storage cleanup done",
    # --- thumbnails ---
    "thumbcc.rebuilt": "thumbnail channel rebuilt",
    "thumbcc.queued": "thumbnail queued",
    "thumbcc.thumbnail_generated": "thumbnail generated",
    # --- shift ---
    "shift.assigned": "shift assigned",
    "shift.released": "shift released",
    "shift.afk_released": "AFK auto-release",
    "shift.takeover_requested": "takeover requested",
    "shift.takeover_approved": "takeover approved",
    # --- bots ---
    "bot.registered": "bot registered",
    "bot.orchestrator.created": "bot created",
    "bot.branding.applied": "bot branding applied",
    "bot.content.generated": "bot content generated",
    "channel.registered": "channel registered",
    # --- kuro-soden pipeline ---
    "kuro-soden.channel.ok": "channel connected",
    "kuro-soden.channel.unreachable": "channel unreachable",
    "kuro-soden.channel.disabled": "channel disabled",
    # --- index ---
    "index.sections.seeded": "index channel seeded",
    # --- userbot ---
    "userbot.accounts.loaded": "userbot accounts loaded",
    "userbot.active": "userbot active",
    # --- scheduler ---
    "scheduler.start": "scheduler started",
}


def _humanize_event(event: str, kv: dict) -> str:
    """Convert a structlog-style event name + key-value pairs into a human sentence."""
    base = _HUMAN_EVENTS.get(event, event.replace("_", " ").replace(".", " › "))

    # Embellish with key context fields
    if event in ("bots.channel.ok", "kuro-soden.channel.ok"):
        title = kv.get("title", "")
        name = kv.get("channel", "")
        return f"{base} · {name} [{title}]"
    if event in ("bots.channel.unreachable", "kuro-soden.channel.unreachable"):
        name = kv.get("channel", "")
        return f"{base} · {name}"
    if event == "kuro-soden.channel.disabled":
        name = kv.get("channel", "")
        return f"{base} · {name}"
    if event == "bots.distribution.started":
        bot = kv.get("bot", "")
        return f"  {base} · {bot}"
    if event == "bots.distribution.failed":
        eid = kv.get("id", "")
        return f"  {base} · id={eid}"
    if event == "bots.distribution.loaded":
        loaded = kv.get("loaded", 0)
        failed = kv.get("failed", 0)
        return f"  {base} · {loaded} ok, {failed} failed"
    if event == "thumbcc.queued":
        return f"  {base} · {kv.get('title', '?')} ({kv.get('entries', 0)} entries)"
    if event == "mainchannel.published":
        return f"  {base} · {kv.get('anime', '?')}"
    if event == "download.unit.failed":
        return f"  {base} · S{kv.get('season', '?')}E{kv.get('episode', '?')}"
    if event == "shift.assigned":
        return f"  {base} · {kv.get('channel', '?')} → {kv.get('name', kv.get('user', '?'))}"

    return base


def rich_processor(_, method_name: str, event_dict: dict) -> str:
    """Structlog processor: render events as clean, styled rich lines.

    Raises ``structlog.DropEvent`` after printing so the default renderer
    never fires.
    """
    import structlog

    event = event_dict.pop("event", "")
    level = event_dict.pop("level", "info").lower()
    timestamp = event_dict.pop("timestamp", "")
    exc_info = event_dict.pop("exc_info", None)

    message = _humanize_event(event, event_dict)

    # Append any remaining kv pairs the event didn't consume
    if event_dict:
        extras = " ".join(
            f"[{DIM}]{k}={v}[/{DIM}]" for k, v in event_dict.items()
        )
        message = f"{message}  {extras}"

    # Build styled output
    ts = f"[{DIM}]{timestamp}[/{DIM}]" if timestamp else ""

    if level in ("error", "critical"):
        prefix = f"[{RED} bold]✖[/{RED} bold]"
        msg_style = RED
    elif level == "warning":
        prefix = f"[{YELLOW}]⚠[/{YELLOW}]"
        msg_style = YELLOW
    elif level == "debug":
        prefix = f"[{DIM}]·[/{DIM}]"
        msg_style = DIM
    else:
        prefix = f"[{CYAN}]●[/{CYAN}]"
        msg_style = "white"

    console.print(f"{ts} {prefix} [{msg_style}]{message}[/{msg_style}]")

    # Render traceback if an exception was attached
    if exc_info:
        tb = "".join(traceback.format_exception(*exc_info))
        for line in tb.rstrip().split("\n"):
            console.print(f"  [{DIM}]{line}[/{DIM}]")

    raise structlog.DropEvent
