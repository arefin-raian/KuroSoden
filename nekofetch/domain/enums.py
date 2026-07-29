"""Domain enumerations. Pure values — no framework imports."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    USER = "user"
    STAFF = "staff"
    ADMIN = "admin"


class Permission(StrEnum):
    # Public
    SEARCH = "search"
    SUBMIT_REQUEST = "submit_request"
    VIEW_OWN_REQUESTS = "view_own_requests"
    # Staff
    REVIEW_REQUESTS = "review_requests"
    QUEUE_DOWNLOADS = "queue_downloads"
    UPLOAD_CONTENT = "upload_content"
    MANAGE_METADATA = "manage_metadata"
    # Admin
    MANAGE_STAFF = "manage_staff"
    APPROVE_USERS = "approve_users"
    MANAGE_QUEUE = "manage_queue"
    GENERATE_BOTS = "generate_bots"
    VIEW_ANALYTICS = "view_analytics"
    MANAGE_STORAGE = "manage_storage"
    CONFIGURE = "configure"


# Role -> granted permissions. Higher roles inherit lower-role permissions.
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.USER: {
        Permission.SEARCH,
        Permission.SUBMIT_REQUEST,
        Permission.VIEW_OWN_REQUESTS,
    },
}
ROLE_PERMISSIONS[Role.STAFF] = ROLE_PERMISSIONS[Role.USER] | {
    Permission.REVIEW_REQUESTS,
    Permission.QUEUE_DOWNLOADS,
    Permission.UPLOAD_CONTENT,
    Permission.MANAGE_METADATA,
}
ROLE_PERMISSIONS[Role.ADMIN] = ROLE_PERMISSIONS[Role.STAFF] | set(Permission)


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"          # awaiting publish approval
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingStage(StrEnum):
    VERIFY = "verify"
    RENAME = "rename"
    METADATA = "metadata"
    BRANDING = "branding"
    THUMBNAIL = "thumbnail"
    ENCODE = "encode"
    STORE = "store"
    PUBLISH = "publish"


class DownloadScope(StrEnum):
    ENTIRE_SERIES = "entire_series"
    SELECTED_EPISODES = "selected_episodes"
    SEASON = "season"


class AudioType(StrEnum):
    SUBBED = "subbed"
    DUBBED = "dubbed"
    DUAL_AUDIO = "dual_audio"
    MULTI = "multi"


class BotKind(StrEnum):
    ADMIN = "admin"
    DISTRIBUTION = "distribution"
    CHANNEL = "channel"


class ContentKind(StrEnum):
    SEASON = "season"
    MOVIE = "movie"
    SPECIAL = "special"


class ShiftStatus(StrEnum):
    """Duty rotation status for the admin shift system."""
    AVAILABLE = "available"            # No one on duty
    ACTIVE = "active"                  # Worker is on duty
    RELIEF_SOUGHT = "relief_sought"    # Worker wants someone to take over
    TAKEOVER_PENDING = "takeover_pending"  # Another admin requested takeover
