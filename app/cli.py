"""Command-line interface for one-shot and scheduled operation."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import uvicorn

from .api import create_api
from .command_runner import CommandRunner
from .config import load_config
from .db_dumper import create_dumper
from .errors import BackupError, ExitCode, stage_exit_code
from .executor import BackupExecutor
from .observability import configure_logging
from .state_store import BackupStateStore
from .storage_provider import create_storage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    sub.add_parser("scheduler")
    config = sub.add_parser("config")
    config.add_subparsers(dest="action", required=True).add_parser("check")
    database = sub.add_parser("database")
    database.add_subparsers(dest="action", required=True).add_parser("check")
    storage = sub.add_parser("storage")
    storage.add_subparsers(dest="action", required=True).add_parser("check")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    try:
        config = load_config()
    except BackupError as exc:
        print(json.dumps({"status": "invalid", "errorType": exc.error_type, "message": exc.sanitized_message}))
        return int(ExitCode.CONFIGURATION)
    configure_logging(config)

    command = args.command or ("run" if config.run_mode == "once" else "scheduler")
    if command == "config":
        print(json.dumps({"status": "valid"}))
        return 0
    if command == "status":
        state = BackupStateStore(config.state_file).snapshot()
        if args.as_json:
            print(json.dumps(state, ensure_ascii=False))
        else:
            summary = (
                f"lastSuccess={state.get('lastSuccess')} "
                f"consecutiveFailures={state.get('consecutiveFailures')} "
                f"running={state.get('currentRun') is not None}"
            )
            print(summary)
        return 0

    runner = CommandRunner(config.secrets)
    if command == "database":
        try:
            dumper = create_dumper(config, runner)
            config.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=config.temp_root) as name:
                dumper.check_client_version()
                version = dumper.check_connection(Path(name))
            print(json.dumps({"status": "ok", "serverVersion": version}))
            return 0
        except BackupError as exc:
            print(json.dumps({"status": "failed", "errorType": exc.error_type, "message": exc.sanitized_message}))
            return int(ExitCode.CONNECTION)
    if command == "storage":
        try:
            create_storage(config).check_connection()
            print(json.dumps({"status": "ok"}))
            return 0
        except BackupError as exc:
            print(json.dumps({"status": "failed", "errorType": exc.error_type, "message": exc.sanitized_message}))
            return int(ExitCode.UPLOAD)

    executor = BackupExecutor(config)
    if command == "run":
        result = executor.run()
        print(
            json.dumps(
                {
                    "success": result.success,
                    "runId": result.run_id,
                    "objectKey": result.object_key,
                    "checksum": result.checksum,
                    "fileSizeBytes": result.file_size_bytes,
                    "durationMs": result.duration_ms,
                    "error": result.error,
                },
                ensure_ascii=False,
            )
        )
        if result.success:
            return 0
        stage = result.error.get("stage", "dump") if result.error else "dump"
        return int(stage_exit_code(stage))
    if command == "scheduler":
        app = create_api(config, executor)
        uvicorn.run(app, host=config.http_host, port=config.http_port, workers=1, log_config=None)
        return 0
    return int(ExitCode.CONFIGURATION)


if __name__ == "__main__":
    sys.exit(main())
