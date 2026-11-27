"""Atomic local operational state store."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BackupRun, RunStatus


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class BackupStateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "lastAttempt": None,
            "lastSuccess": None,
            "lastFailure": None,
            "consecutiveFailures": 0,
            "nextRun": None,
            "currentRun": None,
            "latestRun": None,
            "schedulerActive": False,
            "lastNotificationAt": None,
            "lastNotificationEvent": None,
        }
        self._load()
        self._recover_interrupted_run()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._state.update(loaded)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not prevent a backup. Preserve it for diagnosis.
            return

    def _recover_interrupted_run(self) -> None:
        current = self._state.get("currentRun")
        if not isinstance(current, dict) or current.get("status") != RunStatus.RUNNING.value:
            return
        current.update(
            {
                "status": RunStatus.FAILED.value,
                "stage": "cancelled",
                "finished_at": _timestamp(),
                "error_type": "InterruptedRun",
                "sanitized_error": "A previously running backup was interrupted without graceful shutdown",
            }
        )
        self._state["latestRun"] = current
        self._state["currentRun"] = None
        self._state["lastFailure"] = _timestamp()
        self._state["consecutiveFailures"] = int(self._state.get("consecutiveFailures", 0)) + 1
        self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, filename = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._state, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            Path(filename).chmod(0o600)
            os.replace(filename, self.path)
        finally:
            Path(filename).unlink(missing_ok=True)

    def record_started(self, run: BackupRun) -> None:
        with self._lock:
            self._state["lastAttempt"] = run.started_at.isoformat().replace("+00:00", "Z")
            self._state["currentRun"] = run.to_dict()
            self._persist()

    def update_current(self, run: BackupRun) -> None:
        with self._lock:
            self._state["currentRun"] = run.to_dict()
            self._persist()

    def record_completed(self, run: BackupRun) -> None:
        with self._lock:
            payload = run.to_dict()
            self._state["currentRun"] = None
            self._state["latestRun"] = payload
            if run.status == RunStatus.SUCCESS:
                self._state["lastSuccess"] = payload["finished_at"]
                self._state["consecutiveFailures"] = 0
            elif run.status == RunStatus.FAILED:
                self._state["lastFailure"] = payload["finished_at"]
                self._state["consecutiveFailures"] = int(self._state.get("consecutiveFailures", 0)) + 1
            self._persist()

    def set_next_run(self, value: datetime | None) -> None:
        with self._lock:
            self._state["nextRun"] = value.isoformat().replace("+00:00", "Z") if value else None
            self._persist()

    def set_scheduler_active(self, active: bool) -> None:
        with self._lock:
            self._state["schedulerActive"] = active
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return int(self._state.get("consecutiveFailures", 0))

    def notification_allowed(self, event: str, cooldown_seconds: int) -> bool:
        with self._lock:
            if self._state.get("lastNotificationEvent") != event:
                return True
            raw = self._state.get("lastNotificationAt")
            if not raw:
                return True
            try:
                last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                return True
            return (datetime.now(UTC) - last).total_seconds() >= cooldown_seconds

    def record_notification(self, event: str) -> None:
        with self._lock:
            self._state["lastNotificationAt"] = _timestamp()
            self._state["lastNotificationEvent"] = event
            self._persist()
