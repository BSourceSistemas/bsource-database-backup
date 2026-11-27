#!/usr/bin/env python3
"""Restore one exact R2 backup into disposable PostgreSQL 17 containers.

Requires Docker, a backup run JSON result, and application-specific SQL/expected
output files. No R2 object is deleted and no source database is contacted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def docker(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True)
    if result.returncode:
        # Docker/SMTP/storage error text can contain connection details or secrets.
        raise RuntimeError("Docker operation failed; inspect the isolated test environment locally")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Backup image pinned by @sha256 digest")
    parser.add_argument("--env-file", type=Path, required=True, help="Existing R2 application configuration")
    parser.add_argument("--secrets-dir", type=Path, help="Optional directory mounted read-only at /run/secrets")
    parser.add_argument("--result", type=Path, required=True, help="Final JSON object from backup run")
    parser.add_argument("--verify-sql", type=Path, required=True, help="Deterministic application validation queries")
    parser.add_argument("--expected-results", type=Path, required=True, help="Expected psql -At output")
    parser.add_argument("--postgres-image", default="postgres:17.10-bookworm")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        parser.error("--image must use an immutable registry digest")
    result = json.loads(args.result.read_text())
    if not result.get("success") or not all(result.get(k) for k in ("objectKey", "checksum", "fileSizeBytes")):
        parser.error("--result must describe a successful backup with key, checksum and size")
    sql = args.verify_sql.read_text()
    expected = args.expected_results.read_text().strip()
    if not sql.strip() or not expected:
        parser.error("Verification SQL and expected results must not be empty")
    suffix = uuid.uuid4().hex[:12]
    network = f"bsource-restore-{suffix}"
    target = f"bsource-restore-pg-{suffix}"
    artifact = f"bsource-restore-artifact-{suffix}"
    data = f"bsource-restore-data-{suffix}"
    with tempfile.TemporaryDirectory(prefix="bsource-restore-") as temporary:
        work = Path(temporary)
        metadata = work / "result.json"
        metadata.write_text(json.dumps(result))
        password = work / "pg-password"
        password.write_text(uuid.uuid4().hex)
        password.chmod(0o600)
        pg_env = work / "postgres.env"
        pg_env.write_text(f"POSTGRES_PASSWORD={password.read_text()}\n")
        pg_env.chmod(0o600)
        try:
            docker("network", "create", "--internal", network)
            docker("volume", "create", artifact)
            docker("volume", "create", data)
            secret_mount = ["-v", f"{args.secrets_dir.resolve()}:/run/secrets:ro"] if args.secrets_dir else []
            docker(
                "run",
                "--rm",
                "-i",
                "--user",
                "0",
                "--env-file",
                str(args.env_file.resolve()),
                *secret_mount,
                "-e",
                "NOTIFICATION_PROVIDER=noop",
                "-v",
                f"{metadata}:/result.json:ro",
                "-v",
                f"{artifact}:/artifact",
                "--entrypoint",
                "python",
                args.image,
                "-",
                input_text="""
import json
from pathlib import Path
from app.config import load_config
from app.storage_provider import create_storage, sha256_file
try:
    config = load_config()
    if config.storage_type != "r2" or config.database_type != "postgres":
        raise ValueError("Expected R2 and PostgreSQL configuration")
    result = json.loads(Path("/result.json").read_text())
    storage = create_storage(config)
    storage.verify(result["objectKey"], result["fileSizeBytes"], result["checksum"])
    dump = storage.download(result["objectKey"], Path("/artifact/backup.dump"))
    if dump.stat().st_size != result["fileSizeBytes"] or sha256_file(dump) != result["checksum"]:
        raise ValueError("Downloaded object integrity mismatch")
    dump.chmod(0o644)
except Exception:
    raise SystemExit("R2 download or integrity verification failed") from None
""",
            )
            docker(
                "run",
                "-d",
                "--name",
                target,
                "--network",
                network,
                "-v",
                f"{data}:/var/lib/postgresql/data",
                "--env-file",
                str(pg_env),
                "-e",
                "POSTGRES_DB=restore_check",
                args.postgres_image,
            )
            for _ in range(90):
                try:
                    docker("exec", target, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "restore_check")
                    break
                except RuntimeError:
                    time.sleep(1)
            else:
                raise RuntimeError("Isolated PostgreSQL did not become ready")
            version = int(
                docker(
                    "exec",
                    target,
                    "psql",
                    "-X",
                    "-U",
                    "postgres",
                    "-d",
                    "restore_check",
                    "-Atc",
                    "SHOW server_version_num",
                )
            )
            if not 170000 <= version < 180000:
                raise RuntimeError("The isolated restore target must be PostgreSQL 17")
            # Only the disposable database password is mounted for restore.
            docker(
                "run",
                "--rm",
                "--network",
                network,
                "--user",
                "0",
                "-v",
                f"{artifact}:/artifact:ro",
                "-v",
                f"{password}:/run/secrets/pg-password:ro",
                "--entrypoint",
                "/bin/sh",
                args.image,
                "-c",
                'export PGPASSWORD="$(cat /run/secrets/pg-password)"; exec pg_restore '
                '--host="$1" --username=postgres --dbname=restore_check --exit-on-error '
                "--no-owner --no-acl /artifact/backup.dump",
                "restore",
                target,
            )
            actual = docker(
                "exec",
                "-i",
                target,
                "psql",
                "-X",
                "-U",
                "postgres",
                "-d",
                "restore_check",
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                input_text=sql,
            )
            if actual != expected:
                raise RuntimeError("Restored schema/data queries did not match expected results")
            print(
                json.dumps(
                    {
                        "restoreVerified": True,
                        "runId": result.get("runId"),
                        "objectKey": result["objectKey"],
                        "checksum": result["checksum"],
                        "image": args.image,
                        "postgresImage": args.postgres_image,
                    }
                )
            )
        finally:
            for command in (
                ("rm", "-f", target),
                ("network", "rm", network),
                ("volume", "rm", artifact),
                ("volume", "rm", data),
            ):
                subprocess.run(["docker", *command], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(f"Restore acceptance failed ({type(error).__name__}); no acceptance recorded") from None
