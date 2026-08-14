from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config
from app.errors import BackupError


def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DB_TYPE": "postgres",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_USER": "backup",
        "DB_PASSWORD": "secret",
        "DB_DATABASE": "AppTreino",
        "STORAGE_TYPE": "local",
        "STORAGE_LOCAL_PATH": str(tmp_path / "backups"),
        "STATE_FILE": str(tmp_path / "state.json"),
        "BACKUP_TEMP_ROOT": str(tmp_path / "temp"),
        "BACKUP_TIMEZONE": "UTC",
    }


def test_preserves_database_case_and_current_cron_alias(tmp_path: Path):
    env = base_env(tmp_path)
    env["CRON_SCHEDULE"] = "1 3 * * *"
    config = load_config(env)
    assert config.database == "AppTreino"
    assert config.schedule == "1 3 * * *"


def test_new_schedule_variables_take_precedence(tmp_path: Path):
    env = base_env(tmp_path)
    env.update({"CRON_SCHEDULE": "1 3 * * *", "BACKUP_CRON": "2 4 * * *", "TIMEZONE": "Europe/London"})
    config = load_config(env)
    assert config.schedule == "2 4 * * *"
    assert config.timezone == "UTC"


def test_secret_file_is_supported(tmp_path: Path):
    env = base_env(tmp_path)
    env.pop("DB_PASSWORD")
    secret = tmp_path / "db-password"
    secret.write_text("from-file\n", encoding="utf-8")
    env["DB_PASSWORD_FILE"] = str(secret)
    assert load_config(env).password == "from-file"


def test_direct_and_file_secret_is_rejected(tmp_path: Path):
    env = base_env(tmp_path)
    secret = tmp_path / "db-password"
    secret.write_text("from-file", encoding="utf-8")
    env["DB_PASSWORD_FILE"] = str(secret)
    with pytest.raises(BackupError, match="Configure only one"):
        load_config(env)


def test_invalid_cron_fails_configuration(tmp_path: Path):
    env = base_env(tmp_path)
    env["BACKUP_CRON"] = "not cron"
    with pytest.raises(BackupError) as error:
        load_config(env)
    assert error.value.error_type == "InvalidConfiguration"


def test_mssql_requires_shared_volume(tmp_path: Path):
    env = base_env(tmp_path)
    env.update({"DB_TYPE": "mssql", "DB_PORT": "1433"})
    with pytest.raises(BackupError, match="shared-volume"):
        load_config(env)


def test_mssql_shared_volume_configuration(tmp_path: Path):
    env = base_env(tmp_path)
    env.update(
        {
            "DB_TYPE": "mssql",
            "DB_PORT": "1433",
            "MSSQL_BACKUP_MODE": "shared-volume",
            "MSSQL_SERVER_BACKUP_DIR": "/var/opt/mssql/backup",
            "MSSQL_LOCAL_BACKUP_DIR": str(tmp_path / "mssql"),
        }
    )
    config = load_config(env)
    assert config.mssql_backup_mode == "shared-volume"
