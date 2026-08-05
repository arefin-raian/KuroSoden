"""Lelouch handler registration.

Reuses NekoFetch's existing module-level request helpers
(``_media_to_franchise_dict``, ``apply_franchise_totals``, ``enrich_with_tmdb``)
and registers Lelouch-specific handlers that add:
  • Duplicate detection (main channel → distribution → in-progress).
  • One-request-at-a-time limit for regular users.
  • Admin assignment after submission.
  • A staff batch flow that marshals titles into the *work* line (WorkItems),
    separate from user requests.
  • A management control plane (admin pool, availability, breaks, weights,
    working hours, reassignment) behind Command.
"""

from __future__ import annotations

from pyrogram import Client

from nekofetch.core.container import Container


def register_all(client: Client, container: Container) -> None:
    """Wire all Lelouch handlers — reuses NekoFetch's existing request flow."""

    # ── Auth middleware (same as NekoFetch's admin bot) ───────────────────
    from nekofetch.bots.middleware import install_auth_middleware

    install_auth_middleware(client, container)

    # ── Lelouch's batch handler (marshals titles into the *work* line) ───
    # Distinct from NekoFetch's admin batch, which submits *requests*. Ours
    # stages confirmed titles as WorkItems, so it must claim ``batch|new`` /
    # ``batch|cancel`` / ``/batch`` alone — registering both would double-fire.
    from kurosoden.bots.lelouch.handlers.batch import register as register_batch

    register_batch(client, container)

    # ── Management control plane (admin pool, availability, hours) ───────
    from kurosoden.bots.lelouch.handlers.management import register as register_management

    register_management(client, container)

    # ── Admin self-service profile (pr| surface) + timezone picker ──────
    # Non-owner admins edit their own country/tz/hours/slots here; the owner's
    # muster flow seeds the same fields. The shared timezone picker is mounted on
    # Lelouch too so the profile's Timezone button (lelouch|tz|home) resolves.
    from kurosoden.bots.lelouch.handlers.profile import register as register_profile
    from kurosoden.shared.timezone_ui import register_timezone_ui

    register_profile(client, container)
    register_timezone_ui(client, container, "lelouch")

    # ── Lelouch request handlers ─────────────────────────────────────────
    from kurosoden.bots.lelouch.handlers.requests import register as register_requests

    register_requests(client, container)

    # ── Lelouch redo handler (owner-only re-processing) ─────────────────
    from kurosoden.bots.lelouch.handlers.redo import register as register_redo

    register_redo(client, container)

    # ── Reused NekoFetch staff review board (owns the ``staff|…`` namespace) ─
    # The pending-requests screen's "Open Review Board" button emits
    # ``staff|requests|0``; app.py assumes those callbacks are already on this
    # client. They only are because we mount NekoFetch's review flow here. Its
    # text handlers key off the ``admin`` FSM namespace, so they stay inert
    # against Lelouch's ``lelouch``-namespace request flow (no double-fire).
    from nekofetch.bots.admin.handlers import review

    review.register(client, container)

    # ── Lelouch's settings panel (lelouch|set|…) — shared human-friendly engine ─
    # Registered before the app.py `lelouch|` dispatcher so every settings tap is
    # handled here. Lelouch owns only the request-intake side of the config: the
    # force-join gate and request queue sizing.
    #
    # `features` is deliberately NOT mounted — its one Lelouch-relevant field
    # (accept requests on/off) is the Pause/Resume toggle on Command, so exposing
    # it in Settings too was redundant and confusing. `security` and `queue` are
    # SHARED global sections that also hold download/distribution/owner fields
    # Lelouch has no business showing, so the `fields` allow-list mounts only the
    # request-relevant slice of each. Evicted fields keep living in their sections,
    # edited from the bots that own them.
    from kurosoden.shared.settings_ui import register_settings

    register_settings(
        client, container, "lelouch",
        ["security", "queue"],
        title="Lelouch — Request Settings",
        blurb=(
            "The request desk — whether users must join your channels first, and "
            "how many requests wait in line. Whether requests are accepted at all "
            "is the Pause/Resume switch on Command. The join-channel list has its "
            "own add/remove manager; numbers open a simple editor."
        ),
        fields={
            "security": [
                "force_subscribe",
                "force_subscribe_channels",
                "rate_limit_per_minute",
                "anti_spam_cooldown_seconds",
            ],
            "queue": ["max_visible", "position_recalc_seconds"],
        },
        owner_only=True,
    )
