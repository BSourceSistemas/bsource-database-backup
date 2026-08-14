from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import BackupError
from app.models import BackupRun, RunStatus
from app.state_store import BackupStateStore
from app.storage_provider import LocalStorageProvider, sha256_file


def test_local_upload_and_verification(tmp_path: Path):
    source = tmp_path / "source.dump"
    source.write_bytes(b"valid backup")
    storage = LocalStorageProvider(tmp_path / "storage")
    storage.check_connection()
    storage.upload(source, "prod/postgres/db/object.dump", {"sha256": sha256_file(source)})
    storage.verify("prod/postgres/db/object.dump", source.stat().st_size, sha256_file(source))


def test_local_verification_detects_wrong_checksum(tmp_path: Path):
    source = tmp_path / "source.dump"
    source.write_bytes(b"valid backup")
    storage = LocalStorageProvider(tmp_path / "storage")
    storage.upload(source, "object.dump", {})
    with pytest.raises(BackupError) as error:
        storage.verify("object.dump", source.stat().st_size, "wrong")
    assert error.value.error_type == "ObjectChecksumMismatch"


def test_state_is_atomic_and_tracks_failures(tmp_path: Path):
    store = BackupStateStore(tmp_path / "state.json")
    run = BackupRun(status=RunStatus.RUNNING)
    store.record_started(run)
    run.finish(RunStatus.FAILED)
    store.record_completed(run)
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["consecutiveFailures"] == 1
    assert persisted["currentRun"] is None


def test_interrupted_run_is_recovered_as_failure(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"currentRun": {"status": "running"}, "consecutiveFailures": 0}), encoding="utf-8")
    snapshot = BackupStateStore(path).snapshot()
    assert snapshot["currentRun"] is None
    assert snapshot["latestRun"]["error_type"] == "InterruptedRun"
    assert snapshot["consecutiveFailures"] == 1
