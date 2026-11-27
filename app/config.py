"""Validated application configuration and Docker Secret resolution."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import BackupError

SECRET_PAIRS = {
    "DB_PASSWORD": "DB_PASSWORD_FILE",
    "STORAGE_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID_FILE",
    "STORAGE_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY_FILE",
    "EMAIL_PASSWORD": "SMTP_PASSWORD_FILE",
    "SEQ_API_KEY": "SEQ_API_KEY_FILE",
}


class BackupConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_type: str
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    auth_source: str = "admin"
    run_mode: str = "scheduler"
    run_on_start: bool = True
    schedule: str = "0 */12 * * *"
    timezone: str = "America/Sao_Paulo"
    backup_timeout_seconds: int = Field(default=3600, ge=1)
    misfire_grace_time_seconds: int = Field(default=300, ge=1)
    jitter_seconds: int = Field(default=0, ge=0)
    shutdown_grace_seconds: int = Field(default=30, ge=1)
    max_backup_age_seconds: int = Field(default=129600, ge=1)
    max_consecutive_failures: int = Field(default=3, ge=1)
    state_file: Path = Path("/var/lib/bsource-backup/state.json")
    temp_root: Path = Path("/tmp/bsource-backup")
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=1, le=65535)
    environment: str = "Development"
    storage_type: str
    storage_endpoint_url: str | None = None
    storage_access_key_id: str | None = None
    storage_secret_access_key: str | None = None
    storage_bucket_name: str | None = None
    storage_destination_folder: str = "backups/"
    storage_region: str | None = None
    storage_local_path: Path = Path("/app/backups")
    notification_provider: str = "noop"
    notify_on_success: bool = False
    notify_on_failure: bool = True
    notify_on_recovery: bool = True
    notify_after_failures: int = Field(default=1, ge=1)
    notification_cooldown_seconds: int = Field(default=3600, ge=0)
    email_from: str | None = None
    email_to: tuple[str, ...] = ()
    email_smtp: str | None = None
    email_port: int = Field(default=587, ge=1, le=65535)
    email_user: str | None = None
    email_password: str | None = None
    email_auth_mode: str = "credentials"
    email_starttls: bool = True
    webhook_url: str | None = None
    seq_url: str | None = None
    seq_api_key: str | None = None
    mssql_backup_mode: str | None = None
    mssql_server_backup_dir: str | None = None
    mssql_local_backup_dir: Path | None = None

    @field_validator("database_type", "storage_type", "run_mode", "notification_provider", "email_auth_mode")
    @classmethod
    def lower_token(cls, value: str) -> str:
        return value.lower().strip()

    @field_validator("schedule")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError("invalid cron expression")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_combinations(self) -> BackupConfig:
        if self.database_type not in {"postgres", "mysql", "mariadb", "mongodb", "mssql"}:
            raise ValueError("unsupported DB_TYPE")
        if self.run_mode not in {"once", "scheduler"}:
            raise ValueError("RUN_MODE must be once or scheduler")
        if self.storage_type not in {"s3", "r2", "local"}:
            raise ValueError("unsupported STORAGE_TYPE")
        if self.storage_type in {"s3", "r2"}:
            if not all((self.storage_access_key_id, self.storage_secret_access_key, self.storage_bucket_name)):
                raise ValueError("S3-compatible storage credentials and bucket are required")
            if self.storage_type == "r2" and not self.storage_endpoint_url:
                raise ValueError("STORAGE_ENDPOINT_URL is required for R2")
            if self.storage_type == "s3" and not self.storage_region:
                raise ValueError("STORAGE_REGION is required for S3")
        if self.storage_endpoint_url:
            parsed = urlparse(self.storage_endpoint_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("invalid STORAGE_ENDPOINT_URL")
        if self.notification_provider not in {"noop", "email", "webhook"}:
            raise ValueError("unsupported NOTIFICATION_PROVIDER")
        if self.email_auth_mode not in {"credentials", "none"}:
            raise ValueError("EMAIL_AUTH_MODE must be credentials or none")
        if self.notification_provider == "email":
            if not all((self.email_from, self.email_to, self.email_smtp)):
                raise ValueError("EMAIL_FROM, EMAIL_TO and EMAIL_SMTP are required")
            if self.email_auth_mode == "credentials" and not all((self.email_user, self.email_password)):
                raise ValueError("EMAIL_USER and EMAIL_PASSWORD are required for EMAIL_AUTH_MODE=credentials")
        if self.notification_provider == "webhook" and not self.webhook_url:
            raise ValueError("WEBHOOK_URL is required")
        if self.database_type == "mssql":
            if self.mssql_backup_mode != "shared-volume":
                raise ValueError("MSSQL_BACKUP_MODE=shared-volume is required")
            if not self.mssql_server_backup_dir or not self.mssql_local_backup_dir:
                raise ValueError("MSSQL_SERVER_BACKUP_DIR and MSSQL_LOCAL_BACKUP_DIR are required")
            if not self.mssql_server_backup_dir.startswith("/") or not self.mssql_local_backup_dir.is_absolute():
                raise ValueError("MSSQL backup directories must be absolute")
            if self.mssql_server_backup_dir in {"/", "/tmp"}:
                raise ValueError("MSSQL_SERVER_BACKUP_DIR is too broad")
        return self

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.password,
                self.storage_access_key_id,
                self.storage_secret_access_key,
                self.email_password,
                self.seq_api_key,
            )
            if value
        )


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise BackupError("configuration", "InvalidBoolean", f"{name} must be true or false")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError as exc:
        raise BackupError("configuration", "InvalidInteger", f"{name} must be an integer", cause=exc) from exc


def _read_secret(env: Mapping[str, str], direct: str, file_var: str) -> str | None:
    direct_value = env.get(direct)
    filename = env.get(file_var)
    if direct_value is not None and filename is not None:
        raise BackupError("configuration", "AmbiguousSecretSource", f"Configure only one of {direct} and {file_var}")
    if filename is None:
        return direct_value
    path = Path(filename)
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular file")
        if os.name != "nt" and mode & stat.S_IWOTH:
            raise ValueError("file is writable by others")
        value = path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
    except (OSError, ValueError) as exc:
        raise BackupError(
            "configuration", "InvalidSecretFile", f"Secret file configured by {file_var} is invalid", cause=exc
        ) from exc
    if not value:
        raise BackupError("configuration", "EmptySecret", f"Secret file configured by {file_var} is empty")
    return value


def load_config(environ: Mapping[str, str] | None = None) -> BackupConfig:
    env = dict(os.environ if environ is None else environ)
    secrets = {direct: _read_secret(env, direct, file_var) for direct, file_var in SECRET_PAIRS.items()}
    schedule = env.get("BACKUP_CRON", env.get("CRON_SCHEDULE", "0 */12 * * *"))
    timezone = env.get("BACKUP_TIMEZONE", env.get("TIMEZONE", "America/Sao_Paulo"))
    if "CRON_SCHEDULE" in env and "BACKUP_CRON" not in env:
        logging.getLogger("bsource.backup").warning(
            "CRON_SCHEDULE is deprecated; use BACKUP_CRON",
            extra={"Event": "DeprecatedConfiguration", "Stage": "configuration"},
        )
    if "TIMEZONE" in env and "BACKUP_TIMEZONE" not in env:
        logging.getLogger("bsource.backup").warning(
            "TIMEZONE is deprecated; use BACKUP_TIMEZONE",
            extra={"Event": "DeprecatedConfiguration", "Stage": "configuration"},
        )
    recipients = tuple(item.strip() for item in env.get("EMAIL_TO", "").split(",") if item.strip())
    auth_mode = env.get("EMAIL_AUTH_MODE", "credentials").lower().strip()
    provider = env.get("NOTIFICATION_PROVIDER")
    if provider is None:
        email_complete = all((env.get("EMAIL_FROM"), recipients, env.get("EMAIL_SMTP"))) and (
            auth_mode == "none" or all((env.get("EMAIL_USER"), secrets["EMAIL_PASSWORD"]))
        )
        provider = "email" if email_complete else "noop"
    try:
        return BackupConfig(
            database_type=env.get("DB_TYPE", ""),
            host=env.get("DB_HOST", ""),
            port=_int(env, "DB_PORT", 0),
            database=env.get("DB_DATABASE", ""),
            username=env.get("DB_USER", ""),
            password=secrets["DB_PASSWORD"] or "",
            auth_source=env.get("DB_AUTH_SOURCE", "admin"),
            run_mode=env.get("RUN_MODE", "scheduler"),
            run_on_start=_bool(env, "RUN_ON_START", True),
            schedule=schedule,
            timezone=timezone,
            backup_timeout_seconds=_int(env, "BACKUP_TIMEOUT_SECONDS", 3600),
            misfire_grace_time_seconds=_int(env, "BACKUP_MISFIRE_GRACE_TIME_SECONDS", 300),
            jitter_seconds=_int(env, "BACKUP_JITTER_SECONDS", 0),
            shutdown_grace_seconds=_int(env, "SHUTDOWN_GRACE_SECONDS", 30),
            max_backup_age_seconds=_int(env, "MAX_BACKUP_AGE_SECONDS", 129600),
            max_consecutive_failures=_int(env, "MAX_CONSECUTIVE_FAILURES", 3),
            state_file=Path(env.get("STATE_FILE", "/var/lib/bsource-backup/state.json")),
            temp_root=Path(env.get("BACKUP_TEMP_ROOT", "/tmp/bsource-backup")),
            http_host=env.get("HTTP_HOST", "0.0.0.0"),
            http_port=_int(env, "HTTP_PORT", 8080),
            environment=env.get("APP_ENV", "Development"),
            storage_type=env.get("STORAGE_TYPE", ""),
            storage_endpoint_url=env.get("STORAGE_ENDPOINT_URL"),
            storage_access_key_id=secrets["STORAGE_ACCESS_KEY_ID"],
            storage_secret_access_key=secrets["STORAGE_SECRET_ACCESS_KEY"],
            storage_bucket_name=env.get("STORAGE_BUCKET_NAME"),
            storage_destination_folder=env.get("STORAGE_DESTINATION_FOLDER", "backups/"),
            storage_region=env.get("STORAGE_REGION"),
            storage_local_path=Path(env.get("STORAGE_LOCAL_PATH", "/app/backups")),
            notification_provider=provider,
            notify_on_success=_bool(env, "NOTIFY_ON_SUCCESS", False),
            notify_on_failure=_bool(env, "NOTIFY_ON_FAILURE", True),
            notify_on_recovery=_bool(env, "NOTIFY_ON_RECOVERY", True),
            notify_after_failures=_int(env, "NOTIFY_AFTER_FAILURES", 1),
            notification_cooldown_seconds=_int(env, "NOTIFICATION_COOLDOWN_SECONDS", 3600),
            email_from=env.get("EMAIL_FROM"),
            email_to=recipients,
            email_smtp=env.get("EMAIL_SMTP"),
            email_port=_int(env, "EMAIL_PORT", 587),
            email_user=env.get("EMAIL_USER"),
            email_password=secrets["EMAIL_PASSWORD"],
            email_auth_mode=auth_mode,
            email_starttls=_bool(env, "EMAIL_STARTTLS", True),
            webhook_url=env.get("WEBHOOK_URL"),
            seq_url=env.get("SEQ_URL"),
            seq_api_key=secrets["SEQ_API_KEY"],
            mssql_backup_mode=env.get("MSSQL_BACKUP_MODE"),
            mssql_server_backup_dir=env.get("MSSQL_SERVER_BACKUP_DIR"),
            mssql_local_backup_dir=Path(env["MSSQL_LOCAL_BACKUP_DIR"]) if env.get("MSSQL_LOCAL_BACKUP_DIR") else None,
        )
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors())
        raise BackupError("configuration", "InvalidConfiguration", messages, cause=exc) from exc
