"""Owner-only database clear for Kuro Soden.

Clears operational state while preserving durable identity:

* Postgres keeps users, admin profiles/availability, and alembic_version.
* Mongo collections are emptied.
* Redis is flushed because this deployment owns the Redis database outright.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

KEEP_TABLES = {"users", "admin_availability", "alembic_version"}


@dataclass(frozen=True)
class DatabaseClearResult:
    postgres_truncated: int = 0
    postgres_kept: tuple[str, ...] = ()
    postgres_missing: tuple[str, ...] = ()
    mongo_cleared: int = 0
    redis_flushed: bool = False


class DatabaseClearService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def clear_operational_state(self) -> DatabaseClearResult:
        """Wipe request/content/runtime state and keep identity/profile rows.

        Stops the download worker **before** truncating so that no in-flight
        download task is mid-write when the rows vanish, then restarts it
        afterwards so the pipeline resumes from the (now empty) queue.
        """
        await self._stop_download_worker()
        postgres_truncated, missing = await self._clear_postgres()
        mongo_cleared = await self._clear_mongo()
        redis_flushed = await self._clear_redis()
        await self._restart_download_worker()
        result = DatabaseClearResult(
            postgres_truncated=postgres_truncated,
            postgres_kept=tuple(sorted(KEEP_TABLES)),
            postgres_missing=tuple(sorted(missing)),
            mongo_cleared=mongo_cleared,
            redis_flushed=redis_flushed,
        )
        log.warning(
            "database.clear.operational_state",
            postgres_truncated=result.postgres_truncated,
            postgres_kept=list(result.postgres_kept),
            mongo_cleared=result.mongo_cleared,
            redis_flushed=result.redis_flushed,
        )
        return result

    async def _clear_postgres(self) -> tuple[int, list[str]]:
        if self._c.pg_engine is None:
            return 0, []

        import kurosoden.shared.models  # noqa: F401 - registers Kuro Soden ORM tables
        from nekofetch.infrastructure.database.postgres.models import Base

        wanted = [t.name for t in Base.metadata.sorted_tables if t.name not in KEEP_TABLES]
        async with self._c.pg_engine.begin() as conn:
            rows = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            existing = {r[0] for r in rows}
            tables = [name for name in wanted if name in existing]
            missing = [name for name in wanted if name not in existing]
            if tables:
                await conn.execute(
                    text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
                )
        return len(tables), missing

    async def _clear_mongo(self) -> int:
        if self._c.mongo is None:
            return 0
        names = await self._c.mongo.list_collection_names()
        for name in names:
            await self._c.mongo[name].delete_many({})
        return len(names)

    async def _clear_redis(self) -> bool:
        if self._c.redis is None:
            return False
        await self._c.redis.flushdb()
        return True

    async def _stop_download_worker(self) -> None:
        """Stop the download worker before clearing, if a PipelineManager is wired."""
        pm = getattr(self._c, "pipeline_manager", None)
        if pm is not None:
            try:
                await pm.stop_download_worker()
            except Exception as exc:  # noqa: BLE001
                log.warning("database.clear.worker_stop_failed", error=str(exc))

    async def _restart_download_worker(self) -> None:
        """Restart the download worker after clearing, if a PipelineManager is wired."""
        pm = getattr(self._c, "pipeline_manager", None)
        if pm is not None:
            try:
                await pm.restart_download_worker()
            except Exception as exc:  # noqa: BLE001
                log.warning("database.clear.worker_restart_failed", error=str(exc))
