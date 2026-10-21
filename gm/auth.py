"""Registration, email verification, sign-in (with optional authenticator or email codes), password reset,
invitations, account security, account export and deletion.

Answers never reveal whether an email address has an account.
"""
import io
import json
import secrets
import time

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import transactional
from .db import execute, now, query, transaction
from .security import (audit, client_ip, current_session, current_user, decrypt, encrypt, end_session, login_required,
                       new_totp_secret, rate_limited, revoke_all_sessions, start_session, throttle, totp_uri,
                       verify_totp)

bp = Blueprint("auth", __name__)

COMMON_PASSWORDS = {"password", "password123", "123456789", "1234567890", "qwerty123", "letmein123", "welcome123",
                    "iloveyou", "admin123", "passw0rd", "123456789012", "qwertyuiop", "password1234"}
PENDING_SECONDS = 600
LOCK_AFTER = 10
LOCK_MINUTES = 15


def password_problem(password, email=""):
    if len(password) < 12:
        return "Use at least 12 characters."
    if len(password) > 200:
        return "Use at most 200 characters."
    if password.lower() in COMMON_PASSWORDS:
        return "That password is too common. Choose something less predictable."
    local = email.split("@")[0].lower()
    if local and len(local) >= 4 and local in password.lower():
        return "Don't include your email address in the password."
    return None


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _pending(key):
    data = session.get(key)
    if not data or time.time() - data.get("t", 0) > PENDING_SECONDS:
        session.pop(key, None)
        return None
    user = query("SELECT * FROM users WHERE id = ? AND is_active = 1", (data["uid"],), one=True)
    return user


def _set_pending(key, user, **extra):
    session[key] = dict(uid=user["id"], t=time.time(), **extra)


def create_workspace(name, owner_email, owner_name, password_hash, verified=False, superadmin=False):
    with transaction():
        ws_id = execute("INSERT INTO workspaces (public_id, name, company_name, sender_name, created_at) VALUES (?,?,?,?,?)",
                        (secrets.token_hex(8), name, name, name, now()))
        uid = execute("INSERT INTO users (workspace_id, email, password_hash, full_name, role, is_superadmin, "
                      "email_verified_at, created_at) VALUES (?, ?, ?, ?, 'owner', ?, ?, ?)",
                      (ws_id, owner_email.strip().lower(), password_hash, owner_name, int(superadmin),
                       now() if verified else None, now()))
    return ws_id, uid


# ---------------------------------------------------------------- registration and verification
@bp.route("/register", methods=["GET", "POST"])
def register():
    if not current_app.config["ALLOW_SIGNUP"]:
        return render_template("auth/signup_closed.html"), 403
    if current_user():
        return redirect(url_for("main.overview"))
    form = request.form
    if request.method == "POST":
        if throttle(f"register:{client_ip()}", 5, 3600):
            return render_template("auth/register.html", form=form), 429
        email = form.get("email", "").strip().lower()
        name = form.get("full_name", "").strip()[:120]
        company = form.get("company", "").strip()[:120]
        password = form.get("password", "")
        errors = []
        from .validation import syntax_check
        if not syntax_check(email)[0]:
            errors.append("Enter a valid email address.")
        if not name:
            errors.append("Enter your name.")
        if not company:
            errors.append("Enter your company or workspace name.")
        problem = password_problem(password, email)
        if problem:
            errors.append(problem)
        if form.get("terms") != "on":
            errors.append("Please accept the acceptable-use terms.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register.html", form=form), 400
        existing = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if existing:
            # Same answer as a new sign-up; the owner of the address gets a notice instead of a code.
            if not rate_limited(f"exists-notice:{email}", 1, 3600):
                transactional.send_security_alert(existing, "Someone tried to create a new account with this email address. "
                                                            "If it was you, sign in or reset your password instead.")
            session["verify_hint"] = email
            return redirect(url_for("auth.verify"))
        _, uid = create_workspace(company, email, name, generate_password_hash(password))
        user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True)
        transactional.send_verification_code(user)
        audit("account.register", target=email, workspace_id=user["workspace_id"], user_id=uid)
        _set_pending("verify", user)
        session["verify_hint"] = email
        return redirect(url_for("auth.verify"))
    return render_template("auth/register.html", form=form)


