"""Database-specific dump, preflight, validation, and restore guidance."""

from __future__ import annotations

import json
import os
import posixpath
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .command_runner import CommandRunner
from .config import BackupConfig
from .errors import BackupError


def _write_secret_file(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


class DatabaseDumper(ABC):
    database_type: str
    extension: str

    def __init__(self, config: BackupConfig, runner: CommandRunner):
        self.config = config
        self.runner = runner

    def output_path(self, workdir: Path, run_id: str) -> Path:
        return workdir / f"backup{self.extension}"

    @abstractmethod
    def check_client_version(self) -> str: ...

    @abstractmethod
    def check_connection(self, workdir: Path) -> str: ...

    @abstractmethod
    def create_dump(self, output_path: Path, workdir: Path, run_id: str) -> None: ...

    @abstractmethod
    def validate_dump(self, output_path: Path, workdir: Path, run_id: str) -> None: ...

    @abstractmethod
    def get_restore_instructions(self, object_path: str) -> str: ...

    def _require_file(self, output_path: Path) -> None:
        if not output_path.is_file():
            raise BackupError("validation", "DumpMissing", "Database client did not create the expected dump file")
        if output_path.stat().st_size <= 0:
            raise BackupError("validation", "DumpEmpty", "Database client created an empty dump file")


class PostgreSQLDumper(DatabaseDumper):
    database_type = "postgresql"
    extension = ".dump"

    @property
    def env(self) -> dict[str, str]:
        return {"PGPASSWORD": self.config.password}

    def check_client_version(self) -> str:
        result = self.runner.run(["pg_dump", "--version"], stage="preflight", timeout=15)
        match = re.search(r"(\d+)(?:\.\d+)?", result.stdout)
        if not match or int(match.group(1)) != 16:
            raise BackupError("preflight", "IncompatibleClient", "PostgreSQL client major 16 is required")
        return match.group(0)

    def check_connection(self, workdir: Path) -> str:
        result = self.runner.run(
            [
                "psql",
                "-X",
                "-h",
                self.config.host,
                "-p",
                str(self.config.port),
                "-U",
                self.config.username,
                "-d",
                self.config.database,
                "-Atqc",
                "SHOW server_version_num",
            ],
            stage="connection",
            env=self.env,
            timeout=30,
        )
        try:
            major = int(result.stdout.strip()) // 10000
        except ValueError as exc:
            raise BackupError(
                "connection", "UnknownServerVersion", "Could not determine PostgreSQL server version", cause=exc
            ) from exc
        if major not in {15, 16}:
            raise BackupError("preflight", "IncompatibleServer", f"PostgreSQL server major {major} is not supported")
        return str(major)

    def create_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        self.runner.run(
            [
                "pg_dump",
                "-h",
                self.config.host,
                "-p",
                str(self.config.port),
                "-U",
                self.config.username,
                "-F",
                "c",
                "-b",
                "-f",
                str(output_path),
                self.config.database,
            ],
            stage="dump",
            env=self.env,
            timeout=self.config.backup_timeout_seconds,
        )

    def validate_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        self._require_file(output_path)
        self.runner.run(["pg_restore", "--list", str(output_path)], stage="validation", timeout=300)

    def get_restore_instructions(self, object_path: str) -> str:
        return f"pg_restore -h HOST -U USER -d TARGET_DB --clean --if-exists {object_path}"


class _SQLTextDumper(DatabaseDumper):
    extension = ".sql"
    client_binary: str
    dump_binary: str
    expected_client_major: str
    supported_servers: set[str]
    expect_mariadb: bool

    def _option_file(self, workdir: Path) -> Path:
        password = json.dumps(self.config.password, ensure_ascii=False)
        return _write_secret_file(workdir / "db-client.cnf", f"[client]\npassword={password}\n")

    def check_client_version(self) -> str:
        result = self.runner.run([self.dump_binary, "--version"], stage="preflight", timeout=15)
        text = f"{result.stdout} {result.stderr}"
        is_mariadb = "mariadb" in text.lower()
        if is_mariadb != self.expect_mariadb or self.expected_client_major not in text:
            raise BackupError("preflight", "IncompatibleClient", f"Unexpected {self.database_type} dump client version")
        return text.strip()

    def check_connection(self, workdir: Path) -> str:
        option_file = self._option_file(workdir)
        result = self.runner.run(
            [
                self.client_binary,
                f"--defaults-file={option_file}",
                "--host",
                self.config.host,
                "--port",
                str(self.config.port),
                "--user",
                self.config.username,
                "--batch",
                "--skip-column-names",
                "--execute",
                "SELECT VERSION()",
            ],
            stage="connection",
            timeout=30,
        )
        version = result.stdout.strip().splitlines()[-1]
        is_mariadb = "mariadb" in version.lower()
        if is_mariadb != self.expect_mariadb:
            raise BackupError(
                "preflight", "WrongServerEngine", f"Configured {self.database_type} does not match the connected server"
            )
        normalized = version.lower().replace("-mariadb", "")
        if not any(normalized.startswith(prefix) for prefix in self.supported_servers):
            raise BackupError(
                "preflight", "IncompatibleServer", f"Unsupported {self.database_type} server version: {version}"
            )
        return version

    def create_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        option_file = self._option_file(workdir)
        self.runner.run(
            [
                self.dump_binary,
                f"--defaults-file={option_file}",
                "--host",
                self.config.host,
                "--port",
                str(self.config.port),
                "--user",
                self.config.username,
                "--single-transaction",
                "--routines",
                "--triggers",
                "--events",
                "--hex-blob",
                f"--result-file={output_path}",
                "--databases",
                self.config.database,
            ],
            stage="dump",
            timeout=self.config.backup_timeout_seconds,
        )

    def validate_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        self._require_file(output_path)
        try:
            with output_path.open("rb") as stream:
                head = stream.read(8192)
                stream.seek(max(0, output_path.stat().st_size - 8192))
                tail = stream.read()
        except OSError as exc:
            raise BackupError("validation", "DumpUnreadable", "SQL dump cannot be read", cause=exc) from exc
        if b"\x00" in head or b"--" not in head or b";" not in tail:
            raise BackupError("validation", "InvalidSQLDump", "SQL dump failed structural validation")

    def get_restore_instructions(self, object_path: str) -> str:
        return f"{self.client_binary} --host HOST --user USER --password TARGET_DB < {object_path}"


class MySQLDumper(_SQLTextDumper):
    database_type = "mysql"
    client_binary = "mysql"
    dump_binary = "mysqldump"
    expected_client_major = "8.4"
    supported_servers = {"8.0", "8.4"}
    expect_mariadb = False


class MariaDBDumper(_SQLTextDumper):
    database_type = "mariadb"
    client_binary = "mariadb"
    dump_binary = "mariadb-dump"
    expected_client_major = "11.4"
    supported_servers = {"10.11", "11.4"}
    expect_mariadb = True


class MongoDBDumper(DatabaseDumper):
    database_type = "mongodb"
    extension = ".archive.gz"

    def _config_file(self, workdir: Path) -> Path:
        return _write_secret_file(workdir / "mongodump.yml", json.dumps({"password": self.config.password}))

    def check_client_version(self) -> str:
        result = self.runner.run(["mongodump", "--version"], stage="preflight", timeout=15)
        if not re.search(r"100\.\d+\.\d+", result.stdout):
            raise BackupError("preflight", "IncompatibleClient", "MongoDB Database Tools 100.x is required")
        return result.stdout.strip()

    def check_connection(self, workdir: Path) -> str:
        try:
            from pymongo import MongoClient

            client: MongoClient[dict[str, Any]] = MongoClient(
                self.config.host,
                self.config.port,
                username=self.config.username,
                password=self.config.password,
                authSource=self.config.auth_source,
                serverSelectionTimeoutMS=30000,
            )
            info = client.admin.command("buildInfo")
            client.admin.command("ping")
            client.close()
            version = str(info["version"])
        except Exception as exc:
            raise BackupError("connection", "DatabaseConnectionFailed", "MongoDB connection failed", cause=exc) from exc
        if version.split(".", 1)[0] not in {"7", "8"}:
            raise BackupError("preflight", "IncompatibleServer", f"MongoDB server version {version} is not supported")
        return version

    def create_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        secret_file = self._config_file(workdir)
        self.runner.run(
            [
                "mongodump",
                f"--config={secret_file}",
                f"--host={self.config.host}",
                f"--port={self.config.port}",
                f"--username={self.config.username}",
                f"--authenticationDatabase={self.config.auth_source}",
                f"--db={self.config.database}",
                f"--archive={output_path}",
                "--gzip",
            ],
            stage="dump",
            timeout=self.config.backup_timeout_seconds,
        )

    def validate_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        self._require_file(output_path)
        secret_file = self._config_file(workdir)
        self.runner.run(
            [
                "mongorestore",
                f"--config={secret_file}",
                f"--host={self.config.host}",
                f"--port={self.config.port}",
                f"--username={self.config.username}",
                f"--authenticationDatabase={self.config.auth_source}",
                f"--archive={output_path}",
                "--gzip",
                "--dryRun",
            ],
            stage="validation",
            timeout=300,
        )

    def get_restore_instructions(self, object_path: str) -> str:
        return f"mongorestore --archive={object_path} --gzip --nsInclude='{self.config.database}.*'"


class MSSQLDumper(DatabaseDumper):
    database_type = "mssql"
    extension = ".bak"

    @property
    def env(self) -> dict[str, str]:
        return {"SQLCMDPASSWORD": self.config.password}

    def _base_args(self) -> list[str]:
        return [
            "sqlcmd",
            "-S",
            f"{self.config.host},{self.config.port}",
            "-U",
            self.config.username,
            "-C",
            "-b",
            "-W",
            "-h",
            "-1",
        ]

    def output_path(self, workdir: Path, run_id: str) -> Path:
        assert self.config.mssql_local_backup_dir is not None
        return self.config.mssql_local_backup_dir / f"{run_id}.bak"

    def _server_path(self, run_id: str) -> str:
        assert self.config.mssql_server_backup_dir is not None
        return posixpath.join(self.config.mssql_server_backup_dir.rstrip("/"), f"{run_id}.bak")

    @staticmethod
    def _identifier(value: str) -> str:
        return "[" + value.replace("]", "]]") + "]"

    @staticmethod
    def _literal(value: str) -> str:
        return "N'" + value.replace("'", "''") + "'"

    def check_client_version(self) -> str:
        result = self.runner.run(["sqlcmd", "-?"], stage="preflight", timeout=15)
        text = f"{result.stdout} {result.stderr}"
        if "18." not in text:
            raise BackupError("preflight", "IncompatibleClient", "Microsoft sqlcmd/ODBC 18 is required")
        return text.strip()

    def check_connection(self, workdir: Path) -> str:
        local_dir = self.config.mssql_local_backup_dir
        if local_dir is None or not local_dir.is_dir() or not os.access(local_dir, os.R_OK | os.W_OK):
            raise BackupError(
                "preflight", "SharedVolumeUnavailable", "MSSQL shared volume is not readable and writable"
            )
        query = "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS varchar(10));"
        result = self.runner.run(self._base_args() + ["-Q", query], stage="connection", env=self.env, timeout=30)
        version = result.stdout.strip().splitlines()[-1]
        if version not in {"15", "16"}:
            raise BackupError("preflight", "IncompatibleServer", f"SQL Server major {version} is not supported")
        return version

    def create_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        if output_path.exists():
            raise BackupError("dump", "OutputCollision", "Unique SQL Server output file already exists")
        query = (
            f"BACKUP DATABASE {self._identifier(self.config.database)} "
            f"TO DISK = {self._literal(self._server_path(run_id))} "
            "WITH COPY_ONLY, INIT, CHECKSUM, COMPRESSION;"
        )
        self.runner.run(
            self._base_args() + ["-Q", query],
            stage="dump",
            env=self.env,
            timeout=self.config.backup_timeout_seconds,
        )
        if not output_path.is_file():
            raise BackupError(
                "dump",
                "SharedVolumeMismatch",
                "SQL Server completed the backup but the BAK is not visible in MSSQL_LOCAL_BACKUP_DIR",
            )

    def validate_dump(self, output_path: Path, workdir: Path, run_id: str) -> None:
        self._require_file(output_path)
        query = f"RESTORE VERIFYONLY FROM DISK = {self._literal(self._server_path(run_id))} WITH CHECKSUM;"
        self.runner.run(self._base_args() + ["-Q", query], stage="validation", env=self.env, timeout=600)

    def get_restore_instructions(self, object_path: str) -> str:
        return f"RESTORE DATABASE [TARGET_DB] FROM DISK = N'{object_path}' WITH MOVE ..."


def create_dumper(config: BackupConfig, runner: CommandRunner) -> DatabaseDumper:
    dumper_classes: dict[str, type[DatabaseDumper]] = {
        "postgres": PostgreSQLDumper,
        "mysql": MySQLDumper,
        "mariadb": MariaDBDumper,
        "mongodb": MongoDBDumper,
        "mssql": MSSQLDumper,
    }
    dumper_class = dumper_classes[config.database_type]
    return dumper_class(config, runner)


SUPPORTED_DB_TYPES = ["postgres", "mysql", "mariadb", "mongodb", "mssql"]
