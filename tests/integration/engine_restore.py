"""Container-level dump and restore tests for non-PostgreSQL engines."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid

SECRET = "Test-Password_2026!"
APP_IMAGE = "bsource-database-backup:test"


def docker(*args: str, capture: bool = False, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode and not allow_failure:
        message = (result.stderr or result.stdout or "docker command failed").replace(SECRET, "***")
        raise RuntimeError(message.strip())
    return result.stdout.strip() if capture else ""


def wait_for(container: str, *command: str) -> None:
    for _ in range(90):
        result = subprocess.run(["docker", "exec", container, *command], check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return
        time.sleep(1)
    logs = docker("logs", container, capture=True, allow_failure=True).replace(SECRET, "***")[-4000:]
    raise RuntimeError(f"{container} did not become ready\n{logs}")


class Environment:
    def __init__(self, engine: str):
        suffix = uuid.uuid4().hex[:10]
        self.engine = engine
        self.network = f"bsource-{engine}-{suffix}"
        self.source = f"bsource-{engine}-source-{suffix}"
        self.target = f"bsource-{engine}-target-{suffix}"
        self.storage = f"bsource-{engine}-storage-{suffix}"
        self.shared = f"bsource-{engine}-shared-{suffix}"

    def create(self) -> None:
        docker("network", "create", self.network)
        docker("volume", "create", self.storage)
        if self.engine == "mssql":
            docker("volume", "create", self.shared)
            docker(
                "run",
                "--rm",
                "-v",
                f"{self.shared}:/shared",
                "alpine:3.21.3",
                "sh",
                "-c",
                "touch /shared/.bsource-volume && "
                "chown 10001:2000 /shared /shared/.bsource-volume && chmod 2770 /shared",
            )

    def cleanup(self) -> None:
        docker("rm", "-f", self.source, self.target, allow_failure=True)
        docker("network", "rm", self.network, allow_failure=True)
        docker("volume", "rm", self.storage, allow_failure=True)
        if self.engine == "mssql":
            docker("volume", "rm", self.shared, allow_failure=True)


def backup_args(env: Environment, engine: str, host: str, port: int, database: str) -> list[str]:
    args = [
        "run",
        "--rm",
        "--network",
        env.network,
        "-v",
        f"{env.storage}:/app/backups",
        "-e",
        "RUN_MODE=once",
        "-e",
        "BACKUP_TIMEZONE=UTC",
        "-e",
        "APP_ENV=integration",
        "-e",
        f"DB_TYPE={engine}",
        "-e",
        f"DB_HOST={host}",
        "-e",
        f"DB_PORT={port}",
        "-e",
        "DB_USER=root" if engine != "mssql" else "DB_USER=sa",
        "-e",
        f"DB_PASSWORD={SECRET}",
        "-e",
        f"DB_DATABASE={database}",
        "-e",
        "DB_AUTH_SOURCE=admin",
        "-e",
        "STORAGE_TYPE=local",
        "-e",
        "STORAGE_LOCAL_PATH=/app/backups",
        "-e",
        "STORAGE_DESTINATION_FOLDER=integration",
    ]
    if engine == "mssql":
        args.extend(
            [
                "--group-add",
                "2000",
                "-v",
                f"{env.shared}:/shared",
                "-e",
                "MSSQL_BACKUP_MODE=shared-volume",
                "-e",
                "MSSQL_SERVER_BACKUP_DIR=/var/opt/mssql/backup",
                "-e",
                "MSSQL_LOCAL_BACKUP_DIR=/shared",
            ]
        )
    return [*args, APP_IMAGE, "run"]


def find_backup(env: Environment, pattern: str) -> str:
    result = docker(
        "run",
        "--rm",
        "-v",
        f"{env.storage}:/storage:ro",
        "--entrypoint",
        "find",
        APP_IMAGE,
        "/storage",
        "-type",
        "f",
        "-name",
        pattern,
        "-print",
        "-quit",
        capture=True,
    )
    if not result:
        raise RuntimeError(f"No backup matching {pattern} was found")
    return result


def mysql_restore(engine: str, image: str) -> None:
    env = Environment(engine)
    env.create()
    client = "mysql" if engine == "mysql" else "mariadb"
    dump_pattern = "*.sql"
    try:
        common = [
            "-e",
            f"{engine.upper()}_ROOT_PASSWORD={SECRET}",
            "-e",
            f"{engine.upper()}_DATABASE=source_db",
        ]
        # MariaDB uses MARIADB_*; MySQL uses MYSQL_*.
        docker(
            "run", "-d", "--name", env.source, "--network", env.network,
            "--network-alias", "db-source", *common, image,
        )
        docker(
            "run",
            "-d",
            "--name",
            env.target,
            "--network",
            env.network,
            "--network-alias",
            "db-target",
            "-e",
            f"{engine.upper()}_ROOT_PASSWORD={SECRET}",
            image,
        )
        wait_for(env.source, client, "-uroot", f"-p{SECRET}", "-e", "SELECT 1")
        wait_for(env.target, client, "-uroot", f"-p{SECRET}", "-e", "SELECT 1")
        docker(
            "exec",
            env.source,
            client,
            "-uroot",
            f"-p{SECRET}",
            "source_db",
            "-e",
            "CREATE TABLE restore_probe(id INT PRIMARY KEY,value VARCHAR(20));"
            "INSERT INTO restore_probe VALUES(1,'alpha'),(2,'beta'),(3,'gamma');",
        )
        docker(*backup_args(env, engine, "db-source", 3306, "source_db"))
        backup = find_backup(env, dump_pattern)
        docker(
            "run",
            "--rm",
            "--network",
            env.network,
            "-v",
            f"{env.storage}:/storage:ro",
            "-e",
            f"MYSQL_PWD={SECRET}",
            "--entrypoint",
            client,
            APP_IMAGE,
            "--host",
            "db-target",
            "--user",
            "root",
            "--execute",
            f"source {backup}",
        )
        count = docker(
            "exec",
            env.target,
            client,
            "-uroot",
            f"-p{SECRET}",
            "source_db",
            "--batch",
            "--skip-column-names",
            "-e",
            "SELECT COUNT(*) FROM restore_probe",
            capture=True,
        ).splitlines()[-1]
        if count != "3":
            raise RuntimeError(f"Expected 3 restored rows, received {count}")
    finally:
        env.cleanup()


def mongo_restore(image: str) -> None:
    env = Environment("mongodb")
    env.create()
    try:
        base = ["-e", "MONGO_INITDB_ROOT_USERNAME=root", "-e", f"MONGO_INITDB_ROOT_PASSWORD={SECRET}"]
        docker(
            "run", "-d", "--name", env.source, "--network", env.network,
            "--network-alias", "db-source", *base, image,
        )
        docker(
            "run", "-d", "--name", env.target, "--network", env.network,
            "--network-alias", "db-target", *base, image,
        )
        ping = [
            "mongosh",
            "-u",
            "root",
            "-p",
            SECRET,
            "--authenticationDatabase",
            "admin",
            "--eval",
            "db.runCommand({ping:1})",
        ]
        wait_for(env.source, *ping)
        wait_for(env.target, *ping)
        docker(
            "exec",
            env.source,
            "mongosh",
            "-u",
            "root",
            "-p",
            SECRET,
            "--authenticationDatabase",
            "admin",
            "source_db",
            "--eval",
            "db.restore_probe.insertMany([{id:1},{id:2},{id:3}])",
        )
        docker(*backup_args(env, "mongodb", "db-source", 27017, "source_db"))
        backup = find_backup(env, "*.archive.gz")
        docker(
            "run",
            "--rm",
            "--network",
            env.network,
            "-v",
            f"{env.storage}:/storage:ro",
            "--entrypoint",
            "mongorestore",
            APP_IMAGE,
            "--host=db-target",
            "--port=27017",
            "--username=root",
            f"--password={SECRET}",
            "--authenticationDatabase=admin",
            f"--archive={backup}",
            "--gzip",
        )
        output = docker(
            "exec",
            env.target,
            "mongosh",
            "-u",
            "root",
            "-p",
            SECRET,
            "--authenticationDatabase",
            "admin",
            "source_db",
            "--quiet",
            "--eval",
            "db.restore_probe.countDocuments({})",
            capture=True,
        ).splitlines()[-1]
        if output != "3":
            raise RuntimeError(f"Expected 3 restored documents, received {output}")
    finally:
        env.cleanup()


def mssql_restore(image: str) -> None:
    env = Environment("mssql")
    env.create()
    sqlcmd = "/opt/mssql-tools18/bin/sqlcmd"
    base = ["-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={SECRET}"]
    try:
        docker(
            "run",
            "-d",
            "--name",
            env.source,
            "--group-add",
            "2000",
            "--network",
            env.network,
            "--network-alias",
            "db-source",
            "-v",
            f"{env.shared}:/var/opt/mssql/backup",
            *base,
            image,
        )
        docker(
            "run",
            "-d",
            "--name",
            env.target,
            "--group-add",
            "2000",
            "--network",
            env.network,
            "--network-alias",
            "db-target",
            "-v",
            f"{env.shared}:/var/opt/mssql/backup",
            *base,
            image,
        )
        ready = [sqlcmd, "-S", "localhost", "-U", "sa", "-P", SECRET, "-C", "-Q", "SELECT 1"]
        wait_for(env.source, *ready)
        wait_for(env.target, *ready)
        base_sqlcmd = ["exec", env.source, sqlcmd, "-S", "localhost", "-U", "sa", "-P", SECRET, "-C", "-b"]
        docker(*base_sqlcmd, "-Q", "CREATE DATABASE source_db")
        docker(
            *base_sqlcmd,
            "-d",
            "source_db",
            "-Q",
            "CREATE TABLE restore_probe(id int primary key,value varchar(20)); "
            "INSERT INTO restore_probe VALUES(1,'alpha'),(2,'beta'),(3,'gamma');",
        )
        docker(*backup_args(env, "mssql", "db-source", 1433, "source_db"))
        backup = find_backup(env, "*.bak")
        docker(
            "run",
            "--rm",
            "-v",
            f"{env.storage}:/storage:ro",
            "-v",
            f"{env.shared}:/shared",
            "alpine:3.21.3",
            "sh",
            "-c",
            f"cp '{backup}' /shared/restore.bak && "
            "chown 10001:2000 /shared/restore.bak && chmod 0640 /shared/restore.bak",
        )
        restore = (
            "RESTORE DATABASE restored_db FROM DISK=N'/var/opt/mssql/backup/restore.bak' "
            "WITH MOVE N'source_db' TO N'/var/opt/mssql/data/restored_db.mdf', "
            "MOVE N'source_db_log' TO N'/var/opt/mssql/data/restored_db_log.ldf';"
        )
        docker("exec", env.target, sqlcmd, "-S", "localhost", "-U", "sa", "-P", SECRET, "-C", "-b", "-Q", restore)
        count = docker(
            "exec",
            env.target,
            sqlcmd,
            "-S",
            "localhost",
            "-U",
            "sa",
            "-P",
            SECRET,
            "-C",
            "-h",
            "-1",
            "-W",
            "-Q",
            "SET NOCOUNT ON; SELECT COUNT(*) FROM restored_db.dbo.restore_probe",
            capture=True,
        ).splitlines()[-1]
        if count.strip() != "3":
            raise RuntimeError(f"Expected 3 restored rows, received {count}")
    finally:
        env.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=["mysql", "mariadb", "mongodb", "mssql"])
    parser.add_argument("image")
    args = parser.parse_args()
    if args.engine in {"mysql", "mariadb"}:
        mysql_restore(args.engine, args.image)
    elif args.engine == "mongodb":
        mongo_restore(args.image)
    else:
        mssql_restore(args.image)
    print(f"{args.engine} dump and restore verified against {args.image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
