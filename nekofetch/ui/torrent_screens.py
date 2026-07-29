"""Torrent structure display helpers.

Compact summaries of torrent file trees for Telegram captions — wrapping-safe
(no box-drawing characters), fits within the 1024-char caption limit.
"""
from __future__ import annotations

from nekofetch.sources._torrent import parse_release_meta, VIDEO_EXT


def format_torrent_summary(torrent_name: str, ordered_files: list[dict]) -> str:
    """One-liner-per-season summary of a torrent's video structure.

    Output example::

        96 files · 4 seasons + extras

        S01  24 ep  (E01‥E25)
        S02  12 ep  (E01‥E12)
        S03  22 ep  (E01‥E22)
        S04  30 ep  (E01‥E28 +2 SP)
        OAD   8 ep
    """
    if not ordered_files:
        return f"<b>{_esc(torrent_name)}</b>\nNo video files found."

    groups: dict[str, list[dict]] = {}
    for f in ordered_files:
        kind = f.get("kind", "episode")
        season = f.get("season", 0)
        if kind == "episode":
            key = f"S{season:02d}"
        elif kind == "movie":
            key = "MOV"
        elif kind in ("ova", "special") and season > 0:
            key = f"S{season:02d}"
        elif kind in ("ova", "special"):
            key = "OAD"
        elif kind == "extra" and season > 0:
            key = f"S{season:02d}"
        else:
            key = "EXT"
        groups.setdefault(key, []).append(f)

    season_count = sum(1 for k in groups if k.startswith("S"))
    extras_count = sum(len(v) for k, v in groups.items() if not k.startswith("S"))
    total = sum(len(v) for v in groups.values())

    parts = [f"{total} files"]
    if season_count:
        parts.append(f"{season_count} season{'s' if season_count != 1 else ''}")
    if extras_count:
        parts.append("extras")
    header = " · ".join(parts)

    lines = [header, ""]
    for key in sorted(groups):
        files = groups[key]
        count = len(files)
        eps = [f.get("episode") for f in files if f.get("episode") is not None]
        main = [f for f in files if f.get("kind") == "episode"]
        non_main = len(files) - len(main)

        if eps and main:
            lo, hi = min(eps), max(eps)
            rng = f"E{lo:02d}" if lo == hi else f"E{lo:02d}‥E{hi:02d}"
            suffix = f" +{non_main} SP" if non_main else ""
            lines.append(f"{key}  {count:>2} ep  ({rng}{suffix})")
        elif eps:
            lo, hi = min(eps), max(eps)
            rng = f"E{lo:02d}" if lo == hi else f"E{lo:02d}‥E{hi:02d}"
            lines.append(f"{key}  {count:>2} ep  ({rng})")
        else:
            lines.append(f"{key}  {count:>2} ep")

    return "\n".join(lines)


def format_torrent_mapping(mapping) -> str:
    """Compact mapping summary for a Telegram caption.

    Shows each franchise entry with its assigned torrent file range and
    confidence indicator.  ``mapping`` is a ``TorrentMapping`` instance.
    """
    pct = int(mapping.overall_confidence * 100)
    lines = [f"Franchise Mapping ({pct}%)", ""]

    for me in mapping.entries:
        fe = me.franchise_entry
        if fe.kind.value == "season":
            tag = f"S{fe.season_number:02d}"
            if fe.season_part:
                tag += f"P{fe.season_part}"
        elif fe.kind.value == "movie":
            tag = "MOV"
        else:
            tag = "SP"

        n = me.actual
        exp = me.expected

        if not fe.included:
            lines.append(f"{tag}  {n:>2} files  (excluded)")
            continue

        conf_icon = _conf_icon(me.confidence)
        ep_range = ""
        if me.files:
            eps = [f.episode_number for f in me.files if f.episode_number is not None]
            if eps:
                lo, hi = min(eps), max(eps)
                ep_range = f"E{lo:02d}" if lo == hi else f"E{lo:02d}-E{hi:02d}"

        title = fe.title[:35] if fe.title else ""
        count_part = f"{n:>2}"
        if exp is not None:
            count_part += f"/{exp}"

        parts = [tag, f"{count_part} ep"]
        if ep_range:
            parts.append(f"({ep_range})")
        if title:
            parts.append(f"  {title}")
        parts.append(conf_icon)
        lines.append("  ".join(parts))

    # Show missing episodes warning.
    if mapping.has_gaps:
        lines.append("")
        all_missing = mapping.all_missing
        if len(all_missing) <= 5:
            for m in all_missing:
                title_part = f" — {m.title}" if m.title else ""
                lines.append(f"  !! S{m.season_number:02d}E{m.episode_number:02d} MISSING{title_part}")
        else:
            lines.append(f"  !! {len(all_missing)} episodes MISSING")
            for m in all_missing[:3]:
                title_part = f" — {m.title}" if m.title else ""
                lines.append(f"     S{m.season_number:02d}E{m.episode_number:02d}{title_part}")
            lines.append(f"     ... and {len(all_missing) - 3} more")

    if mapping.unmatched:
        lines.append("")
        lines.append(f"  {len(mapping.unmatched)} unmatched file(s)")

    return "\n".join(lines)


def format_mapping_detail(mapping, page: int = 0, per_page: int = 1) -> str:
    """Detailed per-entry view showing individual file assignments.

    Returns a ``<pre>`` block suitable for Telegram's monospace rendering.
    Paginated — one franchise entry per page to stay within 4096 chars.
    """
    entries = mapping.entries
    if not entries:
        return "<pre>No mapping entries.</pre>"

    start = page * per_page
    subset = entries[start:start + per_page]
    blocks = []

    for me in subset:
        fe = me.franchise_entry
        header = _entry_header(fe)
        lines = [header, "-" * min(len(header), 40)]

        if not me.files:
            lines.append("  (no files assigned)")
        else:
            for fa in me.files:
                ep = f"E{fa.episode_number:03d}" if fa.episode_number else "  ?  "
                name = fa.filename[:45]
                lines.append(f"  {ep}  {name}")

        if me.expected is not None and me.actual != me.expected:
            lines.append(f"  ** expected {me.expected}, got {me.actual} **")

        if me.missing:
            lines.append("")
            lines.append("  MISSING:")
            for m in me.missing:
                title_part = f"  {m.title}" if m.title else ""
                lines.append(f"    E{m.episode_number:03d}{title_part}")

        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    total_pages = (len(entries) + per_page - 1) // per_page
    footer = f"\n— Page {page + 1}/{total_pages} —"
    return f"<pre>{_esc(body + footer)}</pre>"


def _conf_icon(confidence: float) -> str:
    if confidence >= 0.95:
        return "[OK]"
    if confidence >= 0.7:
        return "[~]"
    return "[!]"


def _entry_header(fe) -> str:
    if fe.kind.value == "season":
        h = f"Season {fe.season_number}"
        if fe.season_part:
            h += f" Part {fe.season_part}"
        if fe.title:
            h += f" — {fe.title[:40]}"
        return h
    return f"{fe.kind.value.title()}: {fe.title[:50]}"


def _esc(text: str) -> str:
    import html as _html
    return _html.escape(text or "", quote=False)
