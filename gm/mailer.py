"""Email providers and the message log.

Two providers implement the same interface:
  ResendProvider  - real sending through the official Resend Python SDK.
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
        return True, "Sandbox mode: messages are written to the local outbox and never sent."

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

    def domains(self):
        try:
            resp = self._call(lambda r: r.Domains.list())
        except Exception:
            return None
        data = resp.get("data", []) if isinstance(resp, dict) else []
        return [d for d in data if isinstance(d, dict)]


def _clean(exc):
    text = str(getattr(exc, "message", "") or exc)
    return re.sub(r"re_[A-Za-z0-9_]{6,}", "re_…", text)[:300]  # never echo a key back into logs or the UI


# ---------------------------------------------------------------- provider selection
def workspace_provider(ws):
    override = current_app.config.get("MAIL_PROVIDER_FACTORY")
    if override:
        return override("marketing", ws)
    if ws["sandbox_mode"]:
        return SandboxProvider()
    key = get_secret(ws["id"], "resend_api_key")
    return ResendProvider(key) if key else SandboxProvider()


def transactional_provider():
    override = current_app.config.get("MAIL_PROVIDER_FACTORY")
    if override:
        return override("transactional", None)
    key = current_app.config["RESEND_API_KEY"] or get_secret(0, "resend_api_key")
    return ResendProvider(key) if key else SandboxProvider()


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
