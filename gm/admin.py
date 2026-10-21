"""Administration.

Workspace administration (owners and admins): team members, roles, invitations, ownership transfer, audit log.
System administration (superadmins only): every workspace and its quotas, all users, background jobs,
operational health and system-wide credentials.
"""
import os
import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from . import jobs, transactional
from .db import execute, now, query, scalar, transaction
from .security import (ROLES, audit, current_user, get_secret, require, revoke_all_sessions, secret_hint, set_secret,
                       superadmin_required, ws_id)

bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------- team
@bp.route("/team", methods=["GET", "POST"])
@require("manage_users")
def team():
    wid = ws_id()
    me = current_user()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "invite":
            from .validation import syntax_check
            email = f.get("email", "").strip().lower()
            role = f.get("role")
            if not syntax_check(email)[0] or role not in ROLES or role == "owner":
                flash("Enter a valid email address and role.", "error")
                return redirect(url_for("admin.team"))
            if role == "admin" and me["role"] != "owner":
                flash("Only the owner can invite administrators.", "error")
                return redirect(url_for("admin.team"))
            if query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
                flash("That address already has an account (people can belong to one workspace).", "error")
                return redirect(url_for("admin.team"))
            uid = execute("INSERT INTO users (workspace_id, email, password_hash, full_name, role, created_at) VALUES (?,?,?,?,?,?)",
                          (wid, email, generate_password_hash(secrets.token_urlsafe(32)), f.get("full_name", "").strip()[:120]
                           or email.split("@")[0], role, now()))
            user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True)
            token = transactional.new_reset_token(uid, minutes=72 * 60)
            link = current_app.config["BASE_URL"] + url_for("auth.reset", uid=uid, token=token, invite=1)
            transactional.send_invite(user, me, link)
            audit("team.invited", target=email, detail=role)
            flash(f"Invitation sent to {email}. The link works once and expires in 72 hours.", "success")
            return redirect(url_for("admin.team"))
        target = query("SELECT * FROM users WHERE id = ? AND workspace_id = ?", (f.get("user_id", type=int), wid), one=True)
        if not target:
            abort(404)
        if target["id"] == me["id"] and action != "transfer":
            flash("You can't change your own role or access here.", "error")
            return redirect(url_for("admin.team"))
        if target["role"] == "owner" or (target["role"] == "admin" and me["role"] != "owner"):
            abort(403)
        if action == "role":
            role = f.get("role")
            if role not in ROLES or role == "owner" or (role == "admin" and me["role"] != "owner"):
                abort(403)
            execute("UPDATE users SET role = ? WHERE id = ?", (role, target["id"]))
            audit("team.role_changed", target=target["email"], detail=role)
            flash(f"{target['full_name']} is now {ROLES[role]}.", "success")
        elif action == "toggle":
            execute("UPDATE users SET is_active = ? WHERE id = ?", (0 if target["is_active"] else 1, target["id"]))
            if target["is_active"]:
                revoke_all_sessions(target["id"])
            audit("team.access_changed", target=target["email"], detail="disabled" if target["is_active"] else "enabled")
            flash(f"{target['full_name']} has been {'deactivated' if target['is_active'] else 'reactivated'}.", "success")
        elif action == "transfer":
            if me["role"] != "owner":
                abort(403)
            with transaction():
                execute("UPDATE users SET role = 'owner' WHERE id = ?", (target["id"],))
                execute("UPDATE users SET role = 'admin' WHERE id = ?", (me["id"],))
            audit("team.ownership_transferred", target=target["email"])
            flash(f"{target['full_name']} is now the owner. You are an administrator.", "success")
        elif action == "remove":
            execute("DELETE FROM users WHERE id = ?", (target["id"],))
            audit("team.removed", target=target["email"])
            flash(f"{target['full_name']} was removed from the workspace.", "success")
        else:
            abort(400)
        return redirect(url_for("admin.team"))
    users = query("SELECT * FROM users WHERE workspace_id = ? ORDER BY role = 'owner' DESC, full_name", (wid,))
    return render_template("admin/team.html", users=users, ROLES=ROLES)


