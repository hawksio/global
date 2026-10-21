"""Roles and permissions, server-side sessions, CSRF, rate limiting, TOTP, secret encryption and audit logging.

Sessions are rows in user_sessions; the signed cookie carries only a random session id, so signing out,
password changes and "sign out everywhere" really end sessions.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session, url_for

from .db import execute, now, query

# ---------------------------------------------------------------- roles and permissions
ROLES = {
    "owner": "Owner",
    "admin": "Administrator",
    "marketer": "Marketer",
    "analyst": "Analyst (read only)",
}
ALL = set(ROLES)
EDITORS = {"owner", "admin", "marketer"}
MANAGERS = {"owner", "admin"}
PERMISSIONS = {
    "view_contacts": ALL,
    "edit_contacts": EDITORS,
    "delete_contacts": MANAGERS | {"marketer"},
    "export_contacts": MANAGERS | {"marketer"},
    "discover": EDITORS,
    "verify": EDITORS,
    "use_ai": EDITORS,
    "view_templates": ALL,
    "edit_templates": EDITORS,
    "view_campaigns": ALL,
    "edit_campaigns": EDITORS,
    "approve_campaigns": MANAGERS,
    "view_analytics": ALL,
    "view_deliverability": ALL,
    "manage_integrations": MANAGERS,
    "manage_settings": MANAGERS,
    "manage_users": MANAGERS,
    "view_audit": MANAGERS,
    "transactional": MANAGERS,
}


def can(permission, user=None):
    user = user if user is not None else current_user()
    if not user or not user["workspace_id"]:
        return False
    return user["role"] in PERMISSIONS[permission]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        return view(*args, **kwargs)
    return wrapped


def require(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
            if not can(permission, user):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def superadmin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login"))
        if not user["is_superadmin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def ws_id():
    """The signed-in user's workspace id. Every data query is scoped by it."""
    user = current_user()
    if user is None or not user["workspace_id"]:
        abort(403)
    return user["workspace_id"]


def workspace():
    if "workspace" not in g:
        user = current_user()
        g.workspace = (query("SELECT * FROM workspaces WHERE id = ?", (user["workspace_id"],), one=True)
                       if user and user["workspace_id"] else None)
    return g.workspace


# ---------------------------------------------------------------- sessions
def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _ts(seconds_ago):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds_ago))


def current_session():
    if "user_session" not in g:
        sid = session.get("sid")
        row = None
        if sid:
            idle = current_app.config["SESSION_IDLE_MINUTES"] * 60
            row = query("SELECT * FROM user_sessions WHERE sid_hash = ? AND revoked_at IS NULL AND expires_at > ? "
                        "AND last_seen_at > ?", (sha256(sid), now(), _ts(idle)), one=True)
            if row is None:
                session.pop("sid", None)
            elif row["last_seen_at"] < _ts(60):
                execute("UPDATE user_sessions SET last_seen_at = ? WHERE id = ?", (now(), row["id"]))
        g.user_session = row
    return g.user_session


def current_user():
    if "user" not in g:
        row = current_session()
        g.user = query("SELECT * FROM users WHERE id = ? AND is_active = 1", (row["user_id"],), one=True) if row else None
    return g.user


def start_session(user):
    session.clear()
    sid = secrets.token_urlsafe(32)
    hours = current_app.config["SESSION_HOURS"]
    execute("INSERT INTO user_sessions (user_id, sid_hash, created_at, last_seen_at, expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, datetime('now', ?), ?, ?)",
            (user["id"], sha256(sid), now(), now(), f"+{hours} hours", client_ip(),
             (request.user_agent.string or "")[:200]))
    session["sid"] = sid
    session.permanent = True
    execute("UPDATE users SET last_login_at = ?, failed_logins = 0, locked_until = NULL WHERE id = ?", (now(), user["id"]))
    g.pop("user", None)
    g.pop("user_session", None)


def end_session():
    row = current_session()
    if row:
        execute("UPDATE user_sessions SET revoked_at = ? WHERE id = ?", (now(), row["id"]))
    session.clear()


def revoke_all_sessions(user_id, except_current=False):
    current = current_session() if except_current else None
    if current:
        execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND id != ?",
                (now(), user_id, current["id"]))
    else:
        execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now(), user_id))


def client_ip():
    return request.remote_addr or ""


# ---------------------------------------------------------------- CSRF
CSRF_EXEMPT_PREFIXES = ("/webhooks/", "/u/", "/healthz")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.path.startswith(CSRF_EXEMPT_PREFIXES):
        return None
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="Your form expired or was sent from another site. Reload the page and try again.")
    return None


# ---------------------------------------------------------------- rate limiting
def rate_limited(key, limit, window_seconds):
    """Fixed-window counter. Returns True when the call should be refused (and counts it either way)."""
    bucket = int(time.time() // window_seconds)
    full = f"{key}:{window_seconds}"
    row = query("SELECT window_start, count FROM rate_limits WHERE key = ?", (full,), one=True)
    if row is None or row["window_start"] != bucket:
        execute("INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET window_start = excluded.window_start, count = 1", (full, bucket))
        return False
    execute("UPDATE rate_limits SET count = count + 1 WHERE key = ?", (full,))
    return row["count"] + 1 > limit


def throttle(key, limit, window_seconds, message="Too many attempts. Please wait a few minutes and try again."):
    if rate_limited(key, limit, window_seconds):
        flash(message, "error")
        return True
    return False


# ---------------------------------------------------------------- TOTP (RFC 6238)
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret, at=None):
    return _hotp(secret, int((at or time.time()) // 30))


def verify_totp(secret, code, at=None):
    code = (code or "").replace(" ", "")
    if not (secret and code.isdigit() and len(code) == 6):
        return False
    counter = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret, counter + d), code) for d in (-1, 0, 1))


def totp_uri(secret, email, issuer="Global Marketing"):
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}"


# ---------------------------------------------------------------- secret encryption
def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(current_app.config["ENCRYPTION_KEY"].encode())


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token):
    if not token:
        return ""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        current_app.logger.error("A stored secret could not be decrypted (was GM_ENCRYPTION_KEY changed?)")
        return ""


def set_secret(workspace_id, name, value, user_id=None):
    if not value:
        execute("DELETE FROM secrets WHERE workspace_id = ? AND name = ?", (workspace_id, name))
        return
    hint = ("…" + value[-4:]) if len(value) > 8 else "set"
    execute("INSERT INTO secrets (workspace_id, name, value_enc, hint, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workspace_id, name) DO UPDATE SET value_enc = excluded.value_enc, hint = excluded.hint, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (workspace_id, name, encrypt(value), hint, now(), user_id))


def get_secret(workspace_id, name):
    row = query("SELECT value_enc FROM secrets WHERE workspace_id = ? AND name = ?", (workspace_id, name), one=True)
    return decrypt(row["value_enc"]) if row else ""


def secret_hint(workspace_id, name):
    row = query("SELECT hint, updated_at FROM secrets WHERE workspace_id = ? AND name = ?", (workspace_id, name), one=True)
    return row


# ---------------------------------------------------------------- audit
def audit(action, target="", detail="", workspace_id=None, user_id=None):
    user = current_user() if user_id is None and _has_request() else None
    execute("INSERT INTO audit_log (workspace_id, user_id, action, target, detail, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id if workspace_id is not None else (user["workspace_id"] if user else None),
             user_id if user_id is not None else (user["id"] if user else None),
             action, str(target)[:200], str(detail)[:1000], client_ip() if _has_request() else "", now()))


def _has_request():
    from flask import has_request_context
    return has_request_context()
