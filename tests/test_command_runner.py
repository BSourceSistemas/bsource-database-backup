from __future__ import annotations

import subprocess

import pytest

from app.command_runner import CommandRunner, sanitize_text
from app.errors import BackupError


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = 0
        self.pid = 123

    def communicate(self, input=None, timeout=None):
        return "ok", ""

    def poll(self):
        return self.returncode


def test_runner_always_disables_shell(monkeypatch):
    captured = {}

    def factory(args, **kwargs):
        captured.update(kwargs)
        return FakeProcess(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", factory)
    result = CommandRunner(["secret"]).run(["tool", "arg with spaces"], stage="dump", env={"PASSWORD": "secret"})
    assert result.return_code == 0
    assert captured["shell"] is False
    assert captured["env"]["PASSWORD"] == "secret"


def test_sanitizer_removes_exact_and_pattern_secrets():
    result = sanitize_text("password=alpha token:beta mongodb://user:gamma@host", ["alpha"])
    assert "alpha" not in result
    assert "beta" not in result
    assert "gamma" not in result


def test_nonzero_exit_is_structured(monkeypatch):
    class Failed(FakeProcess):
        def communicate(self, input=None, timeout=None):
            self.returncode = 9
            return "", "password=hunter2 authentication failed"

    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: Failed(args, **kwargs))
    with pytest.raises(BackupError) as error:
        CommandRunner(["hunter2"]).run(["tool"], stage="dump")
    assert error.value.exit_code == 9
    assert "hunter2" not in error.value.sanitized_message
