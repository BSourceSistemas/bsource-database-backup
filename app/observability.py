"""Structured logging and Prometheus metrics."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from .config import BackupConfig

BACKUP_NOTIFICATIONS = Counter(
    "backup_notifications_total", "Notification outcomes after retries", ["provider", "status"]
)
BACKUP_RUNS = Counter("backup_runs_total", "Backup executions", ["database_type", "status"])
BACKUP_FAILURES = Counter("backup_failures_total", "Backup failures", ["database_type", "stage"])
BACKUP_DURATION = Histogram("backup_duration_seconds", "Backup duration", ["database_type"])
BACKUP_FILE_SIZE = Gauge("backup_file_size_bytes", "Latest backup size", ["database_type"])
BACKUP_LAST_ATTEMPT = Gauge("backup_last_attempt_timestamp", "Latest backup attempt", ["database_type"])
BACKUP_LAST_SUCCESS = Gauge("backup_last_success_timestamp", "Latest successful backup", ["database_type"])
BACKUP_CONSECUTIVE_FAILURES = Gauge("backup_consecutive_failures", "Consecutive failures", ["database_type"])
BACKUP_RUNNING = Gauge("backup_running", "Whether a backup is running", ["database_type"])
BACKUP_UPLOAD_DURATION = Histogram("backup_upload_duration_seconds", "Upload duration", ["storage_type"])
SCHEDULER_MISFIRES = Counter("backup_scheduler_missed_runs_total", "Scheduler misfires")
_seq_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "BackupRunId",
            "Application",
            "Environment",
            "DatabaseType",
            "DatabaseName",
            "Stage",
            "StorageType",
            "DurationMs",
            "FileSizeBytes",
            "ObjectKey",
            "Success",
            "ErrorType",
            "ExitCode",
            "ConsecutiveFailures",
            "Event",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(config: BackupConfig | None = None) -> logging.Logger:
    global _seq_configured
    root = logging.getLogger()
    # Test runners and application servers may replace stdout. Rebind our only
    # console handler so it never retains a closed capture stream.
    for existing in list(root.handlers):
        if getattr(existing, "_bsource_handler", False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._bsource_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if config and config.seq_url and not _seq_configured:
        import seqlog

        seqlog.log_to_seq(
            server_url=config.seq_url,
            api_key=config.seq_api_key,
            level=logging.INFO,
            batch_size=10,
            auto_flush_timeout=1,
            override_root_logger=False,
        )
        seqlog.set_global_log_properties(
            Application="BSource.DbBackup",
            Environment=config.environment,
        )
        _seq_configured = True
    return logging.getLogger("bsource.backup")
