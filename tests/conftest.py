from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BackupConfig


@pytest.fixture
def config_factory(tmp_path: Path):
    def factory(**overrides):
        values = {
            "database_type": "postgres",
            "host": "db host",
            "port": 5432,
            "database": "AppTreino",
            "username": "backup user",
            "password": "S3cr3t ! value",
            "run_mode": "once",
            "run_on_start": False,
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "state_file": tmp_path / "state.json",
            "temp_root": tmp_path / "temp",
            "storage_type": "local",
            "storage_local_path": tmp_path / "backups",
            "environment": "test",
            "notification_provider": "noop",
        }
        values.update(overrides)
        return BackupConfig(**values)

    return factory
