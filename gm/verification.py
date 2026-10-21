"""Email Verification and List Cleaning sections.

Verification: check one address with full evidence, validate a CSV file without importing it (downloadable report
and cleaned export), or re-check selected contacts. List cleaning: find and act on invalid, risky, disposable,
duplicate-looking and suppressed contacts.

Everything here is a local check (see validation.py). It is never presented as proof that a mailbox exists.
"""
import csv
import io
import json
import os

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import jobs, validation
from .contacts import VERIFICATION, add_event, csv_safe, read_csv, save_upload
from .db import execute, now, query, scalar
from .security import audit, current_user, require, ws_id

bp = Blueprint("verification", __name__)


@bp.route("/verification", methods=["GET", "POST"])
@require("verify")
def index():
    wid = ws_id()
    result = None
    if request.method == "POST":
        from .security import throttle
        if not throttle(f"verify-single:{wid}", 120, 3600):
            result = validation.check(request.form.get("email", ""), wid, dns=True)
    recent = query("SELECT * FROM jobs WHERE workspace_id = ? AND kind IN ('verify_csv', 'verify_contacts') "
                   "ORDER BY id DESC LIMIT 15", (wid,))
    stats = {r["verification_status"]: r["n"] for r in query(
        "SELECT verification_status, COUNT(*) AS n FROM contacts WHERE workspace_id = ? GROUP BY verification_status", (wid,))}
    return render_template("verification/index.html", result=result, recent=recent, stats=stats,
                           disposable=validation.disposable_meta())


@bp.route("/verification/csv", methods=["POST"])
@require("verify")
def upload_csv():
    wid = ws_id()
    file = request.files.get("file")
    if not file:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("verification.index"))
    try:
        stored, headers, count = save_upload(file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("verification.index"))
    column = next((i for i, h in enumerate(headers) if h.strip().lower() in ("email", "e-mail", "email address", "mail")), 0)
    job = jobs.enqueue("verify_csv", {"stored": stored, "column": column, "filename": file.filename[:120],
                                      "dns": request.form.get("dns", "on") == "on"},
                       workspace_id=wid, user_id=current_user()["id"], total=count)
    audit("verification.csv_started", target=file.filename, detail=f"{count} rows")
    return redirect(url_for("verification.job", job_id=job))


@bp.route("/verification/reverify", methods=["POST"])
@require("verify")
def reverify():
    """Re-check contacts in a status group (e.g. everything 'unknown' because DNS was down)."""
    wid = ws_id()
    status = request.form.get("status", "")
    if status not in VERIFICATION:
        abort(400)
    ids = [r["id"] for r in query("SELECT id FROM contacts WHERE workspace_id = ? AND verification_status = ? LIMIT 20000",
                                  (wid, status))]
    if not ids:
        flash("No contacts in that group.", "info")
        return redirect(url_for("verification.index"))
    job = jobs.enqueue("verify_contacts", {"ids": ids}, workspace_id=wid, user_id=current_user()["id"], total=len(ids),
                       dedupe_key=f"reverify:{wid}:{status}")
    return redirect(url_for("verification.job", job_id=job))