@bp.route("/verify", methods=["GET", "POST"])
def verify():
    user = _pending("verify")
    hint = session.get("verify_hint", "")
    if request.method == "POST":
        if throttle(f"verify:{client_ip()}", 20, 900):
            return render_template("auth/verify.html", hint=hint), 429
        if request.form.get("action") == "resend":
            if user:
                wait = transactional.cooldown_remaining(user["id"], "verify_email")
                if wait:
                    flash(f"Please wait {wait} seconds before asking for another code.", "error")
                else:
                    transactional.send_verification_code(user)
                    flash("If the address is waiting for verification, a new code is on its way.", "success")
            else:
                flash("If the address is waiting for verification, a new code is on its way.", "success")
            return redirect(url_for("auth.verify"))
        if user and transactional.check(user["id"], "verify_email", request.form.get("code", "")):
            execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (now(), user["id"]))
            session.pop("verify", None)
            audit("account.verified", target=user["email"], workspace_id=user["workspace_id"], user_id=user["id"])
            start_session(user)
            flash("Your email address is verified. Welcome to Global Marketing!", "success")
            return redirect(url_for("main.onboarding"))
        flash("That code is not valid or has expired. Check the latest email or ask for a new code.", "error")
        return render_template("auth/verify.html", hint=hint), 400
    return render_template("auth/verify.html", hint=hint)


# ---------------------------------------------------------------- sign-in
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("main.overview"))
    nxt = _safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if throttle(f"login-ip:{client_ip()}", 30, 900) or throttle(f"login-email:{email}", 10, 900):
            return render_template("auth/login.html", email=email, next=nxt), 429
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        password = request.form.get("password", "")
        locked = user and user["locked_until"] and user["locked_until"] > now()
        ok = bool(user) and user["is_active"] and not locked and check_password_hash(user["password_hash"], password)
        if not user:
            check_password_hash(generate_password_hash("timing-equaliser"), password)
        if not ok:
            if user and not locked:
                fails = user["failed_logins"] + 1
                execute("UPDATE users SET failed_logins = ?, locked_until = CASE WHEN ? >= ? THEN datetime('now', ?) "
                        "ELSE locked_until END WHERE id = ?", (fails, fails, LOCK_AFTER, f"+{LOCK_MINUTES} minutes", user["id"]))
                if fails == LOCK_AFTER:
                    audit("account.locked", target=email, workspace_id=user["workspace_id"], user_id=user["id"])
                    transactional.send_security_alert(user, "Your account was locked for 15 minutes after repeated failed sign-in attempts.")
            flash("Email or password not recognised.", "error")
            return render_template("auth/login.html", email=email, next=nxt), 401
        if not user["email_verified_at"]:
            if not transactional.cooldown_remaining(user["id"], "verify_email"):
                transactional.send_verification_code(user)
            _set_pending("verify", user)
            session["verify_hint"] = email
            flash("Please verify your email address first. We've sent you a code.", "info")
            return redirect(url_for("auth.verify"))
        if user["mfa_enabled"]:
            _set_pending("mfa", user, next=nxt)
            return redirect(url_for("auth.login_mfa"))
        ws = query("SELECT email_login_codes FROM workspaces WHERE id = ?", (user["workspace_id"],), one=True)
        if user["email_codes"] or (ws and ws["email_login_codes"]):
            transactional.send_login_code(user)
            _set_pending("code", user, next=nxt)
            return redirect(url_for("auth.login_code"))
        return _finish(user, nxt)
    return render_template("auth/login.html", email="", next=nxt)


def _finish(user, nxt, method="password"):
    start_session(user)
    audit("account.login", detail=method, workspace_id=user["workspace_id"], user_id=user["id"])
    if user["is_superadmin"] and not user["workspace_id"]:
        return redirect(url_for("admin.system"))
    return redirect(nxt or url_for("main.overview"))


@bp.route("/login/2fa", methods=["GET", "POST"])
def login_mfa():
    user = _pending("mfa")
    if user is None:
        flash("Please sign in again.", "info")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if throttle(f"mfa:{user['id']}", 8, 900):
            return render_template("auth/login_code.html", mode="totp"), 429
        if verify_totp(decrypt(user["mfa_secret_enc"]), request.form.get("code", "")):
            nxt = session.pop("mfa", {}).get("next")
            return _finish(user, nxt, "password+totp")
        flash("That code didn't match. Use the current 6-digit code from your authenticator app.", "error")
        return render_template("auth/login_code.html", mode="totp"), 400
    return render_template("auth/login_code.html", mode="totp")


