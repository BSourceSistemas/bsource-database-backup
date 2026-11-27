from __future__ import annotations

import json
import logging

from app import cli
from app.errors import BackupError
from app.models import BackupResult
from app.notifier import EmailNotifier, NoOpNotifier, RetryingNotifier, WebhookNotifier, create_notifier
from app.observability import JsonFormatter, configure_logging


def test_cli_config_and_status(config_factory, monkeypatch, capsys):
    config = config_factory()
    monkeypatch.setattr(cli, "load_config", lambda: config)
    assert cli.main(["config", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    assert cli.main(["status", "--json"]) == 0
    assert "consecutiveFailures" in json.loads(capsys.readouterr().out)


def test_cli_invalid_configuration(monkeypatch, capsys):
    def fail():
        raise BackupError("configuration", "InvalidConfiguration", "invalid")

    monkeypatch.setattr(cli, "load_config", fail)
    assert cli.main(["config", "check"]) == 1
    assert json.loads(capsys.readouterr().out)["errorType"] == "InvalidConfiguration"


def test_cli_database_and_storage_checks(config_factory, monkeypatch, capsys):
    config = config_factory()
    monkeypatch.setattr(cli, "load_config", lambda: config)

    class Dumper:
        def check_client_version(self):
            return "17"

        def check_connection(self, _workdir):
            return "17"

    monkeypatch.setattr(cli, "create_dumper", lambda _config, _runner: Dumper())
    assert cli.main(["database", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["serverVersion"] == "17"

    class Storage:
        def check_connection(self):
            return None

    monkeypatch.setattr(cli, "create_storage", lambda _config: Storage())
    assert cli.main(["storage", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_run_success_and_failure(config_factory, monkeypatch, capsys):
    config = config_factory()
    monkeypatch.setattr(cli, "load_config", lambda: config)

    class SuccessfulExecutor:
        def __init__(self, _config):
            pass

        def run(self):
            return BackupResult(True, "run-id", object_key="key", checksum="sum", file_size_bytes=10, duration_ms=2)

    monkeypatch.setattr(cli, "BackupExecutor", SuccessfulExecutor)
    assert cli.main(["run"]) == 0
    assert json.loads(capsys.readouterr().out)["success"] is True

    class FailedExecutor(SuccessfulExecutor):
        def run(self):
            return BackupResult(False, "run-id", error={"stage": "validation"})

    monkeypatch.setattr(cli, "BackupExecutor", FailedExecutor)
    assert cli.main(["run"]) == 4
    assert json.loads(capsys.readouterr().out)["success"] is False


def test_cli_scheduler_starts_single_uvicorn_worker(config_factory, monkeypatch):
    config = config_factory(run_mode="scheduler")
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "BackupExecutor", lambda _config: object())
    monkeypatch.setattr(cli, "create_api", lambda _config, _executor: "app")
    captured = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.update({"app": app, **kwargs}))
    assert cli.main(["scheduler"]) == 0
    assert captured["workers"] == 1


class RecordingProvider:
    def __init__(self, fail_count=0):
        self.fail_count = fail_count
        self.calls = 0

    def send(self, event, payload):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError("temporary")


def test_retrying_notifier_retries(monkeypatch):
    provider = RecordingProvider(fail_count=2)
    monkeypatch.setattr("app.notifier.time.sleep", lambda _seconds: None)
    RetryingNotifier(provider, attempts=3).send("event", {})  # type: ignore[arg-type]
    assert provider.calls == 3


def test_email_notifier_supports_multiple_recipients(config_factory, monkeypatch):
    config = config_factory(
        notification_provider="email",
        email_from="from@example.com",
        email_to=("one@example.com", "two@example.com"),
        email_smtp="smtp.example.com",
        email_user="user",
        email_password="mail-secret",
    )
    sent = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self, *, context):
            return None

        def login(self, user, password):
            assert password == "mail-secret"

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("app.notifier.smtplib.SMTP", SMTP)
    EmailNotifier(config).send("backup-success", {"runId": "id", "error": "must-not-be-sent"})
    assert sent[0]["To"] == "one@example.com, two@example.com"
    assert "must-not-be-sent" not in sent[0].get_content()


def test_webhook_notifier_posts_json(monkeypatch):
    captured = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def urlopen(request, timeout):
        captured["data"] = request.data
        return Response()

    monkeypatch.setattr("app.notifier.urllib.request.urlopen", urlopen)
    WebhookNotifier("https://example.com/hook").send("backup-failure", {"runId": "id"})
    assert json.loads(captured["data"])["event"] == "backup-failure"


def test_notifier_factory_and_json_logging(config_factory):
    assert isinstance(create_notifier(config_factory(notification_provider="noop")), NoOpNotifier)
    logger = configure_logging()
    assert logger.name == "bsource.backup"
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    record.BackupRunId = "id"  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter().format(record))["BackupRunId"] == "id"


def test_cli_reports_notification_failure_with_success_exit(config_factory, monkeypatch, capsys):
    from app.models import NotificationStatus

    monkeypatch.setattr(cli, "load_config", lambda: config_factory())

    class Executor:
        def __init__(self, _config):
            pass

        def run(self):
            return BackupResult(
                True,
                "run-id",
                notification_status=NotificationStatus.FAILED,
                notification_error_type="NotificationTLSFailed",
            )

    monkeypatch.setattr(cli, "BackupExecutor", Executor)
    assert cli.main(["run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["success"] is True
    assert output["error"] is None
    assert output["notificationStatus"] == "failed"
    assert output["notificationErrorType"] == "NotificationTLSFailed"
