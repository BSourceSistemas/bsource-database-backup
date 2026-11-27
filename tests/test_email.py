from __future__ import annotations

import smtplib
import ssl
from unittest.mock import MagicMock

import pytest

from app.config import load_config
from app.errors import BackupError
from app.notifier import EmailNotifier, RetryingNotifier, notification_error_type

from .test_config import base_env


def email_config(config_factory, **overrides):
    return config_factory(
        notification_provider="email",
        email_from="sender@example.test",
        email_to=("one@example.test", "two@example.test"),
        email_smtp="localhost",
        **overrides,
    )


@pytest.mark.parametrize("mode", ["credentials", "none"])
def test_smtp_auth_tls_and_timeout(config_factory, monkeypatch, mode):
    config = email_config(config_factory, email_auth_mode=mode, email_user="user", email_password="secret")
    smtp = MagicMock()
    server = smtp.return_value.__enter__.return_value
    server.send_message.return_value = {}
    monkeypatch.setattr("app.notifier.smtplib.SMTP", smtp)
    EmailNotifier(config).send("backup-success", {})
    smtp.assert_called_once_with("localhost", 587, timeout=30)
    context = server.starttls.call_args.kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    if mode == "credentials":
        server.login.assert_called_once_with("user", "secret")
    else:
        server.login.assert_not_called()
    server.send_message.assert_called_once()


def test_no_credentials_or_starttls(config_factory, monkeypatch):
    config = email_config(config_factory, email_auth_mode="none", email_starttls=False)
    smtp = MagicMock()
    server = smtp.return_value.__enter__.return_value
    server.send_message.return_value = {}
    monkeypatch.setattr("app.notifier.smtplib.SMTP", smtp)
    EmailNotifier(config).send("backup-success", {})
    server.starttls.assert_not_called()
    server.login.assert_not_called()
    server.send_message.assert_called_once()


@pytest.mark.parametrize(
    ("operation", "error", "category"),
    [
        ("connect", ConnectionRefusedError("sensitive"), "NotificationConnectionFailed"),
        ("connect", TimeoutError("sensitive"), "NotificationTimeout"),
        ("starttls", ssl.SSLCertVerificationError("sensitive"), "NotificationTLSFailed"),
        ("starttls", smtplib.SMTPNotSupportedError("sensitive"), "NotificationCapabilityUnsupported"),
        ("login", smtplib.SMTPAuthenticationError(535, b"sensitive"), "NotificationAuthenticationFailed"),
        (
            "send_message",
            smtplib.SMTPRecipientsRefused({"private": (550, b"sensitive")}),
            "NotificationRecipientsRefused",
        ),
        ("send_message", smtplib.SMTPDataError(554, b"sensitive"), "NotificationSendFailed"),
    ],
)
def test_smtp_errors_propagate_after_retries(config_factory, monkeypatch, operation, error, category):
    config = email_config(config_factory, email_user="user", email_password="secret")
    smtp = MagicMock()
    server = smtp.return_value.__enter__.return_value
    server.send_message.return_value = {}
    failed = smtp if operation == "connect" else getattr(server, operation)
    failed.side_effect = error
    monkeypatch.setattr("app.notifier.smtplib.SMTP", smtp)
    monkeypatch.setattr("app.notifier.time.sleep", lambda _: None)
    with pytest.raises(type(error)) as caught:
        RetryingNotifier(EmailNotifier(config)).send("backup-success", {})
    assert caught.value is error
    assert failed.call_count == 3
    assert notification_error_type(error) == category
    if operation in {"connect", "starttls", "login"}:
        server.send_message.assert_not_called()


def test_partial_recipient_refusal_is_failure(config_factory, monkeypatch):
    config = email_config(config_factory, email_auth_mode="none")
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value.send_message.return_value = {"two@example.test": (550, b"no")}
    monkeypatch.setattr("app.notifier.smtplib.SMTP", smtp)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        EmailNotifier(config).send("backup-success", {})


def smtp_env(tmp_path):
    return {
        **base_env(tmp_path),
        "EMAIL_FROM": "from@example.test",
        "EMAIL_TO": "to@example.test",
        "EMAIL_SMTP": "smtp",
    }


def test_default_requires_credentials_for_explicit_email(tmp_path):
    env = {**smtp_env(tmp_path), "NOTIFICATION_PROVIDER": "email"}
    with pytest.raises(BackupError, match="EMAIL_USER and EMAIL_PASSWORD"):
        load_config(env)
    env.update(EMAIL_USER="user", EMAIL_PASSWORD="secret")
    assert load_config(env).email_auth_mode == "credentials"


@pytest.mark.parametrize("provider", [None, "email", "noop"])
def test_none_mode_provider_inference_and_precedence(tmp_path, provider):
    env = {**smtp_env(tmp_path), "EMAIL_AUTH_MODE": " NONE "}
    if provider:
        env["NOTIFICATION_PROVIDER"] = provider
    config = load_config(env)
    assert config.notification_provider == (provider or "email")
    assert config.email_user is None
    assert config.email_password is None


@pytest.mark.parametrize("name", ["EMAIL_FROM", "EMAIL_TO", "EMAIL_SMTP"])
def test_none_mode_requires_envelope_and_host(tmp_path, name):
    env = {**smtp_env(tmp_path), "EMAIL_AUTH_MODE": "none", "NOTIFICATION_PROVIDER": "email"}
    env.pop(name)
    with pytest.raises(BackupError):
        load_config(env)


@pytest.mark.parametrize("provider", ["email", "noop"])
def test_invalid_auth_mode_rejected(tmp_path, provider):
    with pytest.raises(BackupError, match="EMAIL_AUTH_MODE"):
        load_config({**smtp_env(tmp_path), "EMAIL_AUTH_MODE": "anonymous", "NOTIFICATION_PROVIDER": provider})


def test_smtp_password_file_and_legacy_inference(tmp_path):
    env = smtp_env(tmp_path)
    assert load_config(env).notification_provider == "noop"
    secret = tmp_path / "smtp-password"
    secret.write_text("mail-secret\n")
    env.update(EMAIL_USER="user", SMTP_PASSWORD_FILE=str(secret))
    config = load_config(env)
    assert config.notification_provider == "email"
    assert config.email_password == "mail-secret"


@pytest.mark.parametrize("missing", ["EMAIL_USER", "EMAIL_PASSWORD"])
def test_credentials_mode_requires_each_credential(tmp_path, missing):
    env = {**smtp_env(tmp_path), "NOTIFICATION_PROVIDER": "email", "EMAIL_USER": "user", "EMAIL_PASSWORD": "secret"}
    env.pop(missing)
    with pytest.raises(BackupError, match="EMAIL_USER and EMAIL_PASSWORD"):
        load_config(env)
