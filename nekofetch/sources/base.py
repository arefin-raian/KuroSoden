"""The ``AnimeSource`` interface and its data models.

A source turns an authorized origin (local files, a licensed API) into a uniform
catalog NekoFetch can search, inspect, and download from. Implementations must only
expose content the operator is licensed/authorized to distribute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from nekofetch.domain.enums import AudioType, ContentKind


@dataclass(slots=True)
class AnimeStub:
    """Lightweight search result."""

    source_ref: str          # source-native identifier
    title: str
    poster_url: str | None = None
    year: int | None = None


@dataclass(slots=True)
class AnimeDetails:
    source_ref: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    synopsis: str | None = None
    genres: list[str] = field(default_factory=list)
    studio: str | None = None
    release_date: str | None = None
    poster_url: str | None = None
    banner_url: str | None = None
    season_count: int = 0
    episode_count: int = 0


@dataclass(slots=True)
class Episode:
    source_ref: str
    season: int
    number: int
    title: str | None = None
    kind: ContentKind = ContentKind.SEASON
    # Normalized franchise part for split seasons (e.g. S1P2). None means the
    # source locator is still using the season's native numbering.
    season_part: int | None = None


@dataclass(slots=True)
class VideoVariant:
    """A concrete downloadable rendition of an episode."""

    source_ref: str
    resolution: str                 # e.g. "1080p"
    audio: AudioType = AudioType.SUBBED
    languages: list[str] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)
    container: str | None = None    # mkv / mp4 ...
    size_bytes: int | None = None


@dataclass(slots=True)
class SourceCoverage:
    """A cheap per-source summary used by the Website report card so staff can
    compare sources before committing a download.

    ``sub_episodes`` / ``dub_episodes`` are how many episodes each audio actually
    offers — this is where sources diverge wildly (e.g. 220 subbed but 3 dubbed).
    ``approximate`` flags counts derived from sampling rather than a full listing.
    """

    source: str
    matched_title: str
    source_ref: str
    total_episodes: int = 0
    seasons: int = 1
    sub_episodes: int = 0
    dub_episodes: int = 0
    dual_episodes: int = 0
    available: bool = True
    approximate: bool = False
    note: str | None = None


# Called repeatedly during a download with (downloaded_bytes, total_bytes).
ProgressCallback = Callable[[int, int], Awaitable[None]]


class AnimeSource(ABC):
    """Base class for all authorized content sources."""

    #: Stable identifier used in config (`sources.enabled`) and request records.
    name: str = "base"

    @abstractmethod
    async def search(self, query: str) -> list[AnimeStub]:
        """Search the catalog for titles matching ``query``."""

    @abstractmethod
    async def get_details(self, source_ref: str) -> AnimeDetails:
        """Full metadata for a title (synopsis, genres, studio, season counts)."""

    @abstractmethod
    async def get_episodes(self, source_ref: str) -> list[Episode]:
        """All episodes (seasons/movies/specials detected)."""

    @abstractmethod
    async def get_variants(self, episode_ref: str) -> list[VideoVariant]:
        """Available renditions: resolutions, languages, audio, subtitles."""

    @abstractmethod
    async def download(
        self,
        variant: VideoVariant,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        resume_state: dict | None = None,
    ) -> dict:
        """Download ``variant`` to ``dest``.

        Must be resumable: accept and return a ``resume_state`` dict so an interrupted
        download can continue. Should invoke ``on_progress`` periodically. Returns the
        final resume/metadata state (e.g. checksum, bytes written).
        """

    async def coverage(self, *titles: str) -> SourceCoverage | None:
        """Cheap per-source summary used by the Website report card.

        Receives candidate titles (AniList English + Romaji); the source verifies
        a confident match before reporting. Sources that can't (or needn't)
        provide one return ``None``.
        """
        return None

    async def close(self) -> None:  # pragma: no cover - optional cleanup hook
        """Release any held resources (HTTP clients, handles)."""
        return None
