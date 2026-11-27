from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import create_api
from app.executor import BackupExecutor
from app.state_store import BackupStateStore

from .test_executor import FakeDumper, FakeStorage, RecordingNotifier


def test_operational_endpoints_do_not_return_secrets(config_factory):
    config = config_factory(run_mode="scheduler", run_on_start=False, max_backup_age_seconds=3600)
    executor = BackupExecutor(
        config,
        dumper=FakeDumper(config),
        storage=FakeStorage(),
        notifier=RecordingNotifier(),
        state_store=BackupStateStore(config.state_file),
    )
    with TestClient(create_api(config, executor)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["status"] == "degraded"
        combined = status.text + client.get("/metrics").text
        assert config.password not in combined


def test_notification_failure_does_not_degrade_backup_health(config_factory):
    config = config_factory(run_mode="scheduler", run_on_start=False, notify_on_success=True)
    executor = BackupExecutor(
        config,
        dumper=FakeDumper(config),
        storage=FakeStorage(),
        notifier=RecordingNotifier(fail=True),
    )
    with TestClient(create_api(config, executor)) as client:
        executor.run()
        status = client.get("/status").json()
        assert status["status"] == "healthy"
        assert status["notificationStatus"] == "failed"
        assert status["notificationErrorType"] == "NotificationFailed"
        assert status["consecutiveFailures"] == 0
        assert client.get("/health/ready").status_code == 200
        assert "backup_notifications_total" in client.get("/metrics").text


def test_status_accepts_legacy_state_without_notification_fields(config_factory):
    import json

    config = config_factory(run_on_start=False)
    config.state_file.write_text(json.dumps({"latestRun": {"status": "success"}}))
    executor = BackupExecutor(config, dumper=FakeDumper(config), storage=FakeStorage())
    with TestClient(create_api(config, executor)) as client:
        status = client.get("/status").json()
        assert status["notificationStatus"] == "not-requested"
        assert status["notificationErrorType"] is None
