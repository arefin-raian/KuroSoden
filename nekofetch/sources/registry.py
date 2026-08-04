"""Source registry — discovery and instantiation of authorized sources.

Only names listed in ``config.sources.enabled`` are activated. A pirate-site plugin,
even if present on disk, would still require explicit enabling — and none ship.
"""

from __future__ import annotations

from nekofetch.core.exceptions import ConfigError
from nekofetch.sources.base import AnimeSource

# Logical source aliases used by the admin/review flow → concrete plugin names.
# Staff pick "Torrent"/"Website"; those map onto the real download plugins.
_ALIASES = {
    "torrent": "nyaa",
    "website": "anikoto",
    "telegram_manual": "telegram",
}


class SourceRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, type[AnimeSource]] = {}
        self._instances: dict[str, AnimeSource] = {}
        self._default: str | None = None

    def register(self, source_cls: type[AnimeSource]) -> None:
        self._factories[source_cls.name] = source_cls

    def activate(self, enabled: list[str], *, default: str | None = None, **kwargs) -> None:
        """Instantiate the enabled sources. Extra kwargs are passed to constructors."""
        for name in enabled:
            cls = self._factories.get(name)
            if cls is None:
                raise ConfigError(f"Unknown or unavailable source: {name!r}")
            self._instances[name] = cls(**kwargs.get(name, {}))
        if default and default in self._instances:
            self._default = default
        elif self._instances and self._default is None:
            self._default = next(iter(self._instances))

    def get(self, name: str) -> AnimeSource:
        if name not in self._instances:
            raise ConfigError(f"Source not active: {name!r}")
        return self._instances[name]

    def resolve(self, assigned: str) -> AnimeSource:
        """Resolve a request's *assigned* source string to a live download source.

        The string stored on a request is not always a bare plugin name:

        * ``"anikoto>kickassanime"`` — a website priority list; try each in order.
        * ``"telegram_manual"`` — manual Telegram fallback; served by ``telegram``.
        * ``"anilist"`` — a discovery/metadata layer, never a real download source;
          fall back to the configured default plugin.

        Returns the first active source that matches, raising ``ConfigError`` only
        when nothing in the chain is available.
        """
        candidates: list[str] = []
        for token in assigned.split(">"):
            token = token.strip()
            if not token:
                continue
            token = _ALIASES.get(token, token)
            if token == "anilist":   # discovery layer, not a download plugin
                continue
            candidates.append(token)

        last_error: str | None = None
        for name in candidates:
            if name in self._instances:
                return self._instances[name]
            last_error = name
        # Nothing matched — fall back to the default plugin if it's active.
        if self._default and self._default in self._instances:
            return self._instances[self._default]
        raise ConfigError(f"Source not active: {last_error or assigned!r}")

    def available(self) -> list[str]:
        return list(self._instances)


def build_default_registry() -> SourceRegistry:
    """Register all built-in authorized sources."""
    from nekofetch.sources.anikoto import AnikotoSource
    from nekofetch.sources.anizone import AnizoneSource
    from nekofetch.sources.ddl import DdlSource
    from nekofetch.sources.kickassanime import KickAssAnimeSource
    from nekofetch.sources.local import LocalFileSource
    from nekofetch.sources.miruro import MiruroSource
    from nekofetch.sources.nyaa import NyaaSource
    from nekofetch.sources.telegram.source import TelegramSource

    registry = SourceRegistry()
    registry.register(LocalFileSource)
    registry.register(KickAssAnimeSource)
    registry.register(AnikotoSource)
    registry.register(AnizoneSource)
    registry.register(MiruroSource)
    registry.register(NyaaSource)
    registry.register(DdlSource)
    registry.register(TelegramSource)
    return registry
