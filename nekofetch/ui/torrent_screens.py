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


def format_torrent_mapping(mapping, *, encode_heights=None, fallbacks_cfg=None) -> str:
    """Compact mapping summary for a Telegram caption.

    Shows each franchise entry with its assigned file range and confidence. For a
    DDL release (files already extracted, so their tiers are known) each entry
    also gets a quality line — which tiers are PRESENT and which will be ENCODED
    to fill the gaps (``encode_heights`` + ``fallbacks_cfg`` come from
    ``config.processing.encode_heights`` / ``config.acquisition.resolution_fallbacks``,
    the same inputs the encoder uses). Torrent callers omit those and their files
    carry no resolutions, so no quality line is shown (quality is a download-time
    discovery there). ``mapping`` is a ``TorrentMapping`` instance.
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

        # Per-entry quality line (DDL only — torrent files carry no resolutions).
        present = me.present_resolutions
        if present:
            quality = "  ⌬ " + ", ".join(present) + " ✓"
            if encode_heights:
                to_enc = me.tiers_to_encode(encode_heights, fallbacks_cfg or {})
                if to_enc:
                    quality += "  · encode " + ", ".join(f"{h}p" for h in to_enc)
            lines.append(quality)

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


def format_full_mapping(mapping, *, episode_titles=None, torrent_name: str = "") -> str:
    """Structured, read-only 'Full Mapping' view for an inline Telegram message.

    The owner's spec: per season, one line per episode reading
    ``S01E01 → <episode title | filename>``, then a clearly separated section of
    files that "don't seem to belong here" (unmatched) and a "Missing episodes"
    section. ``episode_titles`` is the ``{season: [ {episode,title}, … ]}`` map
    from :func:`fetch_episode_titles_for_franchise` (optional — falls back to the
    filename when a title isn't known).

    HTML (not <pre>) so it wraps cleanly and stays readable; kept compact enough
    for a single message (callers paginate by entry if a franchise is huge).
    """
    def _title_lookup(season: int) -> dict:
        out: dict[int, str] = {}
        for row in (episode_titles or {}).get(season, []) or []:
            n, ttl = row.get("episode"), row.get("title")
            if n is not None and ttl:
                out[int(n)] = ttl
        return out

    lines: list[str] = []
    if torrent_name:
        lines.append(f"<b>{_esc(torrent_name)}</b>")
    pct = int(mapping.overall_confidence * 100)
    lines.append(f"<i>Overall confidence: {pct}%</i>")

    for me in mapping.entries:
        fe = me.franchise_entry
        header = _entry_header(fe)
        if not fe.included:
            lines.append(f"\n<b>{_esc(header)}</b> — <i>excluded</i>")
            continue
        exp = f" / {me.expected} expected" if me.expected is not None else ""
        lines.append(f"\n<b>{_esc(header)}</b>  ({me.actual} file(s){exp})")
        titles = _title_lookup(fe.season_number) if fe.kind.value == "season" else {}
        for f in me.files:
            if f.episode_number is not None and fe.kind.value == "season":
                slug = f"S{fe.season_number:02d}E{f.episode_number:02d}"
            elif f.episode_number is not None:
                slug = f"E{f.episode_number:02d}"
            else:
                slug = "•"
            label = titles.get(f.episode_number) or f.filename
            lines.append(f"  {slug} → {_esc(label)}")

    missing = getattr(mapping, "all_missing", None) or []
    if missing:
        lines.append("\n<b>⚠️ Missing episodes</b>")
        for m in missing[:20]:
            ttl = f" — {_esc(m.title)}" if m.title else ""
            lines.append(f"  S{m.season_number:02d}E{m.episode_number:02d}{ttl}")
        if len(missing) > 20:
            lines.append(f"  … and {len(missing) - 20} more")

    if mapping.unmatched:
        lines.append("\n<b>🗂 Files that don't seem to belong here</b>")
        for u in mapping.unmatched[:20]:
            name = getattr(u, "filename", str(u))
            lines.append(f"  • {_esc(name)}")
        if len(mapping.unmatched) > 20:
            lines.append(f"  … and {len(mapping.unmatched) - 20} more")

    return "\n".join(lines)


def mapping_telegraph_nodes(mapping, torrent_name: str = "") -> list:
    """Full franchise→torrent mapping as Telegraph DOM nodes.

    Unlike :func:`format_torrent_mapping` (a caption-limited summary) this renders
    the *complete* mapping — every franchise entry with its numbered file line and
    the specific missing episodes — so an admin can read and verify the whole
    structure before confirming. Returns a list of node dicts for
    ``TelegraphClient.createPage``.

    Each episode line is prefixed with the file's 1-based display number, which is
    the same number the ``<file#> S<season>`` re-mapping grammar expects — so the
    Telegraph page doubles as the edit key reference.
    """
    nodes: list = []
    if torrent_name:
        nodes.append({"tag": "h4", "children": [torrent_name]})
    pct = int(mapping.overall_confidence * 100)
    nodes.append({"tag": "p", "children": [
        {"tag": "b", "children": [f"Overall confidence: {pct}%"]},
    ]})

    display_no = 0
    for me in mapping.entries:
        fe = me.franchise_entry
        header = _entry_header(fe)
        if not fe.included:
            nodes.append({"tag": "h4", "children": [f"{header} — excluded"]})
            display_no += len(me.files)
            continue
        exp = f" / {me.expected} expected" if me.expected is not None else ""
        nodes.append({"tag": "h4", "children": [
            f"{header}  ({me.actual} files{exp}) {_conf_icon(me.confidence)}",
        ]})
        items = []
        for f in me.files:
            display_no += 1
            ep = f"E{f.episode_number:02d}" if f.episode_number is not None else "?"
            items.append({"tag": "li", "children": [f"#{display_no}  {ep}  {f.filename}"]})
        if items:
            nodes.append({"tag": "ul", "children": items})

    missing = getattr(mapping, "all_missing", None) or []
    if missing:
        nodes.append({"tag": "h4", "children": ["Missing episodes"]})
        mi = [{"tag": "li", "children": [
            f"S{m.season_number:02d}E{m.episode_number:02d}"
            + (f" — {m.title}" if m.title else "")]} for m in missing]
        nodes.append({"tag": "ul", "children": mi})

    if mapping.unmatched:
        nodes.append({"tag": "h4", "children": [
            f"{len(mapping.unmatched)} unmatched file(s)"]})
        um = [{"tag": "li", "children": [getattr(u, "filename", str(u))]}
              for u in mapping.unmatched]
        nodes.append({"tag": "ul", "children": um})

    return nodes


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
