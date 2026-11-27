from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.command_runner import CommandResult
from app.db_dumper import MSSQLDumper, MySQLDumper, PostgreSQLDumper
from app.errors import BackupError


class RecordingRunner:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = iter(outputs or [])

    def run(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        output = next(self.outputs, ("", ""))
        return CommandResult(tuple(args), 0, output[0], output[1], 1)


def test_postgres_uses_arguments_and_scoped_password(config_factory, tmp_path: Path):
    runner = RecordingRunner()
    dumper = PostgreSQLDumper(config_factory(), runner)
    dumper.create_dump(tmp_path / "backup.dump", tmp_path, "run")
    args, kwargs = runner.calls[0]
    assert args[0] == "pg_dump"
    assert "db host" in args
    assert "S3cr3t ! value" not in args
    assert kwargs["env"] == {"PGPASSWORD": "S3cr3t ! value"}


def test_mysql_uses_restricted_option_file(config_factory, tmp_path: Path):
    runner = RecordingRunner()
    dumper = MySQLDumper(config_factory(database_type="mysql", port=3306), runner)
    dumper.create_dump(tmp_path / "backup.sql", tmp_path, "run")
    args, _ = runner.calls[0]
    option_path = Path(args[1].split("=", 1)[1])
    if os.name != "nt":
        assert option_path.stat().st_mode & 0o777 == 0o600
    assert "S3cr3t ! value" not in args


def test_mssql_uses_unique_shared_file_and_sqlcmdpassword(config_factory, tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = config_factory(
        database_type="mssql",
        port=1433,
        database="Db]Name",
        mssql_backup_mode="shared-volume",
        mssql_server_backup_dir="/var/opt/mssql/backup",
        mssql_local_backup_dir=shared,
    )

    class MSSQLRunner(RecordingRunner):
        def run(self, args, **kwargs):
            self.calls.append((list(args), kwargs))
            (shared / "run-id.bak").write_bytes(b"BAK")
            return CommandResult(tuple(args), 0, "", "", 1)

    runner = MSSQLRunner()
    dumper = MSSQLDumper(config, runner)
    output = dumper.output_path(tmp_path, "run-id")
    dumper.create_dump(output, tmp_path, "run-id")
    args, kwargs = runner.calls[0]
    query = args[-1]
    assert "[Db]]Name]" in query
    assert "/var/opt/mssql/backup/run-id.bak" in query
    assert config.password not in args
    assert kwargs["env"] == {"SQLCMDPASSWORD": config.password}


def test_mssql_rejects_invisible_server_file(config_factory, tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = config_factory(
        database_type="mssql",
        port=1433,
        mssql_backup_mode="shared-volume",
        mssql_server_backup_dir="/server",
        mssql_local_backup_dir=shared,
    )
    with pytest.raises(BackupError) as error:
        MSSQLDumper(config, RecordingRunner()).create_dump(shared / "run.bak", tmp_path, "run")
    assert error.value.error_type == "SharedVolumeMismatch"
