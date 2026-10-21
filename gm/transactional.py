"""Transactional email: verification codes, sign-in codes, password resets and account notices.

Kept apart from marketing: its own sender address and API key (GM_TRANSACTIONAL_FROM / GM_RESEND_API_KEY),
its own templates, category 'transactional' in the message log, and no unsubscribe footer. Message bodies
are never stored, so codes and reset links never land in the database or the logs.

Codes are 6 digits from the `secrets` module, stored as HMAC hashes, single use, short-lived, limited to a few
attempts and subject to a resend cooldown.
"""
import hashlib
import hmac
import secrets

from flask import current_app, render_template

from . import mailer
from .db import execute, now, parse_ts, query

CODE_TTL_MINUTES = {"verify_email": 30, "login": 10, "reset": 30}
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60


def _hash(user_id, purpose, secret):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{user_id}:{purpose}:{secret}".encode(), hashlib.sha256).hexdigest()


def cooldown_remaining(user_id, purpose):
    row = query("SELECT created_at FROM auth_codes WHERE user_id = ? AND purpose = ? ORDER BY id DESC LIMIT 1",
                (user_id, purpose), one=True)
    if not row:
        return 0
    from datetime import datetime, timezone
    age = (datetime.now(timezone.utc) - parse_ts(row["created_at"])).total_seconds()
    return max(0, int(RESEND_COOLDOWN_SECONDS - age))


def _issue(user_id, purpose, secret, minutes=None):
    # A new code replaces any earlier unused one for the same purpose.
    execute("UPDATE auth_codes SET used_at = ? WHERE user_id = ? AND purpose = ? AND used_at IS NULL", (now(), user_id, purpose))
    execute("INSERT INTO auth_codes (user_id, purpose, secret_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, datetime('now', ?))",
            (user_id, purpose, _hash(user_id, purpose, secret), now(),
             f"+{minutes or CODE_TTL_MINUTES[purpose]} minutes"))


def new_code(user_id, purpose):
    code = f"{secrets.randbelow(1_000_000):06d}"
    _issue(user_id, purpose, code)
    return code


def new_reset_token(user_id, minutes=None):
    """Password-reset (and invitation) links: 256-bit random token, single use."""
    token = secrets.token_urlsafe(32)
    _issue(user_id, "reset", token, minutes)
    return token


def check(user_id, purpose, secret):
    """True once for a valid, unexpired, unused secret. Wrong guesses count towards MAX_ATTEMPTS."""
    row = query("SELECT * FROM auth_codes WHERE user_id = ? AND purpose = ? AND used_at IS NULL AND expires_at > ? "
                "ORDER BY id DESC LIMIT 1", (user_id, purpose, now()), one=True)
    if row is None or row["attempts"] >= MAX_ATTEMPTS:
        return False
    if hmac.compare_digest(row["secret_hash"], _hash(user_id, purpose, (secret or "").strip())):
        execute("UPDATE auth_codes SET used_at = ? WHERE id = ? AND used_at IS NULL", (now(), row["id"]))
        return True
    execute("UPDATE auth_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
    return False


def peek(user_id, purpose, secret):
    """Whether a secret is currently valid, without using it up (a reset form is shown before it is submitted)."""
    row = query("SELECT secret_hash, attempts FROM auth_codes WHERE user_id = ? AND purpose = ? AND used_at IS NULL "
                "AND expires_at > ? ORDER BY id DESC LIMIT 1", (user_id, purpose, now()), one=True)
    return bool(row and row["attempts"] < MAX_ATTEMPTS
                and hmac.compare_digest(row["secret_hash"], _hash(user_id, purpose, (secret or "").strip())))


# ---------------------------------------------------------------- sending
def send(to, subject, template, kind, user_id=None, workspace_id=None, **context):
    """Send one transactional email now. Returns the message status ('accepted', 'simulated' or 'failed')."""
    context.setdefault("base_url", current_app.config["BASE_URL"])
    html = render_template(f"email/{template}.html", subject=subject, **context)
    text = render_template(f"email/{template}.txt", subject=subject, **context)
    key = f"tx-{kind}-{secrets.token_hex(12)}"
    mid, _ = mailer.record_message(category="transactional", kind=kind, to_email=to, subject=subject,
                                   idempotency_key=key, workspace_id=workspace_id)
    payload = {"from": current_app.config["TRANSACTIONAL_FROM"], "to": [to], "subject": subject, "html": html,
               "text": text, "tags": [{"name": "category", "value": "transactional"}, {"name": "kind", "value": kind}]}
    if current_app.config["TRANSACTIONAL_REPLY_TO"]:
        payload["reply_to"] = current_app.config["TRANSACTIONAL_REPLY_TO"]
    provider = mailer.transactional_provider()
    result = provider.send(payload, key)
    if not result.ok:
        # Bodies are not stored, so there is nothing to retry later; the person can ask for a new email.
        result.transient = False
        current_app.logger.warning("Transactional email (%s) not sent: %s", kind, result.error)
    return mailer.apply_result(mid, provider, result)


def send_verification_code(user):
    code = new_code(user["id"], "verify_email")
    return send(user["email"], "Your Global Marketing verification code", "verify_code", "verify_email",
                user_id=user["id"], name=user["full_name"], code=code, minutes=CODE_TTL_MINUTES["verify_email"])


def send_login_code(user):
    code = new_code(user["id"], "login")
    return send(user["email"], "Your Global Marketing sign-in code", "login_code", "login_code",
                user_id=user["id"], name=user["full_name"], code=code, minutes=CODE_TTL_MINUTES["login"])


def send_password_reset(user, link):
    return send(user["email"], "Reset your Global Marketing password", "password_reset", "password_reset",
                user_id=user["id"], name=user["full_name"], link=link, minutes=CODE_TTL_MINUTES["reset"])


def send_security_alert(user, what):
    return send(user["email"], "Security notice for your Global Marketing account", "security_alert", "security_alert",
                user_id=user["id"], name=user["full_name"], what=what, when=now() + " UTC")


def send_invite(user, inviter, link):
    return send(user["email"], f"{inviter['full_name']} invited you to Global Marketing", "invite", "invite",
                user_id=user["id"], name=user["full_name"], inviter=inviter["full_name"], link=link)


def send_admin_alert(workspace_id, subject, body):
    """Campaign administration alerts (automatic pauses, failures) to the workspace's owners and admins."""
    for admin in query("SELECT * FROM users WHERE workspace_id = ? AND role IN ('owner', 'admin') AND is_active = 1",
                       (workspace_id,)):
        send(admin["email"], subject, "admin_alert", "admin_alert", user_id=admin["id"], workspace_id=workspace_id,
             name=admin["full_name"], body=body)
