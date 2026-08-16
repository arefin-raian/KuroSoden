"""Franchise Flow — the franchise-structure-first pipeline coordinator.

This service orchestrates the new post-source-selection workflow:

  1. **Franchise mapping** — build a structured map from AniList titles showing
     every TV season with CORRECT season+part labels (e.g. "Season 3 Part 2"
     instead of sequential "Season 4"), plus extras (OVA/MOVIE/SPECIAL).

  2. **Post-processing confirmation** — after processing, show mapping in a
     copyable code block so admin can confirm or edit via reply.

  3. **Reply-based editing** — admin replies to the code-block message; the bot
     parses the correction and reapplies the mapping.

The shift is from "download-first" to "franchise-structure-first": before any
download, the admin sees and confirms the full structure of what will be acquired.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import ContentKind

log = get_logger(__name__)

# Relation types that are NOT part of the canonical continuity and must be
# dropped from the aggregated fallback mapping — the inverse of the walk's
# _CONTENT_WALK_RELS allowlist. SPIN_OFF (Chibi Theatre, Junior High),
# ALTERNATIVE (different adaptations), and SUMMARY (recap/compilation movies)
# are excluded so the batch/distribution path matches the request pipeline.
_EXCLUDED_RELATIONS = {"SPIN_OFF", "ALTERNATIVE", "SUMMARY", "CHARACTER", "OTHER"}


def _esc(text: str) -> str:
    """HTML-escape text for safe rendering in Telegram messages."""
    return html.escape(text or "", quote=False)


def _franchise_entries_from_cache(walk) -> dict | None:
    """Reconstruct ``{anilist_id: FranchiseEntry}`` from a cached franchise-walk
    blob (the ``franchise`` key of a prefetched ``anilist.json``), so callers can
    rebuild the mapping without a live AniList walk.

    Mirrors ``bot_content._franchise_from_cache``: unknown keys are dropped and a
    malformed entry is skipped rather than crashing the whole read. Returns None
    when nothing usable could be reconstructed."""
    from dataclasses import fields as _dc_fields

    from nekofetch.sources.telegram.anilist import FranchiseEntry

    allowed = {f.name for f in _dc_fields(FranchiseEntry)}
    out: dict = {}
    values = walk.values() if isinstance(walk, dict) else (walk or [])
    for raw in values:
        if not isinstance(raw, dict):
            continue
        aid = raw.get("anilist_id")
        if aid is None:
            continue
        try:
            out[int(aid)] = FranchiseEntry(
                **{k: v for k, v in raw.items() if k in allowed}
            )
        except Exception:  # noqa: BLE001 — skip a bad entry, keep the rest
            continue
    return out or None


@dataclass
class MappingEntry:
    """A single entry in the franchise mapping."""
    anilist_id: int | None = None
    kind: ContentKind = ContentKind.SEASON  # SEASON | MOVIE | SPECIAL
    season_number: int = 1        # the REAL displayed season number (1, 2, 3…)
    season_part: int | None = None   # None, 1, 2 (for S3P1, S3P2)
    title: str = ""               # the AniList English title
    episodes: int | None = None
    included: bool = True
    auto_detected_part: bool = False
    source_note: str | None = None


@dataclass
class FranchiseMapping:
    """Full franchise mapping for a request."""
    anime_doc_id: str
    root_title: str
    entries: list[MappingEntry] = field(default_factory=list)

    @property
    def included_entries(self) -> list[MappingEntry]:
        return [e for e in self.entries if e.included]

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def included_count(self) -> int:
        return len(self.included_entries)

    def get_entry(self, season: int, part: int | None = None) -> MappingEntry | None:
        for e in self.entries:
            if e.season_number == season and e.season_part == part:
                return e
        return None


# ── Smart Season/Part Parser ───────────────────────────────────────────────────
#
# AniList gives entries in sequential order (1, 2, 3, 4, 5…) but the REAL
# structure may regroup them. For example:
#   Entry 3: "Attack on Titan Season 3"       → Season 3 Part 1
#   Entry 4: "Attack on Titan Season 3 Part 2" → Season 3 Part 2
#
# The parser below reads each entry's English title to extract the TRUE season
# number and part, then groups consecutive entries that share the same season
# identity (same explicit season number OR consecutive "Final Season" entries).

# Pattern to extract "Season X" from a title
_SEASON_IN_TITLE = re.compile(r"\bseason\s+(\d+)\b", re.IGNORECASE)
# Pattern to extract "Part Y" or "Pt Y" or "Cour Y" from a title
_PART_IN_TITLE = re.compile(r"\b(?:part|pt|cour)\s*[\.:]?\s*(\d+)\b", re.IGNORECASE)
# Pattern to detect "Final Season" (implies a new season, may have parts)
_FINAL_SEASON = re.compile(r"\b(?:the\s+)?final\s+season\b", re.IGNORECASE)


def strip_season_tokens(title: str) -> str:
    """Remove trailing season/part markers while preserving display casing.

    TMDB stores franchise artwork under the base title, not under labels such as
    ``Vanitas Part 2``. This deliberately strips only trailing markers so inner
    words and the original capitalization remain intact for provider search.
    """
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    patterns = (
        r"\s*[-:()]?\s*(?:the\s+)?final\s+season(?:\s+(?:part|pt|cour)\s*\d+)?\s*$",
        r"\s*[-:()]?\s*season\s+(?:\d+|[IVXLCDM]+)(?:\s+(?:part|pt|cour)\s*\d+)?\s*$",
        r"\s*[-:()]?\s*(?:part|pt|cour)\s*\d+\s*$",
        r"\s*[-:()]?\s*(?:season\s+)?[IVXLCDM]+\s+season\s*$",
    )
    previous = None
    while value and value != previous:
        previous = value
        for pattern in patterns:
            value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip(" -:()")
    return value or str(title or "").strip()


def _base_title_key(title: str) -> str:
    """Normalize a title for continuation matching: strip season/part/final
    tokens + punctuation so "Vanitas" and "Vanitas Part 2" share a base key."""
    t = strip_season_tokens(title)
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _extract_season_info(title: str) -> dict:
    """Parse an AniList English title to extract season and part info.

    Returns a dict with keys:
      - ``season`` (int | None): detected season number from title
      - ``part`` (int | None): detected part number from title
      - ``has_final`` (bool): whether title contains "Final Season"

    Examples:
      "Attack on Titan Season 2"          → {"season": 2, "part": None, "has_final": False}
      "Attack on Titan Season 3 Part 2"   → {"season": 3, "part": 2, "has_final": False}
      "Attack on Titan The Final Season"  → {"season": None, "part": None, "has_final": True}
      "Attack on Titan"                   → {"season": None, "part": None, "has_final": False}
    """
    result = {"season": None, "part": None, "has_final": False}
    sm = _SEASON_IN_TITLE.search(title)
    if sm:
        result["season"] = int(sm.group(1))
    pm = _PART_IN_TITLE.search(title)
    if pm:
        result["part"] = int(pm.group(1))
    if _FINAL_SEASON.search(title):
        result["has_final"] = True
    return result


def _kind_from_format(fmt: str | None) -> ContentKind:
    if fmt in ("TV", "TV_SHORT"):
        return ContentKind.SEASON
    if fmt == "MOVIE":
        return ContentKind.MOVIE
    return ContentKind.SPECIAL


# A multi-episode ONA is a real series season (Netflix-style online show), NOT
# an extra: AniList tags it ONA and a season-less title gets one sequential
# group → "Season 1". One-shot ONAs (≤1 episode) stay extras. This keeps the
# mapping, the wizard's watch-order confirm, and the posted watch guide in
# lock-step (the old code classified every ONA as an extra, so the wizard said
# "Season 1" but the distribution channel posted "ONA 1").
_SEASON_FORMATS = {"TV", "TV_SHORT"}

# AniList relation types that mean "this node is NOT part of the main season
# line" — a spin-off / side-story / alternate retelling / recap. A multi-episode
# ONA reached through one of these stays an *extra* even though its episode count
# would otherwise promote it to a season. The main continuity (ROOT, SEQUEL,
# PREQUEL, PARENT — or an unknown/empty relation) is left season-eligible so
# Takopi's Original Sin (a ROOT ONA) still renders as "Season 1". Kept in sync
# with the walk's own exclusions in anilist._CONTENT_WALK_RELS.
_EXTRA_RELATIONS = {
    "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "SUMMARY", "CHARACTER", "OTHER",
}


def is_season_format(
    fmt: str | None,
    episodes: int | None = None,
    relation: str | None = None,
) -> bool:
    """True when an AniList format renders as a season entry (not an extra).

    TV and TV_SHORT are seasons; TV_SPECIAL remains an extra unless a later
    source-specific rule promotes it. A multi-episode ONA is a full series (e.g.
    Takopi's Original Sin — 12 eps ONA shown as Season 1), while a one-shot ONA
    stays an extra.

    ``relation`` (the AniList edge that attached this node to the franchise —
    ROOT/SEQUEL/PREQUEL/SIDE_STORY/…) differentiates a real season from an
    actual extra: a multi-episode ONA promotes to a season only when it sits in
    the main continuity, so a multi-episode *spin-off/side-story* ONA is NOT
    falsely labelled a season. An unknown/empty relation stays season-eligible
    (preserves behaviour for the single-title root case).
    """
    if fmt in _SEASON_FORMATS:
        return True
    if fmt == "ONA" and (episodes or 0) > 1:
        return (relation or "").upper() not in _EXTRA_RELATIONS
    return False


def entry_is_season(entry) -> bool:
    """Season check on a walk entry (FranchiseEntry) — shared with the card/
    guide builders so the wizard's watch order matches the posted channel."""
    return is_season_format(
        getattr(entry, "format", None),
        getattr(entry, "episodes", None),
        getattr(entry, "relation", None),
    )


# ── Service ──────────────────────────────────────────────────────────────────


class FranchiseFlowService:
    """Coordinates the franchise-structure-first workflow."""

    def __init__(self, container: Container) -> None:
        self._c = container

    def build_mapping(
        self,
        franchise_data: dict,
        anime_doc_id: str,
        franchise_entries: dict[int, any] | None = None,
    ) -> FranchiseMapping:
        """Build a FranchiseMapping with smart season/part detection.

        When ``franchise_entries`` (the result of ``AnilistClient.walk_franchise_full``)
        is provided, each TV entry's English title is parsed to extract the REAL
        season number and part. Without it, falls back to sequential numbering.

        Extras (OVAs, Movies, ONAs, Specials) are always parsed from the
        ``relations`` list in ``franchise_data``.
        """
        root_title = franchise_data.get("title") or franchise_data.get("english") or anime_doc_id

        if franchise_entries:
            return self._build_from_franchise_entries(
                franchise_entries, franchise_data, anime_doc_id, root_title,
            )
        return self._build_from_aggregated(franchise_data, anime_doc_id, root_title)

    async def build_mapping_resolved(
        self, franchise_data: dict, anime_doc_id: str,
    ) -> FranchiseMapping:
        """Resolve the franchise walk (cache → live, now Kitsu-backed when AniList
        is down) and build the mapping from it — so per-season episode counts are
        correct (S1 13 / S2 12) instead of the aggregated-total fallback (25/25).

        Use this from any async caller that has only ``franchise_data`` in hand;
        it collapses ``resolve_franchise_entries`` + :meth:`build_mapping` and
        degrades gracefully to the (now safe) aggregated fallback when no walk
        entries are available anywhere.
        """
        entries = await self.resolve_franchise_entries(franchise_data, anime_doc_id)
        return self.build_mapping(
            franchise_data, anime_doc_id, franchise_entries=entries,
        )

    async def resolve_franchise_entries(
        self, franchise_data: dict, anime_doc_id: str,
    ) -> dict | None:
        """Resolve the AniList franchise-walk entries (``{anilist_id: FranchiseEntry}``)
        cache-first, so a caller can feed them to :meth:`build_mapping` and get the
        SAME spin-off/recap-excluding, part-aware mapping the request pipeline uses.

        Order: prefetched ``anilist.json`` → live ``walk_franchise_full`` → None.
        Returns None when neither is available (caller then gets the aggregated
        fallback, which is now relation-type-guarded). This is the single source of
        truth the batch/distribution path was missing — it previously called
        ``build_mapping`` with no entries and silently used the aggregated fallback,
        pulling in SPIN_OFF/SUMMARY relations the walk excludes.
        """
        entries: dict | None = None
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(
                self._c, anime_doc_id, "anilist", anime_doc_id=anime_doc_id,
            )
            walk = (blob or {}).get("franchise")
            if walk:
                entries = _franchise_entries_from_cache(walk)
        except Exception as exc:  # noqa: BLE001
            log.debug("franchise.entries.cache_miss", error=str(exc))
        if not entries:
            try:
                aid = franchise_data.get("anilist_id")
                if aid:
                    entries = await self._c.anilist.walk_franchise_full(int(aid))
            except Exception as exc:  # noqa: BLE001
                log.debug("franchise.entries.walk_failed", error=str(exc))
        return entries or None

    async def persisted_entries(self, franchise_data: dict, anime_doc_id: str) -> list[dict]:
        """Return the canonical, JSON-safe entries to persist on a Request.

        Request rows are the durable handoff contract between Lelouch and every
        later stage. Resolve the same cache-first/live franchise walk used by the
        download and distribution paths, then serialize only included entries in
        the shape consumed by ``_map_episode_to_part`` and ``dict_to_mapping``.
        """
        walked = await self.resolve_franchise_entries(franchise_data, anime_doc_id)
        mapping = self.build_mapping(
            franchise_data, anime_doc_id, franchise_entries=walked,
        )
        return [
            {
                "anilist_id": entry.anilist_id,
                "kind": entry.kind.value,
                "season_number": entry.season_number,
                "season_part": entry.season_part,
                "episodes": entry.episodes,
                "title": entry.title,
                "included": entry.included,
                "auto_detected_part": entry.auto_detected_part,
            }
            for entry in mapping.included_entries
        ]

    def _build_from_franchise_entries(
        self,
        franchise_entries: dict[int, any],
        franchise_data: dict,
        anime_doc_id: str,
        root_title: str,
    ) -> FranchiseMapping:
        """Build mapping from full franchise walk data with smart title parsing.

        Strategy:
          1. Extract TV entries (sorted by air date).
          2. Parse each English title for explicit season/part info.
          3. Group consecutive entries that share a "season identity":
             - Same explicit ``Season X`` number → group them as parts
             - Consecutive entries with ``Final Season`` → group as parts
             - Entries with NO season info → each is its own season
          4. Assign display season numbers (1, 2, 3…) per group, and part
             numbers within multi-entry groups.
          5. Add extras (Movies, OVAs, ONAs, Specials) ungrouped.

        Example result for Attack on Titan:
          Entry 1: "Attack on Titan"                    → Season 1
          Entry 2: "Attack on Titan Season 2"           → Season 2
          Entry 3: "Attack on Titan Season 3"           → Season 3 Part 1
          Entry 4: "Attack on Titan Season 3 Part 2"    → Season 3 Part 2
          Entry 5: "Attack on Titan The Final Season"   → Season 4 Part 1
          Entry 6: "Attack on Titan The Final Season Part 2" → Season 4 Part 2
        """
        from nekofetch.sources.telegram.anilist import FranchiseEntry

        # ── 1. Gather TV entries plus extras ──
        # Multi-episode ONAs count as seasons (a full series, e.g. Takopi's
        # Original Sin) so they render as "Season N" everywhere; one-shot ONAs
        # stay extras with the other OVA/MOVIE/SPECIAL entries.
        tv_entries: list[any] = []
        extra_entries: list[any] = []
        for entry in franchise_entries.values():
            if entry_is_season(entry):
                tv_entries.append(entry)
            elif entry.format in ("OVA", "ONA", "MOVIE", "SPECIAL"):
                extra_entries.append(entry)

        # Sort both by air date
        def _sort_key(e):
            sd = getattr(e, "start_date", None) or {}
            return (sd.get("year", 9999), sd.get("month", 99), sd.get("day", 99))

        tv_entries.sort(key=_sort_key)
        extra_entries.sort(key=_sort_key)

        # ── 2. Parse TV titles to extract real season/part info ──
        mappings: list[dict] = []
        for entry in tv_entries:
            parsed = _extract_season_info(entry.english_title or "")
            mappings.append({
                "entry": entry,
                "anilist_id": entry.anilist_id,
                "title": entry.english_title or "",
                "parsed_season": parsed["season"],
                "parsed_part": parsed["part"],
                "has_final": parsed["has_final"],
                "episodes": entry.episodes or 0,
            })

        # ── 3. Group entries by season identity ──
        #
        # Walk the sorted entries and build groups of consecutive entries that
        # share a season identity:
        #   ("explicit", N)  → entries with ``Season N`` in their title
        #   ("final", 0)     → consecutive entries with ``Final Season``
        #   ("sequential",)  → entries with no season info at all
        #
        # NOTE: an empty ``mappings`` (no TV entries — e.g. a movie-only or
        # OVA-only title) must NOT bail out here: the grouping loop no-ops on an
        # empty list, section 5 builds zero season entries, and section 6 still
        # contributes the extras. Returning early would drop those extras and
        # yield an empty mapping ("No entries selected for mapping").

        groups: list[tuple[tuple, list[int]]] = []  # ((id_type, id_val), [indices])

        i = 0
        while i < len(mappings):
            m = mappings[i]
            if m["parsed_season"] is not None:
                # Group consecutive entries that share this explicit season number
                identity = ("explicit", m["parsed_season"])
                group = [i]
                j = i + 1
                while j < len(mappings) and mappings[j]["parsed_season"] == m["parsed_season"]:
                    group.append(j)
                    j += 1
                groups.append((identity, group))
                i = j
            elif m["has_final"]:
                # Group consecutive entries with "Final Season" in the title
                identity = ("final", 0)
                group = [i]
                j = i + 1
                while j < len(mappings) and mappings[j]["has_final"] and mappings[j]["parsed_season"] is None:
                    group.append(j)
                    j += 1
                groups.append((identity, group))
                i = j
            elif (
                m["parsed_part"] is not None
                and not m["has_final"]
                and groups
                and _base_title_key(m["title"])
                == _base_title_key(mappings[groups[-1][1][-1]]["title"])
            ):
                # A season-less "… Part N" whose base title matches the entry that
                # opened the previous group is a CONTINUATION of that season (e.g.
                # "Vanitas" → "Vanitas Part 2"). Attach it as the next part of the
                # SAME season instead of spawning a bogus new sequential season
                # (which mislabeled Vanitas Part 2 as "S02P2").
                groups[-1][1].append(i)
                i += 1
            else:
                # No season info at all — each entry is its own season
                groups.append((("sequential", 0), [i]))
                i += 1

        # ── 4. Assign display season numbers and parts ──
        display_season: list[int] = []
        display_part: list[int | None] = []
        current_season = 0

        for (_id_type, _id_val), indices in groups:
            current_season += 1
            if len(indices) == 1:
                # Single-entry group — keep any explicit part from the title
                idx = indices[0]
                display_season.append(current_season)
                display_part.append(mappings[idx]["parsed_part"])
            else:
                # Multi-entry group — assign parts sequentially (1, 2, 3…)
                for part_num, idx in enumerate(indices, start=1):
                    display_season.append(current_season)
                    display_part.append(part_num)

        # ── 5. Build entries ──
        entries: list[MappingEntry] = []
        for i, m in enumerate(mappings):
            entries.append(MappingEntry(
                anilist_id=m["anilist_id"],
                kind=ContentKind.SEASON,
                season_number=display_season[i],
                season_part=display_part[i],
                title=m["title"],
                episodes=m["episodes"],
                included=True,
                auto_detected_part=display_part[i] is not None,
            ))

        # ── 6. Add extras ──
        for entry in extra_entries:
            fmt = entry.format or ""
            entries.append(MappingEntry(
                anilist_id=entry.anilist_id,
                kind=ContentKind.MOVIE if fmt == "MOVIE" else ContentKind.SPECIAL,
                season_number=0,  # extras don't have a display season
                season_part=None,
                title=entry.english_title or "",
                episodes=entry.episodes or 0,
                included=True,
            ))

        return FranchiseMapping(
            anime_doc_id=anime_doc_id,
            root_title=root_title,
            entries=entries,
        )

    def _build_from_aggregated(
        self,
        franchise_data: dict,
        anime_doc_id: str,
        root_title: str,
    ) -> FranchiseMapping:
        """Fallback: build mapping from aggregated franchise_data dict only.

        Used when full walk entries aren't available. Just counts seasons
        sequentially (1, 2, 3…) since we don't have per-entry titles to parse.
        """
        entries: list[MappingEntry] = []

        seasons = franchise_data.get("franchise_seasons", 1) or 1
        # Per-season episode counts are NOT knowable from the aggregated totals:
        # ``franchise_episodes`` is the SUM across seasons. Stamping that sum on
        # every season is the "2 seasons, 25 episodes → 25/25" bug (should be
        # 13/12). Only when there is a single season does the total equal that
        # season's count; for multi-season, leave episodes unknown (None) — a
        # blank is honest, a false 25-per-season is not. The correct per-season
        # split comes from the franchise walk (`_build_from_franchise_entries`),
        # which callers now reach via `build_mapping_resolved`.
        single_season = seasons == 1
        for n in range(1, seasons + 1):
            entries.append(MappingEntry(
                kind=ContentKind.SEASON,
                season_number=n,
                season_part=None,
                title=f"Season {n:02d}",
                episodes=franchise_data.get("franchise_episodes") if single_season else None,
                included=True,
            ))

        relations = franchise_data.get("relations", [])
        for rel in relations:
            # Skip non-canonical relations (spin-offs, alternate retellings, and
            # recap/summary movies) — the same classes the AniList franchise walk
            # excludes via _CONTENT_WALK_RELS. Without this guard the aggregated
            # fallback pulled AoT's "No Regrets" OVA and recap movies into the
            # batch mapping, which the request pipeline correctly leaves out.
            if (rel.get("relation") or "").upper() in _EXCLUDED_RELATIONS:
                continue
            fmt = (rel.get("format") or "").upper()
            if fmt not in ("OVA", "MOVIE", "ONA", "SPECIAL"):
                continue
            entry_title = rel.get("title") or rel.get("english") or ""
            part, _ = self._detect_season_part(entry_title)
            entries.append(MappingEntry(
                anilist_id=rel.get("anilist_id"),
                kind=ContentKind.MOVIE if fmt == "MOVIE" else ContentKind.SPECIAL,
                season_number=0,  # extras carry no display season — matches _build_from_franchise_entries
                season_part=part,
                title=entry_title,
                episodes=rel.get("episodes"),
                included=True,
            ))

        return FranchiseMapping(
            anime_doc_id=anime_doc_id,
            root_title=root_title,
            entries=entries,
        )

    # ── legacy season part detection (kept for fallback) ──

    _PART_PATTERNS = [
        re.compile(r"\b(?:part|pt)\s*[\.:]?\s*(\d+)\b", re.IGNORECASE),
        re.compile(r"\bcour\s*(\d+)\b", re.IGNORECASE),
        re.compile(r"\((\d+)\)", re.IGNORECASE),
    ]

    @staticmethod
    def _detect_season_part(title: str) -> tuple[int | None, bool]:
        for pattern in FranchiseFlowService._PART_PATTERNS:
            m = pattern.search(title)
            if m:
                return int(m.group(1)), True
        return None, False

    def apply_inclusions(self, mapping: FranchiseMapping, include_map: dict[str, bool]) -> FranchiseMapping:
        for entry in mapping.entries:
            key = self._entry_key(entry)
            if key in include_map:
                entry.included = include_map[key]
        return mapping

    @staticmethod
    def _entry_key(entry: MappingEntry) -> str:
        if entry.kind == ContentKind.SEASON:
            return f"season_{entry.season_number}"
        return f"{entry.kind.value}_{entry.title or entry.season_number}"

    @staticmethod
    def _entry_key_from_dict(entry_dict: dict) -> str:
        kind = entry_dict.get("kind", "season")
        if kind == "season":
            return f"season_{entry_dict.get('season_number', 1)}"
        return f"{kind}_{entry_dict.get('title', '') or entry_dict.get('season_number', 1)}"

    @staticmethod
    def dict_to_mapping(mapping_dict: dict) -> FranchiseMapping:
        entries = []
        for e in mapping_dict.get("entries", []):
            kind_str = e.get("kind", "season")
            kind = ContentKind(kind_str) if kind_str in ("season", "movie", "special") else ContentKind.SPECIAL
            entries.append(MappingEntry(
                anilist_id=e.get("anilist_id"),
                kind=kind,
                season_number=e.get("season_number", 1),
                season_part=e.get("season_part"),
                title=e.get("title", ""),
                episodes=e.get("episodes"),
                included=e.get("included", True),
                auto_detected_part=e.get("auto_detected_part", False),
            ))
        return FranchiseMapping(
            anime_doc_id=mapping_dict.get("anime_doc_id", ""),
            root_title=mapping_dict.get("root_title", ""),
            entries=entries,
        )

    @staticmethod
    def entry_label(entry: MappingEntry) -> str:
        if entry.kind == ContentKind.SEASON:
            base = f"Season {entry.season_number:02d}"
            if entry.season_part:
                base += f" Part {entry.season_part}"
            # Append the actual title when it is meaningful (not a fallback
            # "Season NN" placeholder and not trivially derivable from the
            # season number alone).
            title = (entry.title or "").strip()
            if title and title != base and not title.startswith("Season "):
                base += f" — {title[:50]}"
            return base
        fmt_label = entry.kind.value.title()
        title = entry.title or ""
        return f"{fmt_label}: {title}" if title else f"{fmt_label} {entry.season_number}"

    @staticmethod
    def parse_mapping_correction(text: str, current_mapping: FranchiseMapping) -> FranchiseMapping | None:
        """Parse an admin's corrected mapping text and apply it.

        The admin copies the code block, edits it, and pastes back.
        Each line is parsed to extract season/part/episode info.

        Lines like:
          "Attack on Titan Season 3 → Season 3 Part 1 (12 eps)"
          "Attack on Titan Season 3 Part 2 → Season 3 Part 2 (12 eps)"
          "Exclude: Attack on Titan Season 2"
          "Include: OVA"

        Returns a new FranchiseMapping with the corrected entries, or None if
        parsing fails.
        """
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        if not lines:
            return None

        # Build a new list of entries from the parsed lines
        corrected_entries: list[MappingEntry] = []

        for line in lines:
            # Skip lines that are clearly commands, not entries
            command_match = re.match(r"(?i)(exclude|include|remove|add)\s*:\s*(.+)", line)
            if command_match:
                action, target = command_match.group(1).lower(), command_match.group(2).strip()
                # Try to match against existing entries
                for entry in current_mapping.entries:
                    label = FranchiseFlowService.entry_label(entry).lower()
                    if target.lower() in label or target.lower() in (entry.title or "").lower():
                        entry.included = (action == "include" or action == "add")
                continue

            # Extract the mapping after "→"
            arrow_match = re.match(r".+?→\s*(.+)", line)
            if arrow_match:
                rhs = arrow_match.group(1).strip()
                # Parse the RHS: "Season 3 Part 1 (12 eps)"
                season_match = re.match(r"(?i)season\s+(\d+)(?:\s+part\s+(\d+))?", rhs)
                if season_match:
                    season_num = int(season_match.group(1))
                    part_num = int(season_match.group(2)) if season_match.group(2) else None
                    # Extract episode count
                    ep_match = re.search(r"\((\d+)\s*eps?\)", rhs)
                    ep_count = int(ep_match.group(1)) if ep_match else None

                    corrected_entries.append(MappingEntry(
                        kind=ContentKind.SEASON,
                        season_number=season_num,
                        season_part=part_num,
                        episodes=ep_count,
                        included=True,
                        auto_detected_part=part_num is not None,
                    ))
                else:
                    # Could be a movie or extra
                    movie_match = re.match(r"(?i)(movie|ova|ona|special)\s*(.*)", rhs)
                    if movie_match:
                        kind_str = movie_match.group(1).upper()
                        title_rest = movie_match.group(2).strip().lstrip(":").strip()
                        ep_match = re.search(r"\((\d+)\s*eps?\)", rhs)
                        ep_count = int(ep_match.group(1)) if ep_match else None
                        k = ContentKind.MOVIE if kind_str == "MOVIE" else ContentKind.SPECIAL
                        corrected_entries.append(MappingEntry(
                            kind=k,
                            season_number=0,
                            title=title_rest,
                            episodes=ep_count,
                            included=True,
                        ))
            else:
                # Line without arrow — might be a movie/extra title
                movie_match = re.match(r"(?i)(movie|ova|ona|special)\s*(.*)", line)
                if movie_match:
                    kind_str = movie_match.group(1).upper()
                    title_rest = movie_match.group(2).strip().lstrip(":").strip()
                    ep_match = re.search(r"\((\d+)\s*eps?\)", line)
                    ep_count = int(ep_match.group(1)) if ep_match else None
                    k = ContentKind.MOVIE if kind_str == "MOVIE" else ContentKind.SPECIAL
                    corrected_entries.append(MappingEntry(
                        kind=k, season_number=0, title=title_rest,
                        episodes=ep_count, included=True,
                    ))

        if not corrected_entries:
            return None

        # Merge corrected entries back into the original mapping
        result = FranchiseMapping(
            anime_doc_id=current_mapping.anime_doc_id,
            root_title=current_mapping.root_title,
        )

        # For each corrected entry, find and update the original
        corrected_map: dict[tuple, MappingEntry] = {}
        for ce in corrected_entries:
            if ce.kind == ContentKind.SEASON:
                corrected_map[("season", ce.season_number, ce.season_part)] = ce
            else:
                corrected_map[("extra", ce.title or "", ce.kind.value)] = ce

        for original in current_mapping.entries:
            if original.kind == ContentKind.SEASON:
                key = ("season", original.season_number, original.season_part)
            else:
                key = ("extra", original.title or "", original.kind.value)

            if key in corrected_map:
                ce = corrected_map[key]
                original.season_number = ce.season_number
                original.season_part = ce.season_part
                original.episodes = ce.episodes or original.episodes
                original.included = ce.included
            result.entries.append(original)

        return result

    @staticmethod
    def format_mapping_code_block(mapping: FranchiseMapping) -> str:
        """Format the mapping as a copyable code block.

        Returns lines like:
          Attack on Titan → Season 1
          Attack on Titan Season 2 → Season 2
          Attack on Titan Season 3 → Season 3 Part 1
          Attack on Titan Season 3 Part 2 → Season 3 Part 2
          Movie: No Regrets
          OVA: Lost Girls
        """
        lines: list[str] = []
        for entry in mapping.included_entries:
            if entry.kind == ContentKind.SEASON:
                label = f"Season {entry.season_number}"
                if entry.season_part:
                    label += f" Part {entry.season_part}"
                ep_str = f" ({entry.episodes} eps)" if entry.episodes else ""
                short_title = entry.title[:60] if entry.title else ""
                if short_title:
                    lines.append(f"{short_title} → {label}{ep_str}")
                else:
                    lines.append(f"{label}{ep_str}")
            elif entry.kind == ContentKind.MOVIE:
                title_part = f": {entry.title[:60]}" if entry.title else ""
                ep_str = f" ({entry.episodes} ep)" if entry.episodes else ""
                lines.append(f"Movie{title_part}{ep_str}")
            else:
                fmt_label = entry.kind.value.title()
                title_part = f": {entry.title[:60]}" if entry.title else ""
                ep_str = f" ({entry.episodes} ep)" if entry.episodes else ""
                lines.append(f"{fmt_label}{title_part}{ep_str}")

        return "\n".join(lines)

    @staticmethod
    def format_mapping_report(mapping: FranchiseMapping) -> str:
        """Build a post-processing confirmation message with a copyable code block.

        The code block shows mapping info that can be copied and edited.
        Buttons are: Confirm and Edit (which enables reply mode).
        """
        lines: list[str] = []
        lines.append(f"<b>📦 Post-Processing Mapping Confirmation</b>")
        lines.append(f"<b>{mapping.root_title}</b>")
        lines.append("")

        code_block = FranchiseFlowService.format_mapping_code_block(mapping)
        lines.append(f"<pre>{_esc(code_block)}</pre>")

        lines.append("")
        lines.append("<i>Confirm to accept this mapping.</i>")
        lines.append("<i>Press Edit, then reply to this message with corrections.</i>")

        return "\n".join(lines)
