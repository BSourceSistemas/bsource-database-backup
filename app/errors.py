"""Typed, sanitized errors used by the backup pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION = 1
    CONNECTION = 2
    DUMP = 3
    VALIDATION = 4
    UPLOAD = 5
    UPLOAD_VERIFICATION = 6
    CANCELLED = 7


@dataclass(slots=True)
class BackupError(Exception):
    stage: str
    error_type: str
    sanitized_message: str
    exit_code: int | None = None
    cause: BaseException | None = None

    def __str__(self) -> str:
        return self.sanitized_message


def stage_exit_code(stage: str) -> ExitCode:
    return {
        "configuration": ExitCode.CONFIGURATION,
        "preflight": ExitCode.CONNECTION,
        "connection": ExitCode.CONNECTION,
        "dump": ExitCode.DUMP,
        "validation": ExitCode.VALIDATION,
        "upload": ExitCode.UPLOAD,
        "upload-verification": ExitCode.UPLOAD_VERIFICATION,
        "cancelled": ExitCode.CANCELLED,
    }.get(stage, ExitCode.DUMP)
