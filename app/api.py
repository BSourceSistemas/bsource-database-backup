"""FastAPI operational endpoints and resilient scheduler lifecycle."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import BackupConfig
from .executor import BackupExecutor
from .models import NotificationStatus
from .observability import SCHEDULER_MISFIRES


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def create_api(config: BackupConfig, executor: BackupExecutor) -> FastAPI:
    scheduler = BackgroundScheduler(timezone=ZoneInfo(config.timezone))
    readiness: dict[str, bool | str | None] = {"ready": False, "error": None}

    def update_next_run(_event: Any = None) -> None:
        job = scheduler.get_job("backup")
        executor.state.set_next_run(job.next_run_time if job else None)

    def listener(event: Any) -> None:
        if event.code == EVENT_JOB_MISSED:
            SCHEDULER_MISFIRES.inc()
        update_next_run(event)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            config.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            config.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            executor.dumper.check_client_version()
            readiness["ready"] = True
        except Exception as exc:
            readiness["error"] = type(exc).__name__
            raise
        trigger = CronTrigger.from_crontab(config.schedule, timezone=ZoneInfo(config.timezone))
        scheduler.add_job(
            executor.run,
            trigger=trigger,
            id="backup",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=config.misfire_grace_time_seconds,
            jitter=config.jitter_seconds,
            replace_existing=True,
        )
        scheduler.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_MISSED)
        scheduler.start()
        executor.state.set_scheduler_active(True)
        update_next_run()
        if config.run_on_start:
            threading.Thread(target=executor.run, name="backup-run-on-start", daemon=True).start()
        try:
            yield
        finally:
            readiness["ready"] = False
            executor.state.set_scheduler_active(False)
            executor.cancel()
            scheduler.shutdown(wait=False)

    app = FastAPI(title="BSource Database Backup", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/health/live")
    def live(response: Response) -> dict[str, Any]:
        active = bool(scheduler.running and executor.state.snapshot().get("schedulerActive"))
        if not active:
            response.status_code = 503
        return {"status": "live" if active else "unavailable"}

    @app.get("/health/ready")
    def ready(response: Response) -> dict[str, Any]:
        if not readiness["ready"]:
            response.status_code = 503
        return {"status": "ready" if readiness["ready"] else "not-ready", "errorType": readiness["error"]}

    @app.get("/status")
    def status() -> dict[str, Any]:
        state = executor.state.snapshot()
        latest = state.get("latestRun") or {}
        now = datetime.now(UTC)
        last_success = _parse_timestamp(state.get("lastSuccess"))
        current = state.get("currentRun") if isinstance(state.get("currentRun"), dict) else None
        current_started = _parse_timestamp(current.get("started_at")) if current else None
        stale = last_success is None or (now - last_success).total_seconds() > config.max_backup_age_seconds
        stuck = bool(current_started and (now - current_started).total_seconds() > config.backup_timeout_seconds)
        degraded = (
            stale
            or int(state.get("consecutiveFailures", 0)) >= config.max_consecutive_failures
            or not state.get("schedulerActive")
            or stuck
        )
        return {
            "status": "degraded" if degraded else "healthy",
            "databaseType": executor.dumper.database_type,
            "databaseName": config.database,
            "lastAttempt": state.get("lastAttempt"),
            "lastSuccess": state.get("lastSuccess"),
            "lastFailure": state.get("lastFailure"),
            "nextRun": state.get("nextRun"),
            "consecutiveFailures": int(state.get("consecutiveFailures", 0)),
            "running": current is not None,
            "notificationStatus": latest.get("notification_status", NotificationStatus.NOT_REQUESTED.value),
            "notificationErrorType": latest.get("notification_error_type"),
        }

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
