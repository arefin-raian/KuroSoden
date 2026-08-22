"""Franchise-to-torrent mapping — bridges torrent file structure to AniList franchise entries.

A torrent may contain flat season numbering (S03E01-S03E22) while AniList splits
that into Season 3 Part 1 (12 eps) and Part 2 (10 eps). This service maps each
torrent file to the correct franchise entry, handling season parts by consuming
files sequentially based on each part's episode count.

Episode numbers are ALWAYS extracted from filenames — never guessed. When a gap
is detected (e.g., EP5 missing from an otherwise complete S01), it is flagged so
the admin can provide a supplemental torrent or individual file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.tier_gapfill import res_height, tiers_to_encode


def _normalize_title(text: str) -> str:
    """Lowercase, strip punctuation/underscores/dots → single-spaced words, so a
    filename fragment and an episode title compare on words alone."""
    text = re.sub(r"[._\-]+", " ", (text or "").lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_episodes_by_title(
    ordered_files: list[dict], episode_titles: dict[int, list[dict]],
) -> int:
    """Supplementary tier: for files with NO parsed episode number, try to pin it
    from an episode TITLE embedded in the filename.

    The owner's insight: many releases name files like ``Show - 05 - The Duel``
    or ``Show S01 The Duel`` — when the numeric marker is missing/garbled but the
    title text is present, the title uniquely identifies the episode. We only fill
    ``episode`` where it is currently ``None`` (never override a confident numeric
    match), and only on an UNAMBIGUOUS, sufficiently-long title hit — so a short or
    repeated title can't mis-map. Best-effort: returns how many it filled.

    Not universal (many files carry no title) — it simply recovers the ones that
    do. ``episode_titles`` is ``{anilist_id: [{number,title}, …]}`` from Jikan.
    """
    # Flatten all known (number, normalized-title) pairs; drop trivially-short or
    # duplicate titles that would match ambiguously.
    seen: dict[str, int | None] = {}
    for rows in (episode_titles or {}).values():
        for row in rows or []:
            num, ttl = row.get("number"), _normalize_title(row.get("title") or "")
            if num is None or len(ttl) < 6:  # too short → ambiguous, skip
                continue
            seen[ttl] = None if ttl in seen else num  # duplicate title → ambiguous
    candidates = {ttl: num for ttl, num in seen.items() if num is not None}
    if not candidates:
        return 0

    filled = 0
    for f in ordered_files:
        if f.get("episode") is not None or f.get("kind", "episode") != "episode":
            continue
        norm_name = _normalize_title(f.get("name") or "")
        # Longest title first so the most specific match wins.
        for ttl in sorted(candidates, key=len, reverse=True):
            if ttl in norm_name:
                f["episode"] = candidates[ttl]
                filled += 1
                break
    return filled


@dataclass
class FileAssignment:
    """One torrent file assigned to a franchise entry.

    ``resolutions`` holds the quality tiers this LOGICAL episode ships (a DDL
    release provides the same episode in several tiers as sibling files, so one
    assignment carries e.g. ``["1080p", "720p", "480p"]``). Empty for torrent
    (quality isn't known until download), so the mapping card shows no quality
    line for torrents — only DDL, whose files are already extracted.
    """
    file_index: int
    filename: str
    episode_number: int | None
    kind: str = "episode"
    season: int = 1
    resolutions: list[str] = field(default_factory=list)


@dataclass
class MissingEpisode:
    """An episode expected but not found in the torrent."""
    season_number: int
    episode_number: int
    title: str = ""


@dataclass
class TorrentMappingEntry:
    """A franchise entry with its assigned torrent files."""
    franchise_entry: MappingEntry
    files: list[FileAssignment] = field(default_factory=list)
    confidence: float = 0.0
    missing: list[MissingEpisode] = field(default_factory=list)

    @property
    def expected(self) -> int | None:
        return self.franchise_entry.episodes

    @property
    def actual(self) -> int:
        return len(self.files)

    @property
    def label(self) -> str:
        e = self.franchise_entry
        if e.kind == ContentKind.SEASON:
            s = f"S{e.season_number:02d}"
            if e.season_part:
                s += f" Part {e.season_part}"
            title = e.title
            if title:
                s += f" — {title[:40]}"
            return s
        return f"{e.kind.value.title()}: {e.title[:50]}"

    @property
    def present_resolutions(self) -> list[str]:
        """Union of the quality tiers shipped across this entry's files, ordered
        high→low (e.g. ``["1080p", "720p", "480p"]``). Empty when the files carry
        no resolution info (torrent — quality unknown until download)."""
        seen: set[str] = set()
        for f in self.files:
            for r in (f.resolutions or []):
                if r:
                    seen.add(r)
        return sorted(seen, key=lambda r: res_height(r), reverse=True)

    def tiers_to_encode(self, encode_heights, fallbacks_cfg) -> list[int]:
        """Lower tiers that WILL be derived for this entry, per the shared
        gap-fill rule — empty when it already ships every requested tier, or when
        quality is unknown (torrent). Same result the encoder produces."""
        present = {res_height(r) for r in self.present_resolutions}
        present.discard(0)
        if not present:
            return []
        return tiers_to_encode(present, encode_heights, fallbacks_cfg)


@dataclass
class TorrentMapping:
    """Complete mapping of a torrent's files to franchise entries."""
    torrent_name: str
    entries: list[TorrentMappingEntry] = field(default_factory=list)
    unmatched: list[FileAssignment] = field(default_factory=list)
    overall_confidence: float = 0.0

    @property
    def included_entries(self) -> list[TorrentMappingEntry]:
        return [e for e in self.entries if e.franchise_entry.included]

    @property
    def has_gaps(self) -> bool:
        return any(e.missing for e in self.entries)

    @property
    def all_missing(self) -> list[MissingEpisode]:
        out = []
        for e in self.entries:
            out.extend(e.missing)
        return out

    def to_dict(self) -> dict:
        return {
            "torrent_name": self.torrent_name,
            "overall_confidence": round(self.overall_confidence, 2),
            "entries": [
                {
                    "anilist_id": e.franchise_entry.anilist_id,
                    "kind": e.franchise_entry.kind.value,
                    "season_number": e.franchise_entry.season_number,
                    "season_part": e.franchise_entry.season_part,
                    "title": e.franchise_entry.title,
                    "episodes": e.franchise_entry.episodes,
                    "included": e.franchise_entry.included,
                    "confidence": round(e.confidence, 2),
                    "files": [
                        {
                            "file_index": f.file_index,
                            "filename": f.filename,
                            "episode_number": f.episode_number,
                            "kind": f.kind,
                            "season": f.season,
                            "resolutions": f.resolutions,
                        }
                        for f in e.files
                    ],
                    "missing": [
                        {
                            "season_number": m.season_number,
                            "episode_number": m.episode_number,
                            "title": m.title,
                        }
                        for m in e.missing
                    ],
                }
                for e in self.entries
            ],
            "unmatched": [
                {
                    "file_index": f.file_index,
                    "filename": f.filename,
                    "episode_number": f.episode_number,
                    "kind": f.kind,
                    "season": f.season,
                    "resolutions": f.resolutions,
                }
                for f in self.unmatched
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> TorrentMapping:
        entries = []
        for ed in d.get("entries", []):
            me = MappingEntry(
                anilist_id=ed.get("anilist_id"),
                kind=ContentKind(ed.get("kind", "season")),
                season_number=ed.get("season_number", 1),
                season_part=ed.get("season_part"),
                title=ed.get("title", ""),
                episodes=ed.get("episodes"),
                included=ed.get("included", True),
            )
            files = [
                FileAssignment(
                    file_index=f["file_index"],
                    filename=f["filename"],
                    episode_number=f.get("episode_number"),
                    kind=f.get("kind", "episode"),
                    season=f.get("season", 1),
                    resolutions=list(f.get("resolutions") or []),
                )
                for f in ed.get("files", [])
            ]
            missing = [
                MissingEpisode(
                    season_number=m.get("season_number", 0),
                    episode_number=m.get("episode_number", 0),
                    title=m.get("title", ""),
                )
                for m in ed.get("missing", [])
            ]
            entries.append(TorrentMappingEntry(
                franchise_entry=me, files=files,
                confidence=ed.get("confidence", 0.0),
                missing=missing,
            ))
        unmatched = [
            FileAssignment(
                file_index=f["file_index"],
                filename=f["filename"],
                episode_number=f.get("episode_number"),
                kind=f.get("kind", "episode"),
                season=f.get("season", 1),
                resolutions=list(f.get("resolutions") or []),
            )
            for f in d.get("unmatched", [])
        ]
        return cls(
            torrent_name=d.get("torrent_name", ""),
            entries=entries,
            unmatched=unmatched,
            overall_confidence=d.get("overall_confidence", 0.0),
        )


def build_torrent_mapping(
    ordered_files: list[dict],
    franchise: FranchiseMapping,
    *,
    episode_titles: dict[int, list[dict]] | None = None,
    season_overrides: dict[int, int] | None = None,
    episode_overrides: dict[int, int] | None = None,
    junk_indices: set[int] | None = None,
) -> TorrentMapping:
    """Map torrent files to franchise entries.

    ``ordered_files`` comes from ``order_episodes()`` — each dict has ``index``,
    ``name``, ``season``, ``episode``, ``kind``, ``seq``.

    ``franchise`` is the AniList-derived franchise mapping with entries for each
    season/part/movie/special.

    ``episode_titles`` is an optional dict mapping franchise entry anilist_id to
    a list of ``{number, title}`` from Jikan. Used both for gap detection (when an
    expected episode number is absent, the gap is flagged with its title) AND to
    feed the season-mapping cascade's title-match tier.

    ``season_overrides`` maps ``file_index → season`` — an admin's manual season
    decision from the mapping card; it wins over every automatic tier.

    ``junk_indices`` is a set of ``file_index`` the admin marked "not an episode"
    in Edit Mapping — those files are forced out of the episode stream so they
    land in ``unmatched`` (the "doesn't belong here" bucket) instead of being
    assigned an episode slot (the "Season 1 Trailer 1 became E01" fix).

    Before grouping, every file's season (and episode, for flat absolute-numbered
    releases) is run through the season-mapping cascade so a franchise with several
    seasons is no longer collapsed onto S1 by ``parse_release_meta``'s default.
    """
    episode_titles = episode_titles or {}
    junk_indices = junk_indices or set()

    # ── season-mapping cascade (auto → MAL titles → absolute → manual) ──────────
    # Rewrite each file's season/episode from the resolver before the historic
    # grouping runs. Fail-open: any resolver error leaves the parsed values.
    ordered_files = _apply_season_resolution(
        ordered_files, franchise, episode_titles, season_overrides,
    )

    # Manual episode order (the web mapping editor's drag-to-reorder): an explicit
    # file_index → episode-number map that WINS over the filename-parsed number, so
    # the admin's chosen position is authoritative. Applied after the season
    # cascade so it also fixes an episode the cascade couldn't derive.
    if episode_overrides:
        ordered_files = [
            {**f, "episode": episode_overrides[f["index"]]}
            if f.get("index") in episode_overrides else f
            for f in ordered_files
        ]

    # Admin-marked junk: force these files out of the episode stream so they are
    # never assigned an episode slot and instead surface as unmatched.
    if junk_indices:
        ordered_files = [
            {**f, "kind": "extra"} if f.get("index") in junk_indices else f
            for f in ordered_files
        ]

    # Supplementary title-match tier: recover an episode number for any file that
    # still has none by matching an embedded episode TITLE (from Jikan) against
    # the filename. Never overrides a numeric match; best-effort only.
    if episode_titles:
        try:
            _match_episodes_by_title(ordered_files, episode_titles)
        except Exception:  # noqa: BLE001 — a title-match hiccup never breaks mapping
            pass

    torrent_name = ""
    for f in ordered_files:
        path = f.get("path", "")
        if "/" in path:
            torrent_name = path.split("/")[0]
            break

    # Group torrent files by (season, kind).
    season_files: dict[int, list[dict]] = {}
    extra_files: list[dict] = []

    for f in ordered_files:
        kind = f.get("kind", "episode")
        season = f.get("season", 1)
        if kind == "episode":
            season_files.setdefault(season, []).append(f)
        else:
            extra_files.append(f)

    # Group franchise entries by season_number for season-type entries.
    season_entries: dict[int, list[MappingEntry]] = {}
    extra_entries: list[MappingEntry] = []

    for entry in franchise.entries:
        if entry.kind == ContentKind.SEASON:
            season_entries.setdefault(entry.season_number, []).append(entry)
        else:
            extra_entries.append(entry)

    # Sort parts within each season by season_part.
    for sn in season_entries:
        season_entries[sn].sort(key=lambda e: e.season_part or 0)

    result_entries: list[TorrentMappingEntry] = []
    used_file_indices: set[int] = set()

    # Map season episodes.
    for sn, parts in sorted(season_entries.items()):
        files = season_files.get(sn, [])
        # Sort by episode number extracted from filename (never guessed).
        files.sort(key=lambda f: (f.get("episode") or 0, f.get("seq", 0)))

        if len(parts) == 1:
            entry = parts[0]
            assignments = _assign_files(files, sn)
            missing = _detect_gaps(
                files, entry.episodes, sn,
                episode_titles.get(entry.anilist_id or 0, []),
            )
            confidence = _compute_confidence(entry.episodes, len(files), len(missing))
            result_entries.append(TorrentMappingEntry(
                franchise_entry=entry, files=assignments,
                confidence=confidence, missing=missing,
            ))
            used_file_indices.update(f["index"] for f in files)
        else:
            # Multiple parts — consume files sequentially by episode count.
            cursor = 0
            for entry in parts:
                count = entry.episodes or 0
                if count == 0:
                    chunk = files[cursor:]
                else:
                    chunk = files[cursor:cursor + count]
                cursor += len(chunk)

                assignments = _assign_files(chunk, sn)
                missing = _detect_gaps(
                    chunk, entry.episodes, sn,
                    episode_titles.get(entry.anilist_id or 0, []),
                )
                confidence = _compute_confidence(
                    entry.episodes, len(chunk), len(missing),
                )
                result_entries.append(TorrentMappingEntry(
                    franchise_entry=entry, files=assignments,
                    confidence=confidence, missing=missing,
                ))
                used_file_indices.update(f["index"] for f in chunk)

    # Map extras (movies, OVAs, specials).
    used_extras: set[int] = set()
    for entry in extra_entries:
        target_kind = entry.kind.value
        matched = [
            f for f in extra_files
            if f["index"] not in used_file_indices
            and f["index"] not in used_extras
            and _kind_matches(f.get("kind", ""), target_kind)
        ]
        if not matched and entry.kind == ContentKind.SPECIAL:
            matched = [
                f for f in extra_files
                if f["index"] not in used_file_indices
                and f["index"] not in used_extras
                and f.get("kind") in ("special", "ova", "extra")
            ]

        assignments = [
            FileAssignment(
                file_index=f["index"],
                filename=f["name"],
                episode_number=f.get("episode"),
                kind=f.get("kind", "episode"),
                season=f.get("season", 0),
                resolutions=list(f.get("resolutions") or []),
            )
            for f in matched
        ]
        confidence = _compute_confidence(entry.episodes, len(matched), 0)
        result_entries.append(TorrentMappingEntry(
            franchise_entry=entry, files=assignments,
            confidence=confidence,
        ))
        used_extras.update(f["index"] for f in matched)
    used_file_indices.update(used_extras)

    # Collect unmatched files.
    unmatched = [
        FileAssignment(
            file_index=f["index"],
            filename=f["name"],
            episode_number=f.get("episode"),
            kind=f.get("kind", "episode"),
            season=f.get("season", 0),
            resolutions=list(f.get("resolutions") or []),
        )
        for f in ordered_files
        if f["index"] not in used_file_indices
    ]

    # Overall confidence.
    total_files = sum(e.actual for e in result_entries)
    if total_files > 0:
        overall = sum(e.confidence * e.actual for e in result_entries) / total_files
    else:
        overall = 0.0

    return TorrentMapping(
        torrent_name=torrent_name,
        entries=result_entries,
        unmatched=unmatched,
        overall_confidence=overall,
    )


def _apply_season_resolution(
    ordered_files: list[dict],
    franchise: FranchiseMapping,
    episode_titles: dict[int, list[dict]],
    season_overrides: dict[int, int] | None,
) -> list[dict]:
    """Run the season-mapping cascade and return copies of ``ordered_files`` with
    each episode's ``season`` (and ``episode`` for absolute folds) rewritten.

    Extras and unresolved files keep their parsed values. Fail-open: any error
    returns the input untouched so a resolver bug never breaks the mapping flow.
    """
    try:
        from nekofetch.services.season_resolver import resolve_seasons

        # The resolver keys titles by season_number; ``episode_titles`` is keyed by
        # anilist_id. Re-key via the franchise entries.
        id_to_season = {
            e.anilist_id: e.season_number
            for e in franchise.entries
            if e.kind == ContentKind.SEASON and e.anilist_id
        }
        titles_by_season: dict[int, list[dict]] = {}
        for aid, titles in episode_titles.items():
            sn = id_to_season.get(aid)
            if sn:
                titles_by_season.setdefault(sn, []).extend(titles)

        res = resolve_seasons(
            ordered_files, franchise,
            titles_by_season=titles_by_season or None,
            overrides=season_overrides,
        )
        by_index = {a.file_index: a for a in res.assignments}
        out: list[dict] = []
        for f in ordered_files:
            a = by_index.get(f.get("index"))
            if a is None:  # unresolved — keep parsed values as a best-effort
                out.append(dict(f))
                continue
            nf = dict(f)
            nf["season"] = a.season
            if a.episode is not None:
                nf["episode"] = a.episode
            out.append(nf)
        return out
    except Exception:  # noqa: BLE001 — never break mapping on a resolver error
        return ordered_files


def _assign_files(files: list[dict], season: int) -> list[FileAssignment]:
    return [
        FileAssignment(
            file_index=f["index"],
            filename=f["name"],
            episode_number=f.get("episode"),
            kind=f.get("kind", "episode"),
            season=f.get("season", season),
            resolutions=list(f.get("resolutions") or []),
        )
        for f in files
    ]


def _detect_gaps(
    files: list[dict],
    expected_count: int | None,
    season_number: int,
    ep_titles: list[dict],
) -> list[MissingEpisode]:
    """Detect missing episodes by checking for gaps in the episode number sequence.

    Only reports gaps when episode numbers are reliably extracted from filenames.
    """
    if not files:
        return []

    ep_nums = [f.get("episode") for f in files if f.get("episode") is not None]
    if not ep_nums:
        return []

    # Build a title lookup from Jikan data.
    title_map: dict[int, str] = {}
    for et in ep_titles:
        title_map[et["number"]] = et.get("title", "")

    # Determine expected range: 1..expected_count if we know it, else 1..max(found).
    lo = min(ep_nums)
    if expected_count and expected_count > 0:
        hi = lo + expected_count - 1
    else:
        hi = max(ep_nums)

    present = set(ep_nums)
    missing = []
    for n in range(lo, hi + 1):
        if n not in present:
            missing.append(MissingEpisode(
                season_number=season_number,
                episode_number=n,
                title=title_map.get(n, ""),
            ))
    return missing


def _compute_confidence(
    expected: int | None, actual: int, gaps: int = 0,
) -> float:
    if expected is None:
        return 0.7 if actual > 0 else 0.0
    if expected == 0:
        return 1.0 if actual == 0 else 0.5
    if gaps > 0:
        return max(0.3, 1.0 - (gaps / expected))
    if actual == expected:
        return 1.0
    ratio = actual / expected
    if 0.8 <= ratio <= 1.2:
        return 0.8
    if 0.5 <= ratio <= 1.5:
        return 0.5
    return 0.3


def _kind_matches(torrent_kind: str, franchise_kind: str) -> bool:
    if torrent_kind == franchise_kind:
        return True
    if franchise_kind == "movie" and torrent_kind == "movie":
        return True
    if franchise_kind == "special" and torrent_kind in ("special", "ova"):
        return True
    return False


async def fetch_episode_titles_for_franchise(
    franchise: FranchiseMapping,
) -> dict[int, list[dict]]:
    """Fetch episode titles from Jikan for each franchise entry.

    Returns a dict mapping anilist_id → [{number, title}, ...].
    Only fetches for season-type entries that have an anilist_id.
    Rate-limited via the MyAnimeListClient's internal throttle.
    """
    from nekofetch.sources.telegram.myanimelist import MyAnimeListClient

    result: dict[int, list[dict]] = {}
    client = MyAnimeListClient()
    try:
        for entry in franchise.entries:
            if entry.kind != ContentKind.SEASON:
                continue
            if not entry.anilist_id:
                continue
            if not entry.title:
                continue
            titles = await client.episode_titles_by_query(entry.title)
            if titles:
                result[entry.anilist_id] = titles
    finally:
        await client.close()
    return result
