"""Public domain models for runs, stage outcomes, and results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NotificationStatus(str, Enum):
    NOT_REQUESTED = "not-requested"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(slots=True)
class StageResult:
    stage: str
    started_at: datetime
    duration_ms: int
    success: bool
    exit_code: int | None = None
    error_type: str | None = None
    sanitized_message: str | None = None


@dataclass(slots=True)
class BackupRun:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.PENDING
    stage: str = "configuration"
    duration_ms: int | None = None
    file_size_bytes: int | None = None
    checksum: str | None = None
    object_key: str | None = None
    error_type: str | None = None
    sanitized_error: str | None = None
    notification_status: NotificationStatus = NotificationStatus.NOT_REQUESTED
    stages: list[StageResult] = field(default_factory=list)

    def finish(self, status: RunStatus) -> None:
        self.status = status
        self.finished_at = utcnow()
        self.duration_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        def normalize(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat().replace("+00:00", "Z")
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            return value

        return cast(dict[str, Any], normalize(asdict(self)))


@dataclass(slots=True)
class BackupResult:
    success: bool
    run_id: str
    output_path: str | None = None
    object_key: str | None = None
    checksum: str | None = None
    file_size_bytes: int | None = None
    duration_ms: int | None = None
    error: dict[str, Any] | None = None
