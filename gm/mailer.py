"""Email providers and the message log.

Three providers implement the same interface:
  ResendProvider  - real sending through the official Resend Python SDK.
  SmtpProvider    - real sending through any SMTP server.
  SandboxProvider - writes each message to instance/outbox/ and sends nothing. Its messages are stored with
                    status 'simulated' so they can never be mistaken for real deliveries.

A message accepted by Resend is only "accepted": delivery, bounces and complaints arrive later by webhook.
"""
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass

from flask import current_app

from .db import execute, now, query
from .security import get_secret

_resend_lock = threading.Lock()  # the SDK keeps its API key in a module global


@dataclass
class SendResult:
    ok: bool
    provider_id: str = ""
    error: str = ""
    transient: bool = False      # worth retrying later (timeouts, rate limits, 5xx)
    auth_error: bool = False     # the API key is missing, invalid or not allowed to send
    simulated: bool = False


class SandboxProvider:
    name = "sandbox"

    def send(self, payload, idempotency_key):
        outbox = current_app.config["OUTBOX_DIR"]
        os.makedirs(outbox, exist_ok=True)
        msg_id = "sandbox-" + secrets.token_hex(8)
        safe = {k: v for k, v in payload.items() if k != "attachments"}
        safe["_sandbox_notice"] = "SIMULATED: this message was written to the local outbox and was NOT sent."
        safe["_idempotency_key"] = idempotency_key
        with open(os.path.join(outbox, f"{now().replace(':', '').replace(' ', '-')}-{msg_id}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(safe, fh, indent=2)
        capture = current_app.config.get("MAIL_CAPTURE")
        if capture is not None:
            capture.append(dict(safe, id=msg_id))
        return SendResult(ok=True, provider_id=msg_id, simulated=True)

    def test_connection(self):
        return True, "Practice mode: messages are written to the local outbox and never sent."

    def close(self):
        pass

    def domains(self):
        return []


class ResendProvider:
    name = "resend"

    def __init__(self, api_key):
        self.api_key = api_key

    def _call(self, fn):
        import resend
        from resend.http_client_requests import RequestsClient
        with _resend_lock:
            resend.api_key = self.api_key
            resend.default_http_client = RequestsClient(timeout=15)
            try:
                return fn(resend)
            finally:
                resend.api_key = None

    def send(self, payload, idempotency_key):
        import requests
        from resend import exceptions as rex
        try:
            resp = self._call(lambda r: r.Emails.send(payload, {"idempotency_key": idempotency_key[:256]}))
        except (rex.MissingApiKeyError, rex.InvalidApiKeyError) as exc:
            return SendResult(ok=False, error=_clean(exc), auth_error=True)
        except rex.RateLimitError as exc:
            # Daily or monthly quota exhaustion will not clear in seconds, but is still retryable later.
            return SendResult(ok=False, error=_clean(exc), transient=True)
        except rex.ApplicationError as exc:
            return SendResult(ok=False, error=_clean(exc), transient=True)
        except rex.ResendError as exc:
            code = str(getattr(exc, "code", ""))
            return SendResult(ok=False, error=_clean(exc), transient=code.startswith("5") or code in ("409", "429"),
                              auth_error=code in ("401", "403"))
        except (requests.Timeout, requests.ConnectionError) as exc:
            return SendResult(ok=False, error=f"Network problem reaching Resend: {type(exc).__name__}", transient=True)
        except Exception as exc:  # unexpected SDK behaviour: keep the message, retry later
            current_app.logger.exception("Unexpected Resend error")
            return SendResult(ok=False, error=f"Unexpected error: {type(exc).__name__}", transient=True)
        provider_id = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
        if not isinstance(provider_id, str) or not provider_id:
            return SendResult(ok=False, error="Resend returned a response without a message id", transient=True)
        return SendResult(ok=True, provider_id=provider_id)

    def test_connection(self):
        from resend import exceptions as rex
        try:
            resp = self._call(lambda r: r.Domains.list())
        except rex.ResendError as exc:
            msg = _clean(exc)
            if "restricted" in msg.lower():
                return True, "The key is valid but restricted to sending, so domains could not be listed."
            return False, msg
        except Exception as exc:
            return False, f"Could not reach Resend: {type(exc).__name__}"
        data = resp.get("data", []) if isinstance(resp, dict) else []
        names = ", ".join(f"{d.get('name')} ({d.get('status')})" for d in data if isinstance(d, dict)) or "no domains yet"
        return True, f"Connected. Domains: {names}"

    def close(self):
        pass

    def domains(self):
        try:
            resp = self._call(lambda r: r.Domains.list())
        except Exception:
            return None
        data = resp.get("data", []) if isinstance(resp, dict) else []
        return [d for d in data if isinstance(d, dict)]


class SmtpProvider:
    """Any SMTP server (your hosting mailbox, Gmail/Workspace, Outlook, Zoho, Brevo, Mailgun, SES...).
    One connection is reused for a whole sending batch; call close() when done. SMTP has no delivery webhooks,
    so messages stay "accepted"; bounces come back to the sending mailbox as normal emails."""
    name = "smtp"

    def __init__(self, host, port, security, username, password):
        self.host, self.port, self.security = host, int(port or 587), security or "starttls"
        self.username, self.password = username, password
        self._conn = None

    def _connect(self):
        import smtplib
        import ssl
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            conn = smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ctx)
        else:
            conn = smtplib.SMTP(self.host, self.port, timeout=20)
            conn.ehlo()
            if self.security == "starttls":
                conn.starttls(context=ctx)
                conn.ehlo()
        if self.username:
            conn.login(self.username, self.password)
        return conn

    def _message(self, payload):
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid, parseaddr
        msg = EmailMessage()
        msg["From"] = payload["from"]
        msg["To"] = ", ".join(payload["to"])
        msg["Subject"] = payload["subject"]
        msg["Date"] = formatdate(localtime=False)
        domain = parseaddr(payload["from"])[1].rpartition("@")[2] or None
        msg["Message-ID"] = make_msgid(domain=domain)
        if payload.get("reply_to"):
            msg["Reply-To"] = payload["reply_to"]
        for k, v in (payload.get("headers") or {}).items():
            msg[k] = v
        msg.set_content(payload.get("text") or "")
        if payload.get("html"):
            msg.add_alternative(payload["html"], subtype="html")
        return msg

    def send(self, payload, idempotency_key):
        import smtplib
        import socket
        msg = self._message(payload)
        for attempt in (1, 2):  # the server may have dropped an idle reused connection
            try:
                if self._conn is None:
                    self._conn = self._connect()
                self._conn.send_message(msg)
                return SendResult(ok=True, provider_id=msg["Message-ID"].strip("<>"))
            except smtplib.SMTPServerDisconnected:
                self._conn = None
                if attempt == 2:
                    return SendResult(ok=False, error="The SMTP server closed the connection", transient=True)
            except smtplib.SMTPAuthenticationError as exc:
                self.close()
                return SendResult(ok=False, error=f"SMTP login failed: {_smtp_text(exc)}", auth_error=True)
            except smtplib.SMTPRecipientsRefused as exc:
                code, text = next(iter(exc.recipients.values()), (550, b""))
                return SendResult(ok=False, error=f"Recipient refused ({code}): {_smtp_text(text)}",
                                  transient=400 <= code < 500)
            except smtplib.SMTPResponseException as exc:
                self.close()
                return SendResult(ok=False, error=f"SMTP error {exc.smtp_code}: {_smtp_text(exc.smtp_error)}",
                                  transient=400 <= exc.smtp_code < 500)
            except (socket.timeout, OSError, smtplib.SMTPException) as exc:
                self.close()
                return SendResult(ok=False, error=f"Could not reach the SMTP server: {type(exc).__name__}", transient=True)

    def close(self):
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None

    def test_connection(self):
        try:
            self._connect().quit()
        except Exception as exc:
            import smtplib
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                return False, f"The server rejected the username or password: {_smtp_text(exc)}"
            return False, f"Could not connect to {self.host}:{self.port} ({type(exc).__name__}). Check the server, port and security."
        return True, f"Connected and signed in to {self.host}:{self.port}."

    def domains(self):
        return None


def _smtp_text(value):
    value = getattr(value, "smtp_error", value)
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    return " ".join(text.split())[:250]


def _clean(exc):
    text = str(getattr(exc, "message", "") or exc)
    return re.sub(r"re_[A-Za-z0-9_]{6,}", "re_…", text)[:300]  # never echo a key back into logs or the UI


# ---------------------------------------------------------------- provider selection
SERVICES = {"resend": "Resend", "smtp": "SMTP"}


def configured_provider(ws, name):
    """The named service for this workspace, or None if it isn't set up."""
    if name == "resend":
        key = get_secret(ws["id"], "resend_api_key")
        return ResendProvider(key) if key else None
    if name == "smtp" and ws["smtp_host"]:
        return SmtpProvider(ws["smtp_host"], ws["smtp_port"], ws["smtp_security"], ws["smtp_username"],
                            get_secret(ws["id"], "smtp_password"))
    return None


def live_provider(ws):
    """The default service if it's set up, otherwise the other one; None when neither is."""
    default = ws["email_provider"] if ws["email_provider"] in SERVICES else "resend"
    for name in [default] + [n for n in SERVICES if n != default]:
        p = configured_provider(ws, name)
        if p:
            return p
    return None


def workspace_provider(ws):
    override = current_app.config.get("MAIL_PROVIDER_FACTORY")
    if override:
        return override("marketing", ws)
    if ws["sandbox_mode"]:
        return SandboxProvider()
    return live_provider(ws) or SandboxProvider()


def transactional_provider():
    override = current_app.config.get("MAIL_PROVIDER_FACTORY")
    if override:
        return override("transactional", None)
    key = current_app.config["RESEND_API_KEY"] or get_secret(0, "resend_api_key")
    if key:
        return ResendProvider(key)
    # Single-business installs: fall back to the main workspace's own email service for sign-in codes.
    ws = query("SELECT * FROM workspaces ORDER BY id LIMIT 1", one=True)
    return (live_provider(ws) if ws else None) or SandboxProvider()


# ---------------------------------------------------------------- message log
def record_message(*, category, to_email, subject, idempotency_key, kind="", workspace_id=None, campaign_id=None,
                   recipient_id=None, contact_id=None, step_no=None):
    """Create the log row before sending. Returns (id, created). An existing key means it was already handled."""
    existing = query("SELECT id FROM messages WHERE idempotency_key = ?", (idempotency_key,), one=True)
    if existing:
        return existing["id"], False
    mid = execute("INSERT OR IGNORE INTO messages (workspace_id, campaign_id, recipient_id, contact_id, step_no, category, "
                  "kind, to_email, subject, status, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'queued', ?, ?)",
                  (workspace_id, campaign_id, recipient_id, contact_id, step_no, category, kind, to_email, subject[:300],
                   idempotency_key, now()))
    row = query("SELECT id FROM messages WHERE idempotency_key = ?", (idempotency_key,), one=True)
    return row["id"], bool(mid) and row["id"] == mid


RETRY_DELAYS_MIN = [2, 10, 60]  # bounded retries for transient failures


def apply_result(message_id, provider, result):
    """Store the outcome of one send attempt. Returns the new status."""
    msg = query("SELECT attempts FROM messages WHERE id = ?", (message_id,), one=True)
    attempts = msg["attempts"] + 1
    if result.ok:
        status = "simulated" if result.simulated else "accepted"
        execute("UPDATE messages SET status = ?, provider = ?, provider_id = ?, attempts = ?, accepted_at = ?, "
                "simulated = ?, last_error = '', next_attempt_at = NULL WHERE id = ?",
                (status, provider.name, result.provider_id, attempts, now(), int(result.simulated), message_id))
        return status
    if result.transient and attempts <= len(RETRY_DELAYS_MIN):
        delay = RETRY_DELAYS_MIN[attempts - 1]
        execute("UPDATE messages SET provider = ?, attempts = ?, last_error = ?, next_attempt_at = datetime('now', ?) "
                "WHERE id = ?", (provider.name, attempts, result.error, f"+{delay} minutes", message_id))
        return "queued"
    execute("UPDATE messages SET status = 'failed', provider = ?, attempts = ?, last_error = ?, next_attempt_at = NULL "
            "WHERE id = ?", (provider.name, attempts, result.error or "Permanent failure", message_id))
    return "failed"
