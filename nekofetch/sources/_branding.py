"""Shared brand-tag helpers for audio / subtitle track + container titles.

Single source of truth for the chrome-bracket title style that's used across
the new-download mux path (``_mux.py``), the manual re-mux path
(``_normalize.py``), the cross-source dual-audio path (``_dualaudio.py``),
and the on-screen subtitle cue in ``_subs.py``.

Visual style (kept consistent so MediaInfo / VLC / mpv all display the same
label across every release):

  * Track name:    ``"Language《 Anime Weebs 》"``
                   e.g. ``Japanese《 Anime Weebs 》``
                   e.g. ``English《 Anime Weebs 》``
                   e.g. ``《 Anime Weebs 》`` (no language tag on the track)

The double-angle brackets (U+300A / U+300B) wrap the channel NAME (not the @
handle) so the track label reads cleanly in a player's track menu.

The terminal-level ``subtitle on-screen cue`` is unaffected by this module;
``_subs.py`` keeps its own text template for the ASS stream.
"""

from __future__ import annotations

# Channel handle. Kept in sync with the form used by ``_subs.py`` (ASS on-screen
# cue), ``_normalize.py`` (transmux path — historically identical), and the
# branding block in ``core/constants.py`` / ``services/bot_factory.py``.
BRAND_HANDLE = "@AniXWeebs"

# Channel display name used in track-title stamps (``《 Anime Weebs 》``). Mirrors
# ``branding.channel_name`` in config.yaml; kept as a module constant so the pure
# track-title helpers stay config-free (like ``BRAND_HANDLE``).
BRAND_NAME = "Anime Weebs"


def brand_track_title(name: str | None, ordinal: int) -> str:
    """Build a stylish track-name label: ``"Language《 Anime Weebs 》"``.

    Args:
        name: the human display name (e.g. ``"Japanese"``, ``"Dual Audio"``).
            When ``None``/empty/whitespace-only, the label is just the bare
            channel stamp ``"《 Anime Weebs 》"`` — no language word and no
            sequence number (an untagged track carries the brand alone).
        ordinal: 1-based track position among its own stream type. Retained for
            signature compatibility with callers; no longer shown.

    Examples:
        ``Japanese《 Anime Weebs 》`` · ``English《 Anime Weebs 》`` · ``《 Anime Weebs 》``
    """
    base = name.strip() if name and name.strip() else ""
    return f"{base}《 {BRAND_NAME} 》"


def brand_container_title(title: str) -> str:
    """Build a stylish container title: ``"AnimeName〢@AniXWeebs"``.

    Idempotent: if the title is ALREADY branded with our separator + handle,
    it's returned unchanged. This guards against double-branding when a
    caller already pre-branded (e.g. ``_normalize.py`` adds the brand
    itself before calling ``mux_to_mkv``).
    """
    marker = f"〢{BRAND_HANDLE}"
    if marker in title:
        return title
    return f"{title}{marker}"
