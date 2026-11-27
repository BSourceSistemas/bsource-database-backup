"""Single backup pipeline shared by CLI and scheduler modes."""

from __future__ import annotations

import logging
import random
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from .command_runner import CommandRunner, sanitize_text
from .config import BackupConfig
from .db_dumper import DatabaseDumper, create_dumper
from .errors import BackupError, stage_exit_code
from .models import BackupResult, BackupRun, NotificationStatus, RunStatus, StageResult, utcnow
from .notifier import NoOpNotifier, Notifier, create_notifier
from .observability import (
    BACKUP_CONSECUTIVE_FAILURES,
    BACKUP_DURATION,
    BACKUP_FAILURES,
    BACKUP_FILE_SIZE,
    BACKUP_LAST_ATTEMPT,
    BACKUP_LAST_SUCCESS,
    BACKUP_RUNNING,
    BACKUP_RUNS,
    BACKUP_UPLOAD_DURATION,
)
from .state_store import BackupStateStore
from .storage_provider import StorageProvider, create_storage, sha256_file

T = TypeVar("T")


class BackupExecutor:
    def __init__(
        self,
        config: BackupConfig,
        *,
        dumper: DatabaseDumper | None = None,
        storage: StorageProvider | None = None,
        state_store: BackupStateStore | None = None,
        notifier: Notifier | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.runner = CommandRunner(config.secrets)
        self.dumper = dumper or create_dumper(config, self.runner)
        self.storage = storage or create_storage(config)
        self.state = state_store or BackupStateStore(config.state_file)
        self.notifier = notifier or create_notifier(config)
        self.log = logger or logging.getLogger("bsource.backup")
        self._cancelled = threading.Event()
        self._run_lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled.set()
        self.runner.cancel()

    def preflight(self) -> dict[str, str]:
        self.config.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="check-", dir=self.config.temp_root) as name:
            workdir = Path(name)
            workdir.chmod(0o700)
            client = self.dumper.check_client_version()
            server = self.dumper.check_connection(workdir)
            self.storage.check_connection()
            return {"clientVersion": client, "serverVersion": server}

    def run(self) -> BackupResult:
        if not self._run_lock.acquire(blocking=False):
            error = BackupError("preflight", "BackupAlreadyRunning", "Another backup is already running")
            return BackupResult(False, "", error=self._error_payload(error))
        run = BackupRun(status=RunStatus.RUNNING)
        previous_failures = self.state.consecutive_failures
        self.state.record_started(run)
        BACKUP_RUNNING.labels(self.dumper.database_type).set(1)
        BACKUP_LAST_ATTEMPT.labels(self.dumper.database_type).set(run.started_at.timestamp())
        self._event("BackupStarted", run, Success=True)
        output_path: Path | None = None
        workdir: Path | None = None
        failure: BackupError | None = None
        try:
            self.config.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            workdir = Path(tempfile.mkdtemp(prefix=f"backup-{run.run_id}-", dir=self.config.temp_root))
            workdir.chmod(0o700)
            self._stage(run, "preflight", self.dumper.check_client_version)
            self._stage(run, "connection", lambda: self.dumper.check_connection(workdir))
            self._stage(run, "preflight", self.storage.check_connection)
            output_path = self.dumper.output_path(workdir, run.run_id)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._stage(run, "dump", lambda: self.dumper.create_dump(output_path, workdir, run.run_id))
            self._stage(run, "validation", lambda: self.dumper.validate_dump(output_path, workdir, run.run_id))
            run.file_size_bytes = output_path.stat().st_size
            run.checksum = sha256_file(output_path)
            run.object_key = self._object_key(run)
            metadata = {
                "sha256": run.checksum,
                "size": str(run.file_size_bytes),
                "run-id": run.run_id,
                "database-type": self.dumper.database_type,
            }
            upload_started = time.monotonic()
            self._stage(
                run,
                "upload",
                lambda: self._retry(lambda: self.storage.upload(output_path, run.object_key or "", metadata), "upload"),
            )
            BACKUP_UPLOAD_DURATION.labels(self.storage.storage_type).observe(time.monotonic() - upload_started)
            self._stage(
                run,
                "upload-verification",
                lambda: self._retry(
                    lambda: self.storage.verify(run.object_key or "", run.file_size_bytes or 0, run.checksum or ""),
                    "upload-verification",
                ),
            )
            run.status = RunStatus.SUCCESS
        except BackupError as exc:
            failure = exc
            run.status = (
                RunStatus.CANCELLED if exc.error_type in {"Cancelled", "CommandCancelled"} else RunStatus.FAILED
            )
            run.error_type = exc.error_type
            run.sanitized_error = sanitize_text(exc.sanitized_message, self.config.secrets)
        except Exception as exc:
            failure = BackupError(
                run.stage,
                "UnexpectedError",
                sanitize_text(str(exc), self.config.secrets) or "Unexpected backup error",
                cause=exc,
            )
            run.status = RunStatus.FAILED
            run.error_type = failure.error_type
            run.sanitized_error = failure.sanitized_message
        finally:
            self._notify(run, previous_failures)
            cleanup_started = utcnow()
            cleanup_error: str | None = None
            try:
                if output_path is not None and output_path.exists():
                    output_path.unlink()
                if workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=False)
            except OSError as exc:
                cleanup_error = sanitize_text(str(exc), self.config.secrets)
                self._event("TemporaryFileCleanupFailed", run, ErrorType="CleanupFailed")
            run.stages.append(
                StageResult(
                    "cleanup",
                    cleanup_started,
                    int((utcnow() - cleanup_started).total_seconds() * 1000),
                    cleanup_error is None,
                    error_type="CleanupFailed" if cleanup_error else None,
                    sanitized_message=cleanup_error,
                )
            )
            run.finish(run.status)
            self.state.record_completed(run)
            self._record_metrics(run)
            self._run_lock.release()
            self._cancelled.clear()

        if run.status == RunStatus.SUCCESS:
            self._event("BackupCompleted", run, Success=True)
        else:
            self._event(
                "BackupFailed",
                run,
                Success=False,
                ErrorType=run.error_type,
                ExitCode=int(stage_exit_code(failure.stage if failure else run.stage)),
            )
        return BackupResult(
            success=run.status == RunStatus.SUCCESS,
            run_id=run.run_id,
            object_key=run.object_key,
            checksum=run.checksum,
            file_size_bytes=run.file_size_bytes,
            duration_ms=run.duration_ms,
            error=self._error_payload(failure) if failure else None,
        )

    def _stage(self, run: BackupRun, name: str, operation: Callable[[], T]) -> T:
        if self._cancelled.is_set():
            raise BackupError("cancelled", "Cancelled", "Backup execution was cancelled")
        run.stage = name
        started = utcnow()
        try:
            result = operation()
        except BackupError as exc:
            run.stages.append(
                StageResult(
                    name,
                    started,
                    int((utcnow() - started).total_seconds() * 1000),
                    False,
                    exc.exit_code,
                    exc.error_type,
                    exc.sanitized_message,
                )
            )
            self.state.update_current(run)
            raise
        run.stages.append(StageResult(name, started, int((utcnow() - started).total_seconds() * 1000), True))
        self.state.update_current(run)
        self._event(f"{name.title().replace('-', '')}Completed", run, Success=True)
        return result

    def _retry(self, operation: Callable[[], T], stage: str) -> T:
        error: BackupError | None = None
        for attempt in range(3):
            try:
                return operation()
            except BackupError as exc:
                error = exc
                if attempt < 2:
                    time.sleep((2**attempt) + random.uniform(0, 0.25))
        assert error is not None
        if error.stage != stage:
            error.stage = stage
        raise error

    def _notify(self, run: BackupRun, previous_failures: int) -> None:
        if isinstance(self.notifier, NoOpNotifier):
            run.notification_status = NotificationStatus.NOT_REQUESTED
            return
        if run.status == RunStatus.SUCCESS:
            event = "backup-recovered" if previous_failures else "backup-success"
            enabled = self.config.notify_on_recovery if previous_failures else self.config.notify_on_success
        elif run.status == RunStatus.FAILED:
            event = "backup-failure"
            enabled = self.config.notify_on_failure and previous_failures + 1 >= self.config.notify_after_failures
        else:
            run.notification_status = NotificationStatus.NOT_REQUESTED
            return
        if not enabled:
            run.notification_status = NotificationStatus.NOT_REQUESTED
            return
        if not self.state.notification_allowed(event, self.config.notification_cooldown_seconds):
            run.notification_status = NotificationStatus.SUPPRESSED
            return
        payload = {
            "runId": run.run_id,
            "environment": self.config.environment,
            "databaseType": self.dumper.database_type,
            "databaseName": self.config.database,
            "stage": run.stage,
            "occurredAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        try:
            self.notifier.send(event, payload)
            run.notification_status = NotificationStatus.SENT
            self.state.record_notification(event)
            self._event("NotificationSent", run, Success=True)
        except Exception:
            run.notification_status = NotificationStatus.FAILED
            self._event("NotificationFailed", run, Success=False, ErrorType="NotificationFailed")

    def _object_key(self, run: BackupRun) -> str:
        prefix = self.config.storage_destination_folder.strip("/")
        env = quote(self.config.environment, safe="-_.")
        database = quote(self.config.database, safe="-_.")
        timestamp = run.started_at.strftime("%Y%m%dT%H%M%SZ")
        dated = run.started_at.strftime("%Y/%m/%d")
        key = f"{env}/{self.dumper.database_type}/{database}/{dated}/{timestamp}-{run.run_id}{self.dumper.extension}"
        return f"{prefix}/{key}" if prefix else key

    @staticmethod
    def _error_payload(error: BackupError | None) -> dict[str, Any] | None:
        if error is None:
            return None
        return {
            "stage": error.stage,
            "errorType": error.error_type,
            "sanitizedMessage": error.sanitized_message,
            "exitCode": error.exit_code,
        }

    def _record_metrics(self, run: BackupRun) -> None:
        status = run.status.value
        BACKUP_RUNNING.labels(self.dumper.database_type).set(0)
        BACKUP_RUNS.labels(self.dumper.database_type, status).inc()
        BACKUP_CONSECUTIVE_FAILURES.labels(self.dumper.database_type).set(self.state.consecutive_failures)
        if run.duration_ms is not None:
            BACKUP_DURATION.labels(self.dumper.database_type).observe(run.duration_ms / 1000)
        if run.file_size_bytes is not None:
            BACKUP_FILE_SIZE.labels(self.dumper.database_type).set(run.file_size_bytes)
        if run.status == RunStatus.SUCCESS and run.finished_at:
            BACKUP_LAST_SUCCESS.labels(self.dumper.database_type).set(run.finished_at.timestamp())
        elif run.status == RunStatus.FAILED:
            BACKUP_FAILURES.labels(self.dumper.database_type, run.stage).inc()

    def _event(self, event: str, run: BackupRun, **properties: Any) -> None:
        extra = {
            "Event": event,
            "BackupRunId": run.run_id,
            "Application": "BSource.DbBackup",
            "Environment": self.config.environment,
            "DatabaseType": self.dumper.database_type,
            "DatabaseName": self.config.database,
            "Stage": run.stage,
            "StorageType": self.storage.storage_type,
            **properties,
        }
        self.log.info(event, extra=extra)
