"""Notification providers whose failures never change backup success."""

from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any

from .config import BackupConfig


class Notifier(ABC):
    @abstractmethod
    def send(self, event: str, payload: dict[str, Any]) -> None: ...


class NoOpNotifier(Notifier):
    def send(self, event: str, payload: dict[str, Any]) -> None:
        return


class EmailNotifier(Notifier):
    def __init__(self, config: BackupConfig):
        self.config = config

    def send(self, event: str, payload: dict[str, Any]) -> None:
        assert self.config.email_smtp is not None
        message = EmailMessage()
        message["From"] = self.config.email_from
        message["To"] = ", ".join(self.config.email_to)
        message["Subject"] = f"[{self.config.environment}] {self.config.database_type}/{self.config.database}: {event}"
        safe_payload = {key: value for key, value in payload.items() if key not in {"error", "command"}}
        message.set_content(json.dumps(safe_payload, ensure_ascii=False, indent=2))
        with smtplib.SMTP(self.config.email_smtp, self.config.email_port, timeout=30) as server:
            if self.config.email_starttls:
                server.starttls(context=ssl.create_default_context())
            if self.config.email_auth_mode == "credentials":
                assert self.config.email_user is not None
                assert self.config.email_password is not None
                server.login(self.config.email_user, self.config.email_password)
            refused = server.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)


class WebhookNotifier(Notifier):
    def __init__(self, url: str):
        self.url = url

    def send(self, event: str, payload: dict[str, Any]) -> None:
        body = json.dumps({"event": event, **payload}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 400:
                raise urllib.error.HTTPError(self.url, response.status, "Webhook failed", response.headers, None)


class RetryingNotifier(Notifier):
    def __init__(self, delegate: Notifier, attempts: int = 3):
        self.delegate = delegate
        self.attempts = attempts

    def send(self, event: str, payload: dict[str, Any]) -> None:
        error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                self.delegate.send(event, payload)
                return
            except Exception as exc:
                error = exc
                if attempt + 1 < self.attempts:
                    time.sleep(2**attempt)
        assert error is not None
        raise error


def create_notifier(config: BackupConfig) -> Notifier:
    if config.notification_provider == "email":
        return RetryingNotifier(EmailNotifier(config))
    if config.notification_provider == "webhook":
        return RetryingNotifier(WebhookNotifier(config.webhook_url or ""))
    return NoOpNotifier()


def notification_error_type(error: Exception) -> str:
    """Return a bounded category, never SMTP response text or recipient details."""
    if isinstance(error, ssl.SSLError):
        return "NotificationTLSFailed"
    if isinstance(error, TimeoutError):
        return "NotificationTimeout"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "NotificationAuthenticationFailed"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "NotificationRecipientsRefused"
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "NotificationCapabilityUnsupported"
    if isinstance(error, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected | ConnectionError):
        return "NotificationConnectionFailed"
    if isinstance(error, smtplib.SMTPException):
        return "NotificationSendFailed"
    if isinstance(error, OSError):
        return "NotificationConnectionFailed"
    return "NotificationFailed"
