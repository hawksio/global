"""Public endpoints: unsubscribe (with RFC 8058 one-click) and the health check.

GET /u/<token> only shows a confirmation page, because mail scanners follow links; the unsubscribe happens on POST.
Mailbox providers' one-click POST (body "List-Unsubscribe=One-Click") is processed immediately without a form.
"""
import os

from flask import Blueprint, current_app, jsonify, render_template, request

from .db import get_db, now, query
from .render import read_unsubscribe_token

bp = Blueprint("public", __name__)


def _process(data, source):
    from .suppression import add_suppression
    ws = query("SELECT id FROM workspaces WHERE id = ?", (data["w"],), one=True)
    if not ws:
        return False
    simulated = False
    if data.get("m"):
        m = query("SELECT simulated FROM messages WHERE id = ? AND workspace_id = ?", (data["m"], ws["id"]), one=True)
        simulated = bool(m and m["simulated"])
    add_suppression(ws["id"], data["e"], "unsubscribe", source, simulated=simulated)
    return True


@bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    data = read_unsubscribe_token(token)
    if data is None:
        return render_template("public/unsubscribe.html", state="invalid"), 400
    ws = query("SELECT company_name, name FROM workspaces WHERE id = ?", (data["w"],), one=True)
    if request.method == "POST":
        one_click = request.form.get("List-Unsubscribe") == "One-Click"
        _process(data, "One-click unsubscribe" if one_click else "Unsubscribe page")
        if one_click:
            return "", 200
        return render_template("public/unsubscribe.html", state="done", ws=ws, email=data["e"])
    return render_template("public/unsubscribe.html", state="confirm", ws=ws, email=data["e"], token=token)


@bp.route("/healthz")
def healthz():
    """Liveness and basic readiness for monitoring. Reveals no configuration values."""
    checks = {}
    try:
        get_db().execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    checks["storage_writable"] = "ok" if os.access(current_app.instance_path, os.W_OK) else "error"
    last = query("SELECT MAX(finished_at) AS t FROM jobs", one=True)
    checks["last_job_finished_at"] = last["t"] if last else None
    stuck = query("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND run_after < datetime('now', '-15 minutes')", one=True)
    checks["worker"] = "ok" if not stuck["n"] else "behind (is the cron job running?)"
    ok = checks["database"] == "ok" and checks["storage_writable"] == "ok"
    return jsonify({"status": "ok" if ok else "error", "time": now(), "checks": checks}), 200 if ok else 503
