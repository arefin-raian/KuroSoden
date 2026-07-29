"""Confident source matching — never grab the wrong title/season.

A website search for "Naruto" can return a recap, a spin-off, or the wrong
season. To be certain we have the *exact* show, we search each source with BOTH
the AniList English and Romaji titles and only accept a result whose own title
strongly matches one of them (in both directions, so a season-2 superset doesn't
get mistaken for season 1).
"""

from __future__ import annotations

from difflib import SequenceMatcher

from nekofetch.core.logging import get_logger
from nekofetch.sources.base import AnimeSource, AnimeStub
from nekofetch.sources.telegram.matching import normalize_words

log = get_logger(__name__)


def strong_title_match(a: str, b: str, *, threshold: float = 0.85) -> bool:
    """True if two titles are near-identical by meaningful word overlap.

    Bidirectional: ≥ ``threshold`` of *each* title's words must be shared. This
    rejects supersets — "Naruto" vs "Naruto Shippuuden" fails (the second has an
    extra distinguishing word), so we never collapse distinct seasons.

    Falls back to character-level similarity (SequenceMatcher >= 0.90) to handle
    Romanization drift like "Takopi" vs "Takopii".
    """
    wa, wb = normalize_words(a), normalize_words(b)
    if not wa or not wb:
        return False
    inter = len(wa & wb)
    if inter / len(wa) >= threshold and inter / len(wb) >= threshold:
        return True
    sa = " ".join(sorted(wa))
    sb = " ".join(sorted(wb))
    return SequenceMatcher(None, sa, sb).ratio() >= 0.90


async def find_verified_match(
    source: AnimeSource, titles: list[str], *, limit: int = 6
) -> AnimeStub | None:
    """Search ``source`` with each candidate title and return the first result
    whose title is verified against the AniList titles. ``None`` if nothing on the
    site confidently matches — better no match than the wrong show.
    """
    candidates = [t for t in titles if t]
    if not candidates:
        return None
    total_seen = 0
    for query in candidates:
        try:
            results = await source.search(query)
        except Exception:
            continue
        total_seen = len(results)
        for stub in results[:limit]:
            if any(strong_title_match(stub.title, t) for t in candidates):
                return stub
    log.warning(
        "match.not_found", source=getattr(source, "name", "?"),
        candidates=candidates, total_seen=total_seen,
    )
    return None