@bp.route("/audit")
@require("view_audit")
def audit_log():
    wid = ws_id()
    q = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    where, args = ["a.workspace_id = ?"], [wid]
    if q:
        where.append("(a.action LIKE ? OR a.target LIKE ? OR a.detail LIKE ?)")
        args += [f"%{q}%"] * 3
    rows = query(f"SELECT a.*, u.full_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id WHERE {' AND '.join(where)} "
                 f"ORDER BY a.id DESC LIMIT 100 OFFSET ?", (*args, (page - 1) * 100))
    return render_template("admin/audit.html", rows=rows, q=q, page=page)


# ---------------------------------------------------------------- system administration
@bp.route("/system")
@superadmin_required
def system():
    workspaces = query("SELECT w.*, (SELECT COUNT(*) FROM users u WHERE u.workspace_id = w.id) AS users, "
                       "(SELECT COUNT(*) FROM contacts c WHERE c.workspace_id = w.id) AS contacts, "
                       "(SELECT COUNT(*) FROM messages m WHERE m.workspace_id = w.id AND m.simulated = 0 AND m.category = "
                       "'marketing' AND m.created_at > datetime('now', '-30 days')) AS sent30 FROM workspaces w ORDER BY w.id")
    health = {
        "jobs_queued": scalar("SELECT COUNT(*) FROM jobs WHERE status = 'queued'"),
        "jobs_running": scalar("SELECT COUNT(*) FROM jobs WHERE status = 'running'"),
        "jobs_failed_24h": scalar("SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND finished_at > datetime('now', '-1 day')"),
        "worker_last": scalar("SELECT MAX(finished_at) FROM jobs"),
        "webhooks_24h": scalar("SELECT COUNT(*) FROM webhook_events WHERE received_at > datetime('now', '-1 day')"),
        "webhook_errors": scalar("SELECT COUNT(*) FROM webhook_events WHERE processed_at IS NULL"),
        "tx_failed_24h": scalar("SELECT COUNT(*) FROM messages WHERE category = 'transactional' AND status = 'failed' AND "
                                "created_at > datetime('now', '-1 day')"),
        "db_size_mb": round(os.path.getsize(current_app.config["DATABASE"]) / 1048576, 1),
        "backups": sorted(os.listdir(os.path.join(current_app.instance_path, "backups")))[-5:]
        if os.path.isdir(os.path.join(current_app.instance_path, "backups")) else [],
        "env": current_app.config["ENV_NAME"],
        "signup": current_app.config["ALLOW_SIGNUP"],
        "transactional": "Resend" if (current_app.config["RESEND_API_KEY"] or get_secret(0, "resend_api_key")) else
                         "local outbox only (nothing is sent)",
    }
    return render_template("admin/system.html", workspaces=workspaces, health=health,
                           resend_hint=secret_hint(0, "resend_api_key"), anthropic_hint=secret_hint(0, "anthropic_api_key"),
                           webhook_hint=secret_hint(0, "resend_webhook_secret"),
                           env_keys={"resend": bool(current_app.config["RESEND_API_KEY"]),
                                     "anthropic": bool(current_app.config["ANTHROPIC_API_KEY"]),
                                     "webhook": bool(current_app.config["RESEND_WEBHOOK_SECRET"])})


