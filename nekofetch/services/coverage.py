"""Post-round coverage: what does the franchise still owe us?

Phase 4's iterative multi-source loop needs to answer one question after every
download round: *given everything we've fetched so far, which (season, episode)
units is this franchise still missing?* — so it can either ask the admin for
more links or let the job proceed to branding/upload.

Why a dedicated diff instead of ``TorrentMapping.all_missing``:
``torrent_mapping._detect_gaps`` only inspects episodes that a torrent actually
shipped, so it is blind to a *wholly-absent season* (zero files → zero gaps
reported) and to a missing *leading* run. The very case Phase 4 exists for — "the
first release covered S1+S2, we still need all of S3" — is exactly what that path
cannot see. So we compute expected units straight from the franchise mapping's
per-entry ``episodes`` count and subtract the (season, episode) pairs that already
have a recorded :class:`MediaFile` row.

This module is deliberately free of any bot / Telegram / Redis surface so it can
be unit-tested against an in-memory DB in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from nekofetch.infrastructure.database.postgres.models import MediaFile


@dataclass(frozen=True)
class MissingUnit:
    """One (season, episode) hole the franchise expects but we don't have."""
    season: int
    episode: int


@dataclass
class CoverageReport:
    """Result of diffing expected franchise units against downloaded rows."""
    missing: list[MissingUnit] = field(default_factory=list)
    # Seasons the franchise expects but for which we have ZERO downloaded units.
    empty_seasons: list[int] = field(default_factory=list)
    # Seasons we expected an episode count for and could therefore diff.
    resolved_seasons: list[int] = field(default_factory=list)
    # Seasons whose expected episode count is unknown (episodes is None); we
    # cannot assert completeness for these, so the loop treats them as "can't
    # prove missing" rather than fabricating holes.
    unknown_seasons: list[int] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when nothing resolvable is missing.

        Unknown-count seasons never *block* completion on their own — we can't
        invent an episode range for them — but if a known season has holes the
        franchise is not yet complete.
        """
        return not self.missing and not self.empty_seasons

    def grouped(self) -> dict[int, list[int]]:
        """Missing episodes grouped by season, each list sorted ascending."""
        out: dict[int, list[int]] = {}
        for u in self.missing:
            out.setdefault(u.season, []).append(u.episode)
        for eps in out.values():
            eps.sort()
        return out


def _expected_units(mapping) -> dict[int, int]:
    """Collapse an included franchise mapping to ``{season: total_episodes}``.

    Multi-part seasons (S3 Part 1 + Part 2) share a ``season_number``; their
    per-part ``episodes`` counts sum into the season's expected total so a
    contiguous 1..N episode range can be derived. Only SEASON-kind entries with a
    known count contribute; movies/specials (season 0, or ``episodes`` None) do
    not define a numbered-episode range and are left out of the diff.
    """
    from nekofetch.domain.enums import ContentKind

    totals: dict[int, int] = {}
    unknown: set[int] = set()
    for e in mapping.included_entries:
        if e.kind is not ContentKind.SEASON:
            continue
        if not e.season_number or e.season_number <= 0:
            continue
        if e.episodes is None:
            unknown.add(e.season_number)
            continue
        totals[e.season_number] = totals.get(e.season_number, 0) + int(e.episodes)
    # A season with any known-count part is resolvable even if another part's
    # count is unknown; only seasons with NO known count stay unknown.
    for s in list(unknown):
        if s in totals:
            unknown.discard(s)
    totals["__unknown__"] = sorted(unknown)  # type: ignore[assignment]
    return totals


async def compute_coverage(session, req, mapping) -> CoverageReport:
    """Diff the franchise's expected episode units against what's on record.

    ``mapping`` is a freshly-built :class:`FranchiseMapping` (episode counts per
    season). ``req`` supplies ``anime_doc_id`` — coverage is keyed by anime, not
    by job, so episodes fetched across several rounds/jobs all count.
    """
    expected = _expected_units(mapping)
    unknown_seasons: list[int] = expected.pop("__unknown__")  # type: ignore[assignment]

    rows = (await session.execute(
        select(MediaFile.season, MediaFile.episode).where(
            MediaFile.anime_doc_id == req.anime_doc_id,
        )
    )).all()
    have: dict[int, set[int]] = {}
    for season, episode in rows:
        if season is None or episode is None:
            continue
        have.setdefault(int(season), set()).add(int(episode))

    missing: list[MissingUnit] = []
    empty_seasons: list[int] = []
    resolved: list[int] = []
    for season, total in sorted(expected.items()):
        resolved.append(season)
        present = have.get(season, set())
        if not present:
            empty_seasons.append(season)
            missing.extend(MissingUnit(season, ep) for ep in range(1, total + 1))
            continue
        for ep in range(1, total + 1):
            if ep not in present:
                missing.append(MissingUnit(season, ep))

    return CoverageReport(
        missing=missing,
        empty_seasons=empty_seasons,
        resolved_seasons=resolved,
        unknown_seasons=unknown_seasons,
    )
