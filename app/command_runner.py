"""Secure subprocess execution without shells or global environment mutation."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from .errors import BackupError

_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)\s*[=:]\s*([^\s,;]+)"),
    re.compile(r"(?i)(mongodb(?:\+srv)?://[^:/\s]+:)([^@\s]+)(@)"),
)


def sanitize_text(text: str, secrets: Sequence[str] = ()) -> str:
    sanitized = text or ""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        sanitized = sanitized.replace(secret, "***")
    sanitized = _CREDENTIAL_PATTERNS[0].sub(lambda m: f"{m.group(1)}=***", sanitized)
    sanitized = _CREDENTIAL_PATTERNS[1].sub(r"\1***\3", sanitized)
    return sanitized


@dataclass(slots=True)
class CommandResult:
    args: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    duration_ms: int


class CommandRunner:
    def __init__(self, secrets: Sequence[str] = (), max_output_chars: int = 65536):
        self._secrets = tuple(secrets)
        self._max_output_chars = max_output_chars
        self._active: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def run(
        self,
        args: Sequence[str],
        *,
        stage: str,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        if not args or any(not isinstance(arg, str) for arg in args):
            raise ValueError("args must be a non-empty sequence of strings")
        started = time.monotonic()
        child_env = os.environ.copy()
        if env:
            child_env.update(env)
        try:
            process = subprocess.Popen(
                list(args),
                shell=False,
                env=child_env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._lock:
                self._active = process
            try:
                stdout, stderr = process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                stdout, stderr = process.communicate()
                raise BackupError(
                    stage=stage,
                    error_type="CommandTimeout",
                    sanitized_message=sanitize_text(
                        f"Command timed out after {timeout}s: {stderr[-2048:]}", self._secrets
                    ),
                    cause=exc,
                ) from exc
        except FileNotFoundError as exc:
            raise BackupError(
                stage=stage,
                error_type="ClientNotInstalled",
                sanitized_message=f"Required database client is not installed: {args[0]}",
                cause=exc,
            ) from exc
        finally:
            with self._lock:
                self._active = None

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = sanitize_text(stdout[-self._max_output_chars :], self._secrets)
        stderr = sanitize_text(stderr[-self._max_output_chars :], self._secrets)
        if process.returncode != 0:
            message = stderr.strip() or stdout.strip() or f"Client exited with code {process.returncode}"
            raise BackupError(
                stage=stage,
                error_type="ClientCommandFailed",
                sanitized_message=message,
                exit_code=process.returncode,
            )
        return CommandResult(tuple(args), process.returncode, stdout, stderr, duration_ms)

    def cancel(self) -> None:
        with self._lock:
            process = self._active
        if process and process.poll() is None:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            return
        kill_group = os.killpg
        try:
            kill_group(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            with suppress(ProcessLookupError):
                kill_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
