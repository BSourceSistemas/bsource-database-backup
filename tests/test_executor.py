from __future__ import annotations

from app.db_dumper import DatabaseDumper
from app.errors import BackupError
from app.executor import BackupExecutor
from app.models import NotificationStatus
from app.notifier import Notifier
from app.state_store import BackupStateStore
from app.storage_provider import StorageProvider


class FakeDumper(DatabaseDumper):
    database_type = "postgresql"
    extension = ".dump"

    def __init__(self, config, fail_stage=None):
        super().__init__(config, runner=None)  # type: ignore[arg-type]
        self.fail_stage = fail_stage

    def check_client_version(self):
        if self.fail_stage == "preflight":
            raise BackupError("preflight", "BadClient", "bad client")
        return "17"

    def check_connection(self, workdir):
        if self.fail_stage == "connection":
            raise BackupError("connection", "BadConnection", "bad connection")
        return "17"

    def create_dump(self, output_path, workdir, run_id):
        if self.fail_stage == "dump":
            raise BackupError("dump", "DumpFailed", "dump failed")
        output_path.write_bytes(b"backup-data")

    def validate_dump(self, output_path, workdir, run_id):
        if self.fail_stage == "validation":
            raise BackupError("validation", "InvalidDump", "invalid dump")
        self._require_file(output_path)

    def get_restore_instructions(self, object_path):
        return "restore"


class FakeStorage(StorageProvider):
    storage_type = "local"

    def __init__(self, fail_stage=None):
        self.fail_stage = fail_stage
        self.objects = {}
        self.upload_attempts = 0

    def check_connection(self):
        return

    def upload(self, local_path, object_key, metadata):
        self.upload_attempts += 1
        if self.fail_stage == "upload":
            raise BackupError("upload", "UploadFailed", "upload failed")
        self.objects[object_key] = (local_path.read_bytes(), dict(metadata))
        return object_key

    def get_object_metadata(self, object_key):
        if self.fail_stage == "upload-verification":
            return {"size": 0, "sha256": "bad"}
        content, metadata = self.objects[object_key]
        return {"size": len(content), "sha256": metadata["sha256"]}

    def download(self, object_key, destination):
        destination.write_bytes(self.objects[object_key][0])
        return destination

    def delete(self, object_key):
        self.objects.pop(object_key, None)


class RecordingNotifier(Notifier):
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def send(self, event, payload):
        self.events.append((event, payload))
        if self.fail:
            raise RuntimeError("notification failed")


def executor(config, dumper, storage, notifier=None):
    return BackupExecutor(
        config,
        dumper=dumper,
        storage=storage,
        notifier=notifier or RecordingNotifier(),
        state_store=BackupStateStore(config.state_file),
    )


def test_successful_pipeline_uploads_and_verifies(config_factory):
    config = config_factory(notify_on_success=True)
    notifier = RecordingNotifier()
    storage = FakeStorage()
    result = executor(config, FakeDumper(config), storage, notifier).run()
    assert result.success
    assert result.checksum
    assert result.object_key.endswith(".dump")
    assert len(storage.objects) == 1
    assert notifier.events[0][0] == "backup-success"
    assert not any(config.temp_root.iterdir())


def test_failure_preserves_correct_stage(config_factory):
    config = config_factory()
    result = executor(config, FakeDumper(config, "validation"), FakeStorage()).run()
    assert not result.success
    assert result.error["stage"] == "validation"
    assert result.error["errorType"] == "InvalidDump"


def test_upload_is_retried_three_times(config_factory, monkeypatch):
    monkeypatch.setattr("app.executor.time.sleep", lambda _value: None)
    config = config_factory()
    storage = FakeStorage("upload")
    result = executor(config, FakeDumper(config), storage).run()
    assert not result.success
    assert storage.upload_attempts == 3


def test_notification_failure_does_not_fail_backup(config_factory):
    config = config_factory(notify_on_success=True)
    result = executor(config, FakeDumper(config), FakeStorage(), RecordingNotifier(fail=True)).run()
    assert result.success
    latest = BackupStateStore(config.state_file).snapshot()["latestRun"]
    assert latest["notification_status"] == NotificationStatus.FAILED.value


def test_database_name_is_encoded_in_object_key(config_factory):
    config = config_factory(database="Case/Sensitive Database")
    result = executor(config, FakeDumper(config), FakeStorage()).run()
    assert result.success
    assert "Case%2FSensitive%20Database" in result.object_key
