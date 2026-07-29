"""Dependency-injection container.

A single composition root that builds and holds long-lived singletons (DB clients,
cipher, config) and lazily constructs repositories and services. Bots and handlers
receive the container rather than importing infrastructure directly, keeping the
dependency arrows pointing inward.

Infrastructure imports are deferred to ``startup()`` so importing this module is cheap
and free of side effects (useful for tests and tooling).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nekofetch.core.config import AppConfig, EnvSettings, get_app_config, get_env
from nekofetch.core.logging import get_logger
from nekofetch.core.security import TokenCipher
from nekofetch.sources.registry import SourceRegistry, build_default_registry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from motor.motor_asyncio import AsyncIOMotorDatabase
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from nekofetch.infrastructure.database.mongo.collections import Collections
    from nekofetch.infrastructure.database.redis.progress import ProgressStore

log = get_logger(__name__)


class Container:
    """Composition root. Build with :meth:`create`, then ``await startup()``."""

    def __init__(self, env: EnvSettings, config: AppConfig) -> None:
        self.env = env
        self.config = config
        self.cipher = TokenCipher(env.secret_key)

        # Secret lives in .env: let ``TELEGRAPH_ACCESS_TOKEN`` override the
        # (optional) config.yaml ``thumbnail_channel.telegraph_access_token`` so
        # every reader (image_backup, senku_thumbnail_adapter, thumbnail_channel_
        # service, publishing_service gate, bot_content) sees one resolved value.
        if getattr(env, "telegraph_access_token", ""):
            self.config.thumbnail_channel.telegraph_access_token = env.telegraph_access_token

        # Stateless singletons available immediately. Reuse the one shared catalog
        # (absolute path, CWD-independent) so t() and container.localizer.get()
        # read the same en.json and edits propagate on restart.
        from nekofetch.localization import messages as _messages

        _messages.reload()  # pick up any en.json edits made since import
        self.localizer = _messages.localizer
        log.info(
            "localization.loaded",
            path=str(_messages.LANG_DIR / "en.json"),
            keys=len(self.localizer._catalogs.get("en", {})),
        )
        self.sources: SourceRegistry = build_default_registry()

        # Metadata enrichment provider (the pluggable scraping seam). It is safe to
        # construct unconditionally: until its scraper is implemented it no-ops.
        from nekofetch.providers.metadata.registry import build_metadata_provider
        from nekofetch.providers.shortlink.registry import build_shortlink_provider

        self.metadata_provider = build_metadata_provider()
        self.shortlink_provider = build_shortlink_provider(config.shortlink)

        # Populated by startup()
        self.pg_engine: AsyncEngine | None = None
        self.pg_sessionmaker: async_sessionmaker | None = None
        self.mongo: AsyncIOMotorDatabase | None = None
        self.collections: Collections | None = None
        self.redis: Redis | None = None
        self.progress: ProgressStore | None = None
        self._services: dict[str, Any] = {}

        # API clients (thin, constructed immediately — no I/O until used)
        from nekofetch.providers.metadata.tmdb import TmdbClient
        from nekofetch.sources.telegram.resilient_client import ResilientMetadataClient
        self.anilist = ResilientMetadataClient()
        # Arm the @acutebot userbot tier as the last resort when both AniList
        # and Jikan miss. Stays dormant unless a userbot session is present.
        self.anilist.enable_acute_fallback(env)
        self.tmdb = TmdbClient(
            token=env.tmdb_read_access_token,
            api_key=env.tmdb_api_key,
        )

        from nekofetch.providers.metadata.series import SeriesResolver
        self.series_resolver = SeriesResolver(self.anilist)

    @classmethod
    def create(cls) -> Container:
        return cls(env=get_env(), config=get_app_config())

    async def startup(self) -> None:
        """Open all infrastructure connections. Idempotent per process."""
        from motor.motor_asyncio import AsyncIOMotorClient
        from redis.asyncio import Redis
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from nekofetch.infrastructure.database.mongo.collections import Collections
        from nekofetch.infrastructure.database.postgres.session import create_all
        from nekofetch.infrastructure.database.redis.progress import ProgressStore

        log.info("container.startup", db="postgres+mongo+redis")

        self.pg_engine = create_async_engine(self.env.postgres_dsn, pool_pre_ping=True)
        self.pg_sessionmaker = async_sessionmaker(self.pg_engine, expire_on_commit=False)
        if self.env.auto_create_schema:
            await create_all(self.pg_engine)  # dev convenience; Alembic owns prod schema

        # Seed dynamic index-section mapping (idempotent).
        from nekofetch.services.index_channel_service import seed_index_sections
        await seed_index_sections(self.pg_sessionmaker)

        # Seed the owner as a full-coverage pool admin (idempotent). Without this
        # the owner is a plain user and — critically — absent from the admin pool,
        # so AssignmentEngine.assign finds nobody and writes no task row, leaving
        # Levi's task list empty even for a QUEUED request.
        try:
            from kurosoden.shared.owner_seed import seed_owner
            await seed_owner(self)
        except Exception as exc:  # noqa: BLE001 — never block startup on seeding
            log.warning("owner_seed.failed", error=str(exc))

        try:
            # On Render, MongoDB Atlas M0 free-tier clusters sometimes reject TLS
            # handshakes due to CA-certificate mismatches in the Docker slim image.
            # ``tlsAllowInvalidCertificates`` is already set on the URI via .env;
            # we also try ``certifi`` as a CA bundle fallback when available.
            mongo_kw: dict = {
                "serverSelectionTimeoutMS": 15000,
                "connectTimeoutMS": 10000,
            }
            try:
                import certifi  # type: ignore[import-untyped]
                mongo_kw["tlsCAFile"] = certifi.where()
            except ImportError:
                pass
            self.mongo = AsyncIOMotorClient(
                self.env.mongo_uri, **mongo_kw
            )[self.env.mongo_db]
            await self.mongo.list_collection_names()  # force connection check
        except Exception as exc:
            log.error("mongo.connect.failed", error=str(exc))
            raise
        self.collections = Collections(self.mongo)
        await self.collections.ensure_indexes()

        # Managed Redis (Render/Railway/Upstash) silently drops idle connections;
        # without these, the first command after an idle gap raises
        # ConnectionError("Connection closed by server") and crashes the handler
        # mid-flow. health_check_interval pings idle conns; retry_on_* + keepalive
        # make redis-py transparently reconnect instead of surfacing the drop.
        from redis.backoff import ExponentialBackoff
        from redis.retry import Retry

        try:
            self.redis = Redis.from_url(
                self.env.redis_url,
                decode_responses=True,
                health_check_interval=30,
                socket_keepalive=True,
                socket_connect_timeout=15,
                socket_timeout=15,
                retry_on_timeout=True,
                retry=Retry(ExponentialBackoff(cap=10, base=0.5), retries=3),
            )
            await self.redis.ping()
            log.info("redis.connected", url=self.env.redis_url.split("@")[-1])
        except Exception as exc:
            log.error(
                "redis.connect.failed",
                url=self.env.redis_url.split("@")[-1],
                error=str(exc),
                hint=(
                    "TLS on port 6379 may be blocked by your firewall/antivirus. "
                    "Try a local Redis (redis://localhost:6379) or check your network."
                ),
            )
            raise
        self.progress = ProgressStore(self.redis)

        # Apply persisted runtime overrides (admin settings panel) over config.yaml.
        from nekofetch.services.settings_service import SettingsService

        await SettingsService(self).apply_overrides()

        # Activate only authorized sources listed in config.
        self.sources.activate(
            self.config.sources.enabled,
            default=self.config.sources.default,
            miruro=self.config.sources.miruro.model_dump(),
        )

        self.env.storage_path.mkdir(parents=True, exist_ok=True)
        self.env.session_path.mkdir(parents=True, exist_ok=True)

    def session(self) -> AsyncSession:
        """Open a new Postgres session (caller manages the transaction scope)."""
        assert self.pg_sessionmaker is not None, "Container not started"
        return self.pg_sessionmaker()

    async def shutdown(self) -> None:
        log.info("container.shutdown")
        close = getattr(self.metadata_provider, "close", None)
        if close is not None:
            await close()
        if hasattr(self, 'anilist'):
            await self.anilist.close()
        if hasattr(self, 'tmdb'):
            await self.tmdb.close()
        if self.redis is not None:
            await self.redis.aclose()
        if self.pg_engine is not None:
            await self.pg_engine.dispose()
