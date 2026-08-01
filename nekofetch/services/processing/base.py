"""Processing-stage abstraction.

Each pipeline stage is a small, independently-toggleable unit operating on a shared
:class:`StageContext`. Stages should be idempotent so a job can be reprocessed safely.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from nekofetch.core.container import Container
from nekofetch.domain.enums import ProcessingStage
from nekofetch.infrastructure.database.postgres.models import MediaFile, Request


@dataclass
class StageContext:
    job_id: int
    request: Request
    files: list[MediaFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Stage(ABC):
    stage: ProcessingStage

    def __init__(self, container: Container) -> None:
        self.c = container

    @property
    def log_name(self) -> str:
        """Human/telemetry label for this stage. Defaults to the enum value, but
        stages that SHARE a :class:`ProcessingStage` (e.g. Branding + Watermark
        both map to ``BRANDING``) override it so the activity log and the live
        card don't show the same word twice for two different operations."""
        return self.stage.value

    @abstractmethod
    def enabled(self) -> bool:
        """Whether this stage runs, per configuration toggles."""

    @abstractmethod
    async def process(self, ctx: StageContext) -> None:
        """Mutate the context / files in place. Raise ProcessingError on failure."""