def _job(job_id):
    job = query("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, ws_id()), one=True)
    if not job:
        abort(404)
    return job


@bp.route("/jobs/<int:job_id>")
@require("view_contacts")
def job(job_id):
    j = _job(job_id)
    return render_template("verification/job.html", job=j, result=json.loads(j["result"] or "{}"),
                           params=json.loads(j["params"] or "{}"))


@bp.route("/jobs/<int:job_id>.json")
@require("view_contacts")
def job_json(job_id):
    return jsonify(jobs.job_dict(_job(job_id)))


@bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@require("view_contacts")
def job_cancel(job_id):
    j = _job(job_id)
    if j["user_id"] != current_user()["id"] and current_user()["role"] not in ("owner", "admin"):
        abort(403)
    jobs.request_cancel(job_id, j["workspace_id"])
    flash("Cancelling… work already finished is kept.", "info")
    return redirect(request.referrer or url_for("main.overview"))


def _report_path(job_id):
    return os.path.join(current_app.config["REPORT_DIR"], f"verify-{job_id}.csv")


@bp.route("/jobs/<int:job_id>/report.csv")
@require("verify")
def report(job_id):
    j = _job(job_id)
    path = _report_path(j["id"])
    if j["kind"] != "verify_csv" or not os.path.exists(path):
        abort(404)
    only = request.args.get("only")  # "clean" = validated rows only
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if only == "clean":
        idx = rows[0].index("gm_status")
        rows = [rows[0]] + [r for r in rows[1:] if r[idx] == "validated"]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    name = f"verification-{job_id}{'-cleaned' if only == 'clean' else ''}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@jobs.handler("verify_csv")
def _verify_csv(ctx):
    p = ctx.params
    src = os.path.join(current_app.config["IMPORT_DIR"], p["stored"])
    rows = read_csv(src)
    header, body = rows[0], rows[1:]
    counts = {k: 0 for k in ("validated", "invalid", "risky", "unknown", "suppressed", "duplicate", "empty")}
    seen = set()
    os.makedirs(current_app.config["REPORT_DIR"], exist_ok=True)
    with open(_report_path(ctx.id), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([csv_safe(h) for h in header] + ["gm_status", "gm_normalized", "gm_role", "gm_disposable", "gm_reason",
                                                    "gm_checked_at"])
        for n, row in enumerate(body):
            if n % 100 == 0:
                ctx.check_cancel()
                ctx.progress(n, len(body), f"Checked {n:,} of {len(body):,}")
            email = row[p["column"]].strip() if p["column"] < len(row) else ""
            if not email:
                counts["empty"] += 1
                w.writerow([csv_safe(c) for c in row] + ["invalid", "", "", "", "No email address", now()])
                continue
            res = validation.check(email, ctx.workspace_id, dns=p.get("dns", True))
            status = res["status"]
            if res["normalized"] and res["normalized"] in seen:
                status, res["summary"] = "duplicate", "Duplicate of an earlier row"
            if res["normalized"]:
                seen.add(res["normalized"])
            counts[status] += 1
            w.writerow([csv_safe(c) for c in row] + [status, res["normalized"] or "", int(res["is_role"]),
                                                     int(res["is_disposable"]), csv_safe(res["summary"]), now()])
    os.remove(src)  # the uploaded file is not kept; only the report is
    ctx.progress(len(body), len(body), "Finished")
    return counts


@jobs.handler("verify_contacts")
def _verify_contacts(ctx):
    ids = ctx.params["ids"]
    counts = {}
    for n, cid in enumerate(ids):
        if n % 50 == 0:
            ctx.check_cancel()
            ctx.progress(n, len(ids), f"Checked {n:,} of {len(ids):,}")
        c = query("SELECT * FROM contacts WHERE id = ? AND workspace_id = ?", (cid, ctx.workspace_id), one=True)
        if not c:
            continue
        res = validation.check(c["email"], ctx.workspace_id, dns=True)
        validation.store_on_contact(cid, res)
        if res["status"] != c["verification_status"]:
            add_event(ctx.workspace_id, cid, "verification", f"{VERIFICATION[res['status']]}: {res['summary']}")
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    ctx.progress(len(ids), len(ids), "Finished")
    return counts


# ---------------------------------------------------------------- list cleaning
CLEAN_GROUPS = {
    "invalid": ("Invalid addresses", "c.verification_status = 'invalid'"),
    "risky": ("Risky addresses (review)", "c.verification_status = 'risky'"),
    "unknown": ("Could not be checked", "c.verification_status = 'unknown'"),
    "unverified": ("Never checked", "c.verification_status = 'unverified'"),
    "disposable": ("Disposable domains", "c.is_disposable = 1"),
    "role": ("Role-based addresses", "c.is_role = 1"),
    "suppressed": ("Suppressed / unsubscribed / bounced", "c.status != 'active'"),
    "no_basis": ("No lawful basis recorded", "c.lawful_basis = 'unknown'"),
}


@bp.route("/cleaning")
@require("verify")
def cleaning():
    wid = ws_id()
    groups = {k: (label, scalar(f"SELECT COUNT(*) FROM contacts c WHERE c.workspace_id = ? AND {cond}", (wid,)))
              for k, (label, cond) in CLEAN_GROUPS.items()}
    # Addresses that differ only by dots or +tags: shown for review, never merged automatically.
    lookalikes = query(
        "SELECT lower(replace(substr(email_normalized, 1, instr(email_normalized, '@') - 1), '.', '')) AS base, domain, "
        "COUNT(*) AS n, group_concat(email_normalized, ', ') AS emails FROM contacts WHERE workspace_id = ? "
        "GROUP BY base, domain HAVING n > 1 LIMIT 50", (wid,))
    total = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ?", (wid,))
    return render_template("verification/cleaning.html", groups=groups, lookalikes=lookalikes, total=total)


@bp.route("/cleaning/apply", methods=["POST"])
@require("delete_contacts")
def cleaning_apply():
    wid = ws_id()
    group, action = request.form.get("group"), request.form.get("action")
    if group not in CLEAN_GROUPS or action not in ("delete", "suppress", "tag", "verify"):
        abort(400)
    cond = CLEAN_GROUPS[group][1]
    rows = query(f"SELECT c.id, c.email_normalized FROM contacts c WHERE c.workspace_id = ? AND {cond}", (wid,))
    if not rows:
        flash("Nothing to clean in that group.", "info")
        return redirect(url_for("verification.cleaning"))
    ids = [r["id"] for r in rows]
    if action == "delete":
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            execute(f"DELETE FROM contacts WHERE workspace_id = ? AND id IN ({','.join('?' * len(chunk))})", (wid, *chunk))
        flash(f"Deleted {len(ids)} contacts.", "success")
    elif action == "suppress":
        from .suppression import add_suppression
        for r in rows:
            add_suppression(wid, r["email_normalized"], "manual", "List cleaning")
        flash(f"Suppressed {len(ids)} addresses.", "success")
    elif action == "tag":
        from .contacts import tag_ids
        tid = tag_ids(wid, [f"review-{group}"])[0]
        for cid in ids:
            execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)", (cid, tid))
        flash(f"Tagged {len(ids)} contacts with review-{group}.", "success")
    else:
        job = jobs.enqueue("verify_contacts", {"ids": ids}, workspace_id=wid, user_id=current_user()["id"], total=len(ids))
        return redirect(url_for("verification.job", job_id=job))
    audit("cleaning.applied", target=group, detail=f"{action} {len(ids)}")
    return redirect(url_for("verification.cleaning"))


@bp.route("/cleaning/export.csv")
@require("export_contacts")
def cleaning_export():
    """Export only contacts that are validated, active, not disposable and have a lawful basis recorded."""
    from .contacts import export_rows
    rows = query("SELECT * FROM contacts WHERE workspace_id = ? AND verification_status = 'validated' AND status = 'active' "
                 "AND is_disposable = 0 AND lawful_basis != 'unknown' AND marketing_opt_out = 0 ORDER BY id", (ws_id(),))
    return export_rows(rows, "cleaned-contacts.csv")
