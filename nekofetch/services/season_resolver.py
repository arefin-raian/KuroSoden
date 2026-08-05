"""Season-mapping cascade — decide which franchise season each downloaded file belongs to.

`parse_release_meta` (in ``_torrent.py``) extracts a season number from every
filename, but it **defaults to season 1** whenever a name carries only an episode
number (the very common "flat"/absolute-numbering release: ``One Piece - 1069``,
``Attack on Titan - 60``). For a single-season request that default is correct; for
a multi-season franchise it silently piles every file onto S1.

This module resolves the ambiguity with a three-tier cascade, most-trusted first:

1. **auto**   — the filename already names an explicit season (``S03E01`` /
   ``Season 3``). ``parse_release_meta`` sets a truthy ``season_explicit`` flag for
   those; we trust it verbatim.
2. **titles** — no explicit season, but we have per-season episode *titles* from
   MyAnimeList (via ``fetch_episode_titles_for_franchise``). Match each file's
   parsed episode title against the franchise's season title lists; the season with
   the best title hit wins.
3. **absolute** — still ambiguous, but the franchise season episode-counts are
   known: fold a flat absolute episode number back into (season, episode) by
   walking the cumulative per-season boundaries (ep 60 with seasons of 25/25/22 →
   S3E10). This is deterministic and needs no network.

Anything the cascade cannot resolve is returned as ``unresolved`` so the caller can
fall back to a **manual** admin mapping card. The module is pure (no I/O, no session)
so it unit-tests cleanly; the MAL titles are fetched by the caller and passed in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping

# Confidence assigned to each resolution tier, high→low. Callers may surface these
# so an admin sees *why* a file landed where it did before accepting the mapping.
CONF_EXPLICIT = 1.0
CONF_TITLE = 0.85
CONF_ABSOLUTE = 0.7
CONF_SINGLE = 0.95   # only one season exists → nothing to disambiguate
CONF_NONE = 0.0


@dataclass
class SeasonAssignment:
    """One file resolved to a (season, episode) with the tier that decided it."""
    file_index: int
    filename: str
    season: int
    episode: int | None
    method: str          # "explicit" | "title" | "absolute" | "single" | "unresolved"
    confidence: float


@dataclass
class ResolveResult:
    assignments: list[SeasonAssignment] = field(default_factory=list)
    unresolved: list[SeasonAssignment] = field(default_factory=list)

    @property
    def all_resolved(self) -> bool:
        return not self.unresolved

    @property
    def overall_confidence(self) -> float:
        rows = self.assignments + self.unresolved
        if not rows:
            return 0.0
        return sum(a.confidence for a in rows) / len(rows)


# --------------------------------------------------------------------------- #
# season boundary table
# --------------------------------------------------------------------------- #

def _season_episode_counts(franchise: FranchiseMapping) -> dict[int, int]:
    """Collapse a franchise mapping to ``{season_number: total_episodes}``.

    Multi-part seasons (S3P1 + S3P2) are summed under their shared real season
    number, mirroring ``coverage._expected_units`` so the two views agree.
    """
    counts: dict[int, int] = {}
    for e in franchise.entries:
        if e.kind != ContentKind.SEASON:
            continue
        if not e.included:
            continue
        if e.season_number <= 0:
            continue
        counts[e.season_number] = counts.get(e.season_number, 0) + (e.episodes or 0)
    return counts


def _absolute_to_season(
    absolute_ep: int, counts: dict[int, int]
) -> tuple[int, int] | None:
    """Fold a flat absolute episode number into (season, within-season episode).

    Walks seasons in order, subtracting each season's episode count until the
    absolute number lands inside a season. Returns ``None`` when any season count
    is unknown (0) before the target, or the number overruns the known total —
    those cases are genuinely ambiguous and must fall through to manual mapping.
    """
    remaining = absolute_ep
    for season in sorted(counts):
        count = counts[season]
        if count <= 0:
            return None  # unknown boundary — can't fold safely
        if remaining <= count:
            return season, remaining
        remaining -= count
    return None


# --------------------------------------------------------------------------- #
# title matching
# --------------------------------------------------------------------------- #

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    """Lowercase + strip punctuation/spacing for tolerant title comparison."""
    return _NORM_RE.sub("", (text or "").lower())


def _title_from_name(filename: str) -> str:
    """Extract the human episode title from a release filename, if any.

    Release names occasionally carry the episode title after the number, e.g.
    ``[Grp] Show - 05 - To You, in 2000 Years [1080p].mkv``. We take the segment
    after the last ``-``-delimited episode token, stripping the group/quality
    brackets. Returns "" when no title-looking segment is present.
    """
    base = re.sub(r"\.[a-z0-9]{2,4}$", "", filename, flags=re.I)
    base = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", base)  # drop [group] (quality)
    # Split on " - " and take the trailing segment when it's wordy (not a number).
    parts = [p.strip() for p in re.split(r"\s-\s|_-_", base) if p.strip()]
    if len(parts) >= 2 and not re.fullmatch(r"[eE]?\d{1,4}v?\d*", parts[-1]):
        cand = parts[-1]
        if re.search(r"[a-z]", cand.lower()) and len(cand) > 2:
            return cand
    return ""


def _match_season_by_title(
    ep_title: str,
    titles_by_season: dict[int, list[dict]],
) -> tuple[int, int] | None:
    """Find the (season, episode) whose MAL title best matches ``ep_title``.

    ``titles_by_season`` maps season_number → ``[{number, title}]``. A match must
    be exact after normalisation to count — fuzzy partial matches are too risky to
    auto-assign a season on, so they fall through to manual. Returns the first
    exact hit, or None.
    """
    needle = _norm(ep_title)
    if not needle or len(needle) < 3:
        return None
    for season in sorted(titles_by_season):
        for et in titles_by_season[season]:
            if _norm(et.get("title", "")) == needle:
                return season, int(et["number"])
    return None


# --------------------------------------------------------------------------- #
# the cascade
# --------------------------------------------------------------------------- #

def resolve_seasons(
    ordered_files: list[dict],
    franchise: FranchiseMapping,
    *,
    titles_by_season: dict[int, list[dict]] | None = None,
    overrides: dict[int, int] | None = None,
) -> ResolveResult:
    """Resolve each file's season via the auto → titles → absolute cascade.

    ``ordered_files`` are ``order_episodes()`` dicts (``index``, ``name``,
    ``season``, ``episode``, ``season_explicit``, ``kind``). ``franchise`` supplies
    the per-season episode counts. ``titles_by_season`` (optional) maps
    season_number → ``[{number, title}]`` from MyAnimeList for tier 2.
    ``overrides`` maps ``file_index → season`` — an admin's manual decision, which
    wins over every automatic tier.

    Per file the first tier that fires wins:
      manual override > explicit season in name > MAL title match >
      absolute-number fold > (single-season shortcut) > unresolved.
    """
    overrides = overrides or {}
    titles_by_season = titles_by_season or {}
    counts = _season_episode_counts(franchise)
    single_season = len(counts) == 1
    only_season = next(iter(counts)) if single_season else None

    result = ResolveResult()

    for f in ordered_files:
        idx = f.get("index")
        name = f.get("name", "")
        kind = f.get("kind", "episode")
        parsed_ep = f.get("episode")

        # Extras (movies/OVAs/specials) aren't season episodes — pass through with
        # their parsed season untouched; the franchise mapper handles them.
        if kind != "episode":
            result.assignments.append(SeasonAssignment(
                file_index=idx, filename=name, season=f.get("season", 0),
                episode=parsed_ep, method="explicit", confidence=CONF_EXPLICIT,
            ))
            continue

        # 0) manual override — admin has the final word.
        if idx in overrides:
            result.assignments.append(SeasonAssignment(
                file_index=idx, filename=name, season=overrides[idx],
                episode=parsed_ep, method="manual", confidence=CONF_EXPLICIT,
            ))
            continue

        # 1) explicit season stated in the filename.
        if f.get("season_explicit"):
            result.assignments.append(SeasonAssignment(
                file_index=idx, filename=name, season=f.get("season", 1),
                episode=parsed_ep, method="explicit", confidence=CONF_EXPLICIT,
            ))
            continue

        # 2) MAL episode-title match.
        ep_title = _title_from_name(name)
        if ep_title and titles_by_season:
            hit = _match_season_by_title(ep_title, titles_by_season)
            if hit:
                season, ep = hit
                result.assignments.append(SeasonAssignment(
                    file_index=idx, filename=name, season=season,
                    episode=ep, method="title", confidence=CONF_TITLE,
                ))
                continue

        # 3) absolute-number fold across known season boundaries.
        if parsed_ep is not None and counts:
            folded = _absolute_to_season(parsed_ep, counts)
            if folded:
                # Only trust the fold when it actually crosses a boundary; an ep
                # that lands in S1 unchanged is the trivial single-season case.
                season, ep = folded
                if season != 1 or not single_season:
                    result.assignments.append(SeasonAssignment(
                        file_index=idx, filename=name, season=season,
                        episode=ep, method="absolute", confidence=CONF_ABSOLUTE,
                    ))
                    continue

        # 4) single-season franchise — nothing to disambiguate.
        if single_season:
            result.assignments.append(SeasonAssignment(
                file_index=idx, filename=name, season=only_season,
                episode=parsed_ep, method="single", confidence=CONF_SINGLE,
            ))
            continue

        # 5) give up → manual mapping.
        result.unresolved.append(SeasonAssignment(
            file_index=idx, filename=name, season=f.get("season", 1),
            episode=parsed_ep, method="unresolved", confidence=CONF_NONE,
        ))

    return result