@bp.route("/login/code", methods=["GET", "POST"])
def login_code():
    user = _pending("code")
    if user is None:
        flash("Please sign in again.", "info")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if request.form.get("action") == "resend":
            wait = transactional.cooldown_remaining(user["id"], "login")
            if wait:
                flash(f"Please wait {wait} seconds before asking for another code.", "error")
            else:
                transactional.send_login_code(user)
                flash("A new code is on its way.", "success")
            return redirect(url_for("auth.login_code"))
        if transactional.check(user["id"], "login", request.form.get("code", "")):
            nxt = session.pop("code", {}).get("next")
            return _finish(user, nxt, "password+email_code")
        flash("That code is not valid or has expired.", "error")
        return render_template("auth/login_code.html", mode="email"), 400
    return render_template("auth/login_code.html", mode="email")


@bp.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user:
        audit("account.logout")
    end_session()
    flash("You have signed out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- password reset and invitations
@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if throttle(f"forgot-ip:{client_ip()}", 10, 3600):
            return render_template("auth/forgot.html"), 429
        user = query("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,), one=True)
        if user and not rate_limited(f"forgot-email:{email}", 3, 3600):
            token = transactional.new_reset_token(user["id"])
            link = current_app.config["BASE_URL"] + url_for("auth.reset", uid=user["id"], token=token)
            transactional.send_password_reset(user, link)
            audit("account.reset_requested", workspace_id=user["workspace_id"], user_id=user["id"])
        flash("If that address has an account, we've emailed a reset link. It works once and expires in 30 minutes.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reset/<int:uid>/<token>", methods=["GET", "POST"])
def reset(uid, token):
    user = query("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,), one=True)
    if user is None or not transactional.peek(uid, "reset", token):
        return render_template("auth/link_invalid.html"), 400
    invited = request.args.get("invite") == "1" or not user["last_login_at"] and not user["email_verified_at"]
    if request.method == "POST":
        password = request.form.get("password", "")
        problem = password_problem(password, user["email"])
        if problem:
            flash(problem, "error")
            return render_template("auth/reset.html", user=user, invited=invited), 400
        if not transactional.check(uid, "reset", token):
            return render_template("auth/link_invalid.html"), 400
        execute("UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL, "
                "email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
                (generate_password_hash(password), now(), uid))
        revoke_all_sessions(uid)
        audit("account.password_reset", workspace_id=user["workspace_id"], user_id=uid)
        if not invited:
            transactional.send_security_alert(user, "Your password was changed using a reset link.")
        flash("Your password is set. Please sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", user=user, invited=invited)


# ---------------------------------------------------------------- account security
@bp.route("/account/security", methods=["GET"])
@login_required
def security():
    user = current_user()
    sessions = query("SELECT * FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? "
                     "ORDER BY last_seen_at DESC", (user["id"], now()))
    setup_secret = session.get("mfa_setup")
    qr_svg = None
    if setup_secret:
        import segno
        buf = io.BytesIO()
        segno.make(totp_uri(setup_secret, user["email"]), error="m").save(buf, kind="svg", scale=4, border=2)
        qr_svg = buf.getvalue().decode()
    return render_template("auth/security.html", sessions=sessions, current=current_session(),
                           setup_secret=setup_secret, qr_svg=qr_svg)


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    user = current_user()
    if throttle(f"pwchange:{user['id']}", 10, 3600):
        return redirect(url_for("auth.security"))
    if not check_password_hash(user["password_hash"], request.form.get("current", "")):
        flash("Your current password is not correct.", "error")
        return redirect(url_for("auth.security"))
    new = request.form.get("password", "")
    problem = password_problem(new, user["email"])
    if problem:
        flash(problem, "error")
        return redirect(url_for("auth.security"))
    execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
    revoke_all_sessions(user["id"], except_current=True)
    audit("account.password_changed")
    transactional.send_security_alert(user, "Your password was changed. Other devices were signed out.")
    flash("Password changed. Other devices have been signed out.", "success")
    return redirect(url_for("auth.security"))


@bp.route("/account/mfa/start", methods=["POST"])
@login_required
def mfa_start():
    session["mfa_setup"] = new_totp_secret()
    return redirect(url_for("auth.security") + "#mfa")


@bp.route("/account/mfa/confirm", methods=["POST"])
@login_required
def mfa_confirm():
    user = current_user()
    secret = session.get("mfa_setup")
    if not secret or not verify_totp(secret, request.form.get("code", "")):
        flash("That code didn't match. Check your phone's clock and try the current code.", "error")
        return redirect(url_for("auth.security") + "#mfa")
    execute("UPDATE users SET mfa_secret_enc = ?, mfa_enabled = 1 WHERE id = ?", (encrypt(secret), user["id"]))
    session.pop("mfa_setup", None)
    audit("account.mfa_enabled")
    transactional.send_security_alert(user, "Two-step verification with an authenticator app was turned on.")
    flash("Two-step verification is on.", "success")
    return redirect(url_for("auth.security"))


@bp.route("/account/mfa/disable", methods=["POST"])
@login_required
def mfa_disable():
    user = current_user()
    if not check_password_hash(user["password_hash"], request.form.get("password", "")):
        flash("Your password is not correct.", "error")
        return redirect(url_for("auth.security"))
    execute("UPDATE users SET mfa_secret_enc = '', mfa_enabled = 0 WHERE id = ?", (user["id"],))
    audit("account.mfa_disabled")
    transactional.send_security_alert(user, "Two-step verification with an authenticator app was turned off.")
    flash("Authenticator two-step verification is off.", "success")
    return redirect(url_for("auth.security"))


@bp.route("/account/email-codes", methods=["POST"])
@login_required
def email_codes():
    user = current_user()
    on = request.form.get("enabled") == "1"
    execute("UPDATE users SET email_codes = ? WHERE id = ?", (int(on), user["id"]))
    audit("account.email_codes", detail="on" if on else "off")
    flash("Sign-in codes by email are " + ("on." if on else "off."), "success")
    return redirect(url_for("auth.security"))


@bp.route("/account/sessions/revoke-others", methods=["POST"])
@login_required
def revoke_others():
    revoke_all_sessions(current_user()["id"], except_current=True)
    audit("account.sessions_revoked")
    flash("All other devices have been signed out.", "success")
    return redirect(url_for("auth.security"))


@bp.route("/account/export")
@login_required
def export_account():
    """Your personal data as JSON: profile, sessions and your own activity log."""
    user = current_user()
    data = {
        "exported_at": now() + " UTC",
        "profile": {k: user[k] for k in ("email", "full_name", "role", "created_at", "last_login_at", "email_verified_at",
                                         "mfa_enabled", "email_codes")},
        "sessions": [dict(created_at=s["created_at"], last_seen_at=s["last_seen_at"], ip=s["ip"], user_agent=s["user_agent"],
                          revoked_at=s["revoked_at"])
                     for s in query("SELECT * FROM user_sessions WHERE user_id = ? ORDER BY id", (user["id"],))],
        "activity": [dict(r) for r in query("SELECT action, target, detail, ip, created_at FROM audit_log WHERE user_id = ? "
                                            "ORDER BY id", (user["id"],))],
    }
    audit("account.exported")
    return send_file(io.BytesIO(json.dumps(data, indent=2).encode()), mimetype="application/json", as_attachment=True,
                     download_name="global-marketing-account.json")


@bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    user = current_user()
    if not check_password_hash(user["password_hash"], request.form.get("password", "")):
        flash("Your password is not correct.", "error")
        return redirect(url_for("auth.security"))
    others = query("SELECT COUNT(*) AS n FROM users WHERE workspace_id = ? AND id != ? AND is_active = 1",
                   (user["workspace_id"], user["id"]), one=True)["n"] if user["workspace_id"] else 0
    if user["role"] == "owner" and others:
        flash("Make another member the owner (Administration → Team) before deleting your account.", "error")
        return redirect(url_for("auth.security"))
    ws = user["workspace_id"]
    audit("account.deleted", target=user["email"])
    end_session()
    with transaction():
        if user["role"] == "owner" and ws:
            # Sole member: the workspace and everything in it go too (contacts, campaigns, logs).
            execute("DELETE FROM workspaces WHERE id = ?", (ws,))
            execute("DELETE FROM secrets WHERE workspace_id = ?", (ws,))
            execute("DELETE FROM messages WHERE workspace_id = ?", (ws,))
        execute("DELETE FROM users WHERE id = ?", (user["id"],))
    flash("Your account has been deleted.", "success")
    return redirect(url_for("auth.login"))


@bp.app_errorhandler(429)
def too_many(_e):
    return render_template("error.html", code=429, message="Too many requests. Please wait a moment and try again."), 429
