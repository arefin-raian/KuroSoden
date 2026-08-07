"""Redis-backed live progress store.

Download/processing progress is written here frequently and read by the UI layer to
render live dashboards via message edits, avoiding hot writes to Postgres.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from nekofetch.core.constants import REDIS_PROGRESS


@dataclass(slots=True)
class ProgressSnapshot:
    job_id: int
    status: str
    progress: float = 0.0          # 0..100
    speed_bps: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    current_episode: int | None = None
    eta_seconds: int | None = None
    label: str | None = None
    stage: str | None = None       # human stage: Downloading / Compressing / Muxing …
    resolution: str | None = None  # e.g. "1080p" — the variant currently in flight
    audio: str | None = None       # "Sub" / "Dub" / "Dual"
    season: int | None = None
    season_part: int | None = None     # split-season part (P1/P2), if any
    episode_index: int | None = None   # n-th of total_episodes
    total_episodes: int | None = None
    retry_attempt: int = 0             # 0 = first try; >0 = which retry is in flight
    retry_max: int = 0                 # total attempts configured (retry_attempts)
    retry_reason: str | None = None    # human reason for the last failure (from _classify)
    low_disk: bool = False             # True when chunker fell back to one-episode-at-a-time


class ProgressStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def set(self, snap: ProgressSnapshot, ttl: int = 3600) -> None:
        key = REDIS_PROGRESS.format(job_id=snap.job_id)
        await self._redis.set(key, json.dumps(asdict(snap)), ex=ttl)

    async def get(self, job_id: int) -> ProgressSnapshot | None:
        raw = await self._redis.get(REDIS_PROGRESS.format(job_id=job_id))
        if not raw:
            return None
        return ProgressSnapshot(**json.loads(raw))

    async def delete(self, job_id: int) -> None:
        """Drop a job's live-progress snapshot — used when a job is cancelled or
        recovered at startup so ACTIVE TASKS stops showing a phantom download."""
        await self._redis.delete(REDIS_PROGRESS.format(job_id=job_id))
