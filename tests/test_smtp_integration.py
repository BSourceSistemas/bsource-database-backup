"""Real loopback SMTP conversations: verified STARTTLS, AUTH and relay without AUTH."""

from __future__ import annotations

import logging
import socket
import ssl
import subprocess

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from app.notifier import EmailNotifier

from .test_email import email_config


@pytest.fixture
def smtp_server(tmp_path, monkeypatch):
    # The test SMTP server runs on a background thread beyond pytest capture boundaries.
    monkeypatch.setattr(logging.getLogger("mail.log"), "disabled", True)
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    controllers = []

    def start(auth):
        messages = []
        authentications = []

        class Handler:
            async def handle_DATA(self, server, session, envelope):
                messages.append(envelope)
                return "250 accepted"

        def authenticate(server, session, envelope, mechanism, auth_data):
            authentications.append(mechanism)
            valid = auth_data.login == b"user" and auth_data.password == b"secret"
            return AuthResult(success=valid, handled=False)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        controller = Controller(
            Handler(),
            hostname="127.0.0.1",
            port=port,
            tls_context=context,
            require_starttls=True,
            auth_required=auth,
            auth_exclude_mechanism=[] if auth else ["LOGIN", "PLAIN"],
            authenticator=authenticate,
        )
        controller.start()
        controllers.append(controller)
        return port, messages, authentications

    yield start, cert
    for controller in controllers:
        controller.stop()


@pytest.mark.parametrize("auth", [False, True])
def test_real_smtp_verified_tls(config_factory, monkeypatch, smtp_server, auth):
    start, cert = smtp_server
    port, messages, authentications = start(auth)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    config = email_config(
        config_factory,
        email_port=port,
        email_auth_mode="credentials" if auth else "none",
        email_user="user" if auth else None,
        email_password="secret" if auth else None,
    )
    EmailNotifier(config).send("backup-success", {"runId": "integration"})
    assert len(messages) == 1
    assert len(messages[0].rcpt_tos) == 2
    assert bool(authentications) == auth


def test_real_untrusted_tls_is_rejected(config_factory, smtp_server):
    start, _cert = smtp_server
    port, messages, authentications = start(False)
    config = email_config(config_factory, email_port=port, email_auth_mode="none")
    with pytest.raises(ssl.SSLCertVerificationError):
        EmailNotifier(config).send("backup-success", {})
    assert messages == []
    assert authentications == []
