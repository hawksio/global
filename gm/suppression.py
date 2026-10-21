"""Suppression list: addresses that must never receive marketing email from this workspace.

Entries come from unsubscribes, hard bounces, spam complaints, provider suppressions and manual additions.
Adding an entry updates the matching contact and stops every active campaign or follow-up sequence for it
immediately. Suppressions survive contact deletion so a deleted-then-reimported address stays protected.
"""
import csv
import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from . import validation
from .db import execute, now, query, scalar
from .security import audit, current_user, require, ws_id

bp = Blueprint("suppression", __name__, url_prefix="/suppressions")

REASONS = {"unsubscribe": "Unsubscribed", "hard_bounce": "Hard bounce", "complaint": "Spam complaint",
           "manual": "Added manually", "provider": "Suppressed by the email provider"}
CONTACT_STATUS = {"unsubscribe": "unsubscribed", "hard_bounce": "bounced", "complaint": "complained",
                  "manual": "suppressed", "provider": "suppressed"}


def is_suppressed(workspace_id, email_normalized):
    return bool(query("SELECT 1 FROM suppressions WHERE workspace_id = ? AND email_normalized = ?",
                      (workspace_id, email_normalized), one=True))


def add_suppression(workspace_id, email_normalized, reason, source="", simulated=False):
    """Idempotent. A stronger reason (complaint, bounce) replaces a weaker one (manual)."""
    email_normalized = email_normalized.strip().lower()
    existing = query("SELECT reason FROM suppressions WHERE workspace_id = ? AND email_normalized = ?",
                     (workspace_id, email_normalized), one=True)
    if existing is None:
        execute("INSERT INTO suppressions (workspace_id, email_normalized, reason, source, simulated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (workspace_id, email_normalized, reason, source[:200], int(simulated), now()))
    elif existing["reason"] == "manual" and reason != "manual":
        execute("UPDATE suppressions SET reason = ?, source = ? WHERE workspace_id = ? AND email_normalized = ?",
                (reason, source[:200], workspace_id, email_normalized))
    contact = query("SELECT id, status FROM contacts WHERE workspace_id = ? AND email_normalized = ?",
                    (workspace_id, email_normalized), one=True)
    if contact:
        execute("UPDATE contacts SET status = ?, verification_status = 'suppressed', marketing_opt_out = "
                "CASE WHEN ? = 'unsubscribe' THEN 1 ELSE marketing_opt_out END, updated_at = ? WHERE id = ?",
                (CONTACT_STATUS[reason], reason, now(), contact["id"]))
        from .contacts import add_event
        add_event(workspace_id, contact["id"], "suppressed", f"{REASONS[reason]}" + (f" ({source})" if source else ""),
                  simulated=simulated)
        from .campaigns import stop_for_contact
        stop_for_contact(contact["id"], REASONS[reason])


def remove_suppression(workspace_id, email_normalized):
    execute("DELETE FROM suppressions WHERE workspace_id = ? AND email_normalized = ?", (workspace_id, email_normalized))
    execute("UPDATE contacts SET status = 'active', verification_status = 'unverified', updated_at = ? "
            "WHERE workspace_id = ? AND email_normalized = ?", (now(), workspace_id, email_normalized))
    audit("suppression.removed", target=email_normalized)


@bp.route("/")
@require("view_contacts")
def index():
    wid = ws_id()
    reason = request.args.get("reason", "")
    q = request.args.get("q", "").strip().lower()
    where, args = ["workspace_id = ?"], [wid]
    if reason in REASONS:
        where.append("reason = ?")
        args.append(reason)
    if q:
        where.append("email_normalized LIKE ?")
        args.append(f"%{q}%")
    page = max(1, request.args.get("page", 1, type=int))
    total = scalar(f"SELECT COUNT(*) FROM suppressions WHERE {' AND '.join(where)}", args)
    rows = query(f"SELECT * FROM suppressions WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 100 OFFSET ?",
                 (*args, (page - 1) * 100))
    counts = {r["reason"]: r["n"] for r in query("SELECT reason, COUNT(*) AS n FROM suppressions WHERE workspace_id = ? "
                                                 "GROUP BY reason", (wid,))}
    return render_template("suppressions.html", rows=rows, total=total, page=page, pages=max(1, -(-total // 100)),
                           reason=reason, q=q, counts=counts, REASONS=REASONS)


@bp.route("/add", methods=["POST"])
@require("edit_contacts")
def add():
    wid = ws_id()
    added, bad = 0, 0
    raw = request.form.get("emails", "")
    file = request.files.get("file")
    if file and file.filename:
        raw += "\n" + file.read(5 * 1024 * 1024).decode("utf-8-sig", errors="replace")
    for token in raw.replace(";", "\n").replace(",", "\n").split():
        normalized = validation.normalize(token)
        if normalized:
            add_suppression(wid, normalized, "manual", f"Added by {current_user()['full_name']}")
            added += 1
        elif "@" in token:
            bad += 1
    audit("suppression.added", detail=f"{added} addresses")
    flash(f"Suppressed {added} addresses." + (f" {bad} entries were not valid addresses." if bad else ""), "success")
    return redirect(url_for("suppression.index"))


@bp.route("/remove", methods=["POST"])
@require("manage_settings")
def remove():
    email = request.form.get("email", "").strip().lower()
    row = query("SELECT reason FROM suppressions WHERE workspace_id = ? AND email_normalized = ?", (ws_id(), email), one=True)
    if not row:
        abort(404)
    if row["reason"] == "complaint" and request.form.get("confirm") != "yes":
        flash("This address reported your email as spam. Only remove it if the person has explicitly asked to hear from you.",
              "error")
        return redirect(url_for("suppression.index", q=email))
    remove_suppression(ws_id(), email)
    flash(f"{email} removed from the suppression list.", "success")
    return redirect(url_for("suppression.index"))


@bp.route("/export.csv")
@require("export_contacts")
def export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "reason", "source", "created_at", "simulated"])
    for r in query("SELECT * FROM suppressions WHERE workspace_id = ? ORDER BY id", (ws_id(),)):
        from .contacts import csv_safe
        w.writerow([csv_safe(r["email_normalized"]), r["reason"], csv_safe(r["source"]), r["created_at"], r["simulated"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=suppressions.csv"})