@bp.route("/system/workspaces/<int:workspace_id>", methods=["POST"])
@superadmin_required
def system_workspace(workspace_id):
    ws = query("SELECT * FROM workspaces WHERE id = ?", (workspace_id,), one=True)
    if not ws:
        abort(404)
    f = request.form
    if f.get("action") == "quotas":
        vals = [f.get(k, type=int) for k in ("daily_send_limit", "hourly_send_limit", "ai_monthly_limit", "discovery_daily_limit")]
        if any(v is None or v < 0 for v in vals):
            flash("Quotas must be zero or more.", "error")
        else:
            execute("UPDATE workspaces SET daily_send_limit = ?, hourly_send_limit = ?, ai_monthly_limit = ?, "
                    "discovery_daily_limit = ? WHERE id = ?", (*vals, workspace_id))
            audit("system.quotas", target=workspace_id, detail=str(vals), workspace_id=workspace_id)
            flash("Quotas updated.", "success")
    elif f.get("action") == "pause":
        from .campaigns import pause_workspace
        pause_workspace(ws, "Paused by a system administrator: " + (f.get("reason", "").strip()[:200] or "no reason given"))
        flash("All sending for that workspace is paused.", "success")
    else:
        abort(400)
    return redirect(url_for("admin.system"))


@bp.route("/system/users")
@superadmin_required
def system_users():
    q = request.args.get("q", "").strip().lower()
    rows = query("SELECT u.*, w.name AS workspace FROM users u LEFT JOIN workspaces w ON w.id = u.workspace_id "
                 "WHERE lower(u.email) LIKE ? OR lower(u.full_name) LIKE ? ORDER BY u.id DESC LIMIT 200",
                 (f"%{q}%", f"%{q}%"))
    return render_template("admin/system_users.html", rows=rows, q=q)


@bp.route("/system/users/<int:user_id>", methods=["POST"])
@superadmin_required
def system_user(user_id):
    target = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if not target or target["id"] == current_user()["id"]:
        abort(400)
    action = request.form.get("action")
    if action == "toggle":
        execute("UPDATE users SET is_active = ? WHERE id = ?", (0 if target["is_active"] else 1, user_id))
        if target["is_active"]:
            revoke_all_sessions(user_id)
    elif action == "unlock":
        execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
    elif action == "superadmin":
        execute("UPDATE users SET is_superadmin = ? WHERE id = ?", (0 if target["is_superadmin"] else 1, user_id))
    else:
        abort(400)
    audit(f"system.user_{action}", target=target["email"], workspace_id=target["workspace_id"])
    flash("User updated.", "success")
    return redirect(url_for("admin.system_users"))


@bp.route("/system/jobs")
@superadmin_required
def system_jobs():
    status = request.args.get("status", "")
    where, args = "", []
    if status in ("queued", "running", "completed", "failed", "cancelled"):
        where, args = "WHERE status = ?", [status]
    rows = query(f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT 200", args)
    return render_template("admin/system_jobs.html", rows=rows, status=status)


@bp.route("/system/jobs/<int:job_id>", methods=["POST"])
@superadmin_required
def system_job(job_id):
    job = query("SELECT * FROM jobs WHERE id = ?", (job_id,), one=True)
    if not job:
        abort(404)
    if request.form.get("action") == "retry" and job["status"] == "failed":
        execute("UPDATE jobs SET status = 'queued', attempts = 0, error = '', run_after = ?, finished_at = NULL WHERE id = ?",
                (now(), job_id))
        flash("Job queued again.", "success")
    elif request.form.get("action") == "cancel":
        jobs.request_cancel(job_id, job["workspace_id"]) if job["workspace_id"] else execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        flash("Cancellation requested.", "success")
    return redirect(url_for("admin.system_jobs"))


@bp.route("/system/keys", methods=["POST"])
@superadmin_required
def system_keys():
    f = request.form
    name = f.get("name")
    if name not in ("resend_api_key", "anthropic_api_key", "resend_webhook_secret"):
        abort(400)
    value = f.get("value", "").strip()
    set_secret(0, name, value, current_user()["id"])
    audit("system.key", target=name, detail="set" if value else "removed", workspace_id=0)
    flash("Saved (encrypted). Environment variables, when set, take precedence.", "success")
    return redirect(url_for("admin.system"))
