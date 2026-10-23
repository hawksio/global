"""Campaigns and follow-up sequences.

Workflow: draft → (review checklist, dry-run report, test email) → pending approval → approved → scheduled/sending
→ completed, with pause, resume and cancel along the way. Editing content after approval sends it back to draft.

Sending (run by the background worker, never in a web request):
  * Recipients are snapshotted at launch from the segment, keeping only eligible contacts (see eligibility()).
  * Immediately before every send the contact is re-checked: suppression list, status, opt-out, verification.
  * Each email has an idempotency key "camp-<id>-rcp-<id>-step-<n>" that is unique in the message log and passed
    to Resend, so a retry or a crashed worker can never send the same step twice.
  * Limits: the campaign's per-minute rate, the workspace's hourly and daily caps, per-contact frequency
    (minimum hours between marketing emails), sending window and weekdays in the recipient's (or campaign's) time zone.
  * Transient failures are retried a bounded number of times; permanent failures are recorded, not retried.
  * Follow-ups stop automatically on reply, unsubscribe, bounce, complaint or suppression.
  * "Simulation" campaigns (dry_run = 1) go through the same engine but use the sandbox provider: nothing is sent and
    every message is stored as 'simulated'.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import jobs, mailer, render
from .ai import content_check
from .contacts import LAWFUL_BASES, RECIPIENT_TYPES, SOURCES, VERIFICATION, add_event, filter_sql
from .db import execute, fmt_ts, now, parse_ts, query, scalar, transaction
from .security import audit, can, current_user, require, workspace, ws_id

bp = Blueprint("campaigns", __name__, url_prefix="/campaigns")

STATUS_LABELS = {"draft": "Draft", "pending_approval": "Waiting for approval", "approved": "Approved", "scheduled": "Scheduled",
                 "sending": "Sending", "paused": "Paused", "completed": "Completed", "cancelled": "Cancelled"}
EDITABLE = ("draft", "pending_approval", "approved")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_READINESS_SCORE = 60


# ---------------------------------------------------------------- eligibility
def eligibility(contact, include_risky=False):
    """(eligible, reason). The rules that decide whether a contact may receive marketing email at all."""
    if contact["status"] != "active":
        return False, f"Status is {contact['status']}"
    if contact["marketing_opt_out"]:
        return False, "Opted out of marketing"
    if contact["verification_status"] in ("invalid", "suppressed"):
        return False, f"Address is {contact['verification_status']}"
    if contact["verification_status"] == "risky" and not include_risky:
        return False, "Address is risky (review it or include risky addresses deliberately)"
    if contact["lawful_basis"] == "unknown":
        return False, "No permission to email recorded"
    if contact["recipient_type"] == "consumer" and contact["lawful_basis"] != "consent":
        return False, "Consumer address without recorded consent"
    if contact["recipient_type"] == "unknown" and contact["lawful_basis"] == "legitimate_interest":
        return False, "Legitimate interest claimed but recipient type unknown"
    if contact["is_disposable"]:
        return False, "Disposable email domain"
    return True, ""


def segment_contacts(workspace_id, segment):
    where, args = filter_sql(workspace_id, segment)
    return query(f"SELECT c.* FROM contacts c WHERE {where} ORDER BY c.id", args)


def segment_report(workspace_id, segment):
    rows = segment_contacts(workspace_id, segment)
    suppressed = {r["email_normalized"] for r in query("SELECT email_normalized FROM suppressions WHERE workspace_id = ?",
                                                       (workspace_id,))}
    eligible, reasons, sources = [], {}, {}
    for c in rows:
        ok, why = eligibility(c, segment.get("include_risky"))
        if ok and c["email_normalized"] in suppressed:
            ok, why = False, "On the suppression list"
        if ok:
            eligible.append(c)
            sources[c["source"]] = sources.get(c["source"], 0) + 1
        else:
            reasons[why] = reasons.get(why, 0) + 1
    return {"matched": len(rows), "eligible": eligible, "excluded": reasons, "sources": sources,
            "unverified": sum(1 for c in eligible if c["verification_status"] in ("unverified", "unknown")),
            "role": sum(1 for c in eligible if c["is_role"])}


def stop_for_contact(contact_id, reason):
    """Stop every active campaign/sequence for a contact and cancel anything still queued for it."""
    execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = ?, next_send_at = NULL "
            "WHERE contact_id = ? AND status = 'active'", (reason[:200], contact_id))
    execute("UPDATE messages SET status = 'cancelled', last_error = ?, next_attempt_at = NULL "
            "WHERE contact_id = ? AND status = 'queued' AND category = 'marketing'", (f"Stopped: {reason}"[:300], contact_id))


# ---------------------------------------------------------------- data access
def get_campaign(campaign_id):
    c = query("SELECT * FROM campaigns WHERE id = ? AND workspace_id = ?", (campaign_id, ws_id()), one=True)
    if not c:
        abort(404)
    return c


def steps_of(campaign_id):
    return query("SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY step_no", (campaign_id,))


def _form_segment(f):
    seg = {k: f.get(f"seg_{k}", "").strip() for k in ("q", "company", "domain", "country", "industry", "tag", "source")}
    seg = {k: v for k, v in seg.items() if v}
    seg["verification_status"] = [v for v in f.getlist("seg_verification") if v in VERIFICATION] or ["validated"]
    seg["recipient_type"] = [v for v in f.getlist("seg_recipient_type") if v in RECIPIENT_TYPES]
    seg["lawful_basis"] = [v for v in f.getlist("seg_lawful_basis") if v in LAWFUL_BASES]
    seg["status"] = ["active"]
    if f.get("seg_exclude_role") == "on":
        seg["exclude_role"] = True
    if f.get("seg_include_risky") == "on":
        seg["include_risky"] = True
    if f.get("seg_exclude_sandbox", "on") == "on":
        seg["exclude_sandbox"] = True
    return seg


def _valid_tz(name):
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def back_to_draft(c, why):
    if c["status"] in ("pending_approval", "approved"):
        execute("UPDATE campaigns SET status = 'draft', approved_by = NULL, approved_at = NULL, test_sent_at = NULL, "
                "content_reviewed = 0 WHERE id = ?", (c["id"],))
        flash(f"{why} The campaign went back to draft and needs approval again.", "info")


# ---------------------------------------------------------------- views
@bp.route("/")
@require("view_campaigns")
def index():
    wid = ws_id()
    status = request.args.get("status", "")
    where, args = ["c.workspace_id = ?"], [wid]
    if status in STATUS_LABELS:
        where.append("c.status = ?")
        args.append(status)
    if request.args.get("sequences") == "1":
        where.append("(SELECT COUNT(*) FROM campaign_steps s WHERE s.campaign_id = c.id) > 1")
    rows = query(f"SELECT c.*, (SELECT COUNT(*) FROM campaign_steps s WHERE s.campaign_id = c.id) AS steps, "
                 f"(SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id = c.id) AS recipients, "
                 f"(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND m.status NOT IN ('queued','cancelled')) AS sent "
                 f"FROM campaigns c WHERE {' AND '.join(where)} ORDER BY c.id DESC", args)
    return render_template("campaigns/list.html", campaigns=rows, status=status, STATUS_LABELS=STATUS_LABELS,
                           sequences=request.args.get("sequences") == "1")


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:campaign_id>/edit", methods=["GET", "POST"])
@require("edit_campaigns")
def edit(campaign_id=None):
    wid = ws_id()
    ws = workspace()
    c = get_campaign(campaign_id) if campaign_id else None
    if c and c["status"] not in EDITABLE:
        flash("This campaign has started. Pause it to change limits, or cancel it.", "info")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    templates = query("SELECT * FROM templates WHERE workspace_id = ? AND archived = 0 ORDER BY name", (wid,))
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()[:120]
        tz = f.get("timezone", ws["timezone"])
        errors = []
        if not name:
            errors.append("Give the campaign a name.")
        if not _valid_tz(tz):
            errors.append("Choose a valid time zone.")
        start, end = f.get("send_window_start", type=int), f.get("send_window_end", type=int)
        if start is None or end is None or not (0 <= start < end <= 24):
            errors.append("The sending window must start before it ends (hours 0–24).")
        days = ",".join(d for d in f.getlist("send_days") if d in "0123456") or "0,1,2,3,4"
        rate = max(1, min(f.get("rate_per_minute", 10, type=int), 60))
        from_email = f.get("from_email", "").strip().lower() or ws["sender_email"]
        from .validation import syntax_check
        if from_email and not syntax_check(from_email)[0]:
            errors.append("The sender email address is not valid.")
        template = None
        if f.get("template_id", "").isdigit():
            template = query("SELECT * FROM templates WHERE id = ? AND workspace_id = ?", (int(f["template_id"]), wid), one=True)
        if not c and not template:
            errors.append("Choose a template for the first email (you can edit the campaign's copy afterwards).")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("campaigns/edit.html", c=c, form=f, templates=templates, segment=_form_segment(f),
                                   DAYS=DAYS, **_choices()), 400
        segment = _form_segment(f)
        values = (name, json.dumps(segment), f.get("from_name", "").strip()[:100] or ws["sender_name"], from_email,
                  f.get("reply_to", "").strip()[:200] or ws["reply_to"], tz, start, end, days, rate,
                  int(f.get("dry_run") == "on"), now())
        with transaction():
            if c:
                content_changed = json.dumps(segment) != c["segment"] or int(f.get("dry_run") == "on") != c["dry_run"]
                execute("UPDATE campaigns SET name = ?, segment = ?, from_name = ?, from_email = ?, reply_to = ?, timezone = ?, "
                        "send_window_start = ?, send_window_end = ?, send_days = ?, rate_per_minute = ?, dry_run = ?, "
                        "updated_at = ? WHERE id = ?", (*values, c["id"]))
                cid = c["id"]
                if content_changed:
                    back_to_draft(c, "The audience or practice-run setting changed.")
            else:
                cid = execute("INSERT INTO campaigns (workspace_id, name, segment, from_name, from_email, reply_to, timezone, "
                              "send_window_start, send_window_end, send_days, rate_per_minute, dry_run, updated_at, "
                              "template_id, ai_generated, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (wid, *values, template["id"], template["ai_generated"], current_user()["id"], now()))
                execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, preheader, html_body, text_body) "
                        "VALUES (?, 0, 0, ?, ?, ?, ?)", (cid, template["subject"], template["preheader"], template["html_body"],
                                                         template["text_body"]))
        audit("campaign.saved", target=cid)
        flash("Campaign saved.", "success")
        return redirect(url_for("campaigns.detail", campaign_id=cid))
    segment = json.loads(c["segment"]) if c else {"verification_status": ["validated"], "exclude_sandbox": True}
    return render_template("campaigns/edit.html", c=c, form=c or {"dry_run": 1, "send_window_start": 8, "send_window_end": 18,
                                                                 "send_days": "0,1,2,3,4", "rate_per_minute": 10,
                                                                 "timezone": ws["timezone"]},
                           templates=templates, segment=segment, DAYS=DAYS, **_choices())


def _choices():
    return {"VERIFICATION": VERIFICATION, "RECIPIENT_TYPES": RECIPIENT_TYPES, "LAWFUL_BASES": LAWFUL_BASES, "SOURCES": SOURCES,
            "tags": query("SELECT name FROM tags WHERE workspace_id = ? ORDER BY name", (ws_id(),))}


@bp.route("/<int:campaign_id>/steps", methods=["GET", "POST"])
@require("view_campaigns")
def steps(campaign_id):
    c = get_campaign(campaign_id)
    if request.method == "POST":
        if not can("edit_campaigns"):
            abort(403)
        if c["status"] not in EDITABLE:
            flash("Content can't change after a campaign has started.", "error")
            return redirect(url_for("campaigns.steps", campaign_id=c["id"]))
        f = request.form
        action = f.get("action")
        if action == "add":
            n = (scalar("SELECT MAX(step_no) FROM campaign_steps WHERE campaign_id = ?", (c["id"],)) or 0) + 1
            if n > 5:
                flash("A sequence can have at most 5 follow-ups.", "error")
            else:
                first = steps_of(c["id"])[0]
                execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) "
                        "VALUES (?, ?, 3, ?, ?, '')", (c["id"], n, "Re-checking in: " + first["subject"][:200],
                                                       "<p>Hi {{first_name|there}},</p><p>I'm following up on my last email in case it got buried. Is this worth a quick "
                                                       "conversation?</p>"
                                                       "<p>Best regards,<br>{{sender_name}}</p>"))
                back_to_draft(c, "A follow-up was added.")
        elif action == "delete":
            n = f.get("step_no", type=int)
            if n and n > 0:
                execute("DELETE FROM campaign_steps WHERE campaign_id = ? AND step_no = ?", (c["id"], n))
                for i, s in enumerate(steps_of(c["id"])):
                    execute("UPDATE campaign_steps SET step_no = ? WHERE id = ?", (i, s["id"]))
                back_to_draft(c, "A follow-up was removed.")
        elif action == "save":
            n = f.get("step_no", type=int)
            step = query("SELECT * FROM campaign_steps WHERE campaign_id = ? AND step_no = ?", (c["id"], n), one=True)
            if not step:
                abort(404)
            subject = f.get("subject", "").strip()[:300]
            html_body = render.strip_scripts(f.get("html_body", ""))[:200000]
            text_body = f.get("text_body", "")[:100000]
            delay = 0 if n == 0 else max(1, min(f.get("delay_days", 3, type=int), 60))
            bad = render.unknown_fields(subject, html_body, text_body)
            if bad or not subject or not html_body.strip():
                flash("Each email needs a subject and body" + (f"; unknown fields: {', '.join(bad)}" if bad else "") + ".", "error")
            else:
                execute("UPDATE campaign_steps SET subject = ?, html_body = ?, text_body = ?, delay_days = ?, preheader = ? "
                        "WHERE id = ?", (subject, html_body, text_body, delay, f.get("preheader", "")[:200], step["id"]))
                if (subject, html_body, text_body, delay) != (step["subject"], step["html_body"], step["text_body"],
                                                              step["delay_days"]):
                    back_to_draft(c, "The email content changed.")
                flash(f"{'First email' if n == 0 else f'Follow-up {n}'} saved.", "success")
        return redirect(url_for("campaigns.steps", campaign_id=c["id"]))
    return render_template("campaigns/steps.html", c=c, steps=steps_of(c["id"]), FIELDS=render.FIELDS,
                           editable=c["status"] in EDITABLE and can("edit_campaigns"), STATUS_LABELS=STATUS_LABELS)


def readiness(ws, c):
    """The pre-launch checklist. Returns a list of {label, ok, detail, blocking}."""
    from .deliverability import latest_check
    seg = json.loads(c["segment"])
    rep = segment_report(ws["id"], seg)
    steps = steps_of(c["id"])
    items = []

    def add(label, ok, detail, blocking=True):
        items.append({"label": label, "ok": bool(ok), "detail": detail, "blocking": blocking and not ok})

    add("1. People who will receive it", rep["eligible"],
        f"{len(rep['eligible'])} of {rep['matched']} matching contacts are eligible." if rep["matched"] else
        "No contacts match these choices.")
    srcs = ", ".join(f"{SOURCES.get(k, k)}: {v}" for k, v in rep["sources"].items()) or "none"
    add("2. Where these contacts came from", True,
        f"Sources: {srcs}. Everyone included has a recorded permission to email; private people must have agreed.", blocking=False)
    add("3. Addresses checked", rep["unverified"] == 0,
        f"{rep['unverified']} eligible addresses were never fully checked. Use Contacts → Check emails first." if rep["unverified"]
        else "All eligible addresses have been checked.", blocking=False)
    add("4. Do-not-email list applied", True,
        f"{rep['excluded'].get('On the suppression list', 0)} suppressed addresses excluded; re-checked before every send.",
        blocking=False)
    errors = []
    for s in steps:
        errors += [f"{'Email 1' if s['step_no'] == 0 else 'Follow-up ' + str(s['step_no'])}: {f['title']}"
                   for f in content_check(s["subject"], s["html_body"], s["text_body"]) if f["level"] == "error"]
    add("5. Email passes the automatic checks", not errors, "; ".join(errors) or "No blocking problems found.")
    add("6. AI-generated content reviewed by a person", not c["ai_generated"] or c["content_reviewed"],
        "Tick the review box on this page after reading every email." if c["ai_generated"] and not c["content_reviewed"]
        else "Reviewed." if c["ai_generated"] else "Not AI-generated.")
    add("7. Your sender email and postal address are set", c["from_email"] and ws["company_address"],
        "Set a sender email and your company's postal address in Settings." if not (c["from_email"] and ws["company_address"])
        else f"From {c['from_name']} <{c['from_email']}>.")
    real = not c["dry_run"] and not ws["sandbox_mode"]
    plain = c["style"] == "plain"  # Compose: a test is recommended, not required (like Gmail)
    add("8. Test email sent", c["test_sent_at"] or not real or plain,
        f"Test sent {c['test_sent_at']} UTC." if c["test_sent_at"] else "Send yourself a test email first." if real and not plain
        else "Optional." if plain else "Optional for a practice run.", blocking=real and not plain)
    if real:
        domain = c["from_email"].rsplit("@", 1)[-1] if "@" in c["from_email"] else ""
        check = latest_check(ws["id"], domain)
        ok = check and check["score"] >= MIN_READINESS_SCORE and check["results"].get("dkim", {}).get("status") == "pass"
        add("9. Sending domain ready", ok,
            f"Readiness score {check['score']}/100 (checked {check['checked_at']} UTC)." if check else
            f"Run the Deliverability Centre checks for {domain or 'your domain'} first.")
    else:
        add("9. Sending domain ready", True, "Not needed for a practice run.", blocking=False)
    add("10. Sending isn't paused", not ws["sending_paused_reason"],
        ws["sending_paused_reason"] or f"Limits: {ws['hourly_send_limit']}/hour, {ws['daily_send_limit']}/day, "
                                       f"{c['rate_per_minute']}/minute.")
    return items, rep


@bp.route("/<int:campaign_id>")
@require("view_campaigns")
def detail(campaign_id):
    c = get_campaign(campaign_id)
    ws = workspace()
    items, rep = readiness(ws, c) if c["status"] in EDITABLE else (None, None)
    from .analytics import campaign_stats
    recipients = query("SELECT r.*, ct.first_name, ct.last_name, ct.company FROM campaign_recipients r "
                       "JOIN contacts ct ON ct.id = r.contact_id WHERE r.campaign_id = ? ORDER BY r.id DESC LIMIT 100",
                       (c["id"],))
    recent = query("SELECT * FROM messages WHERE campaign_id = ? ORDER BY id DESC LIMIT 50", (c["id"],))
    return render_template("campaigns/detail.html", c=c, items=items, report=rep, steps=steps_of(c["id"]),
                           stats=campaign_stats(c["id"], ws["id"]), recipients=recipients, recent=recent,
                           STATUS_LABELS=STATUS_LABELS, DAYS=DAYS, segment=json.loads(c["segment"]),
                           team=[u["email"] for u in query("SELECT email FROM users WHERE workspace_id = ? AND is_active = 1",
                                                           (ws["id"],))],
                           creator=query("SELECT full_name FROM users WHERE id = ?", (c["created_by"],), one=True),
                           approver=query("SELECT full_name FROM users WHERE id = ?", (c["approved_by"],), one=True))


@bp.route("/<int:campaign_id>/dry-run")
@require("view_campaigns")
def dry_run(campaign_id):
    """Side-effect-free rehearsal: who would get what, who is excluded and why, and how long sending would take."""
    c = get_campaign(campaign_id)
    ws = workspace()
    rep = segment_report(ws["id"], json.loads(c["segment"]))
    step0 = steps_of(c["id"])[0]
    samples = []
    for contact in rep["eligible"][:3]:
        subj, html_out, text_out, _ = render.build_marketing(ws, contact, step0["subject"], step0["html_body"],
                                                             step0["text_body"], step0["preheader"])
        samples.append({"to": contact["email"], "subject": subj, "html": html_out})
    n = len(rep["eligible"])
    hours_per_day = c["send_window_end"] - c["send_window_start"]
    per_hour = min(ws["hourly_send_limit"], c["rate_per_minute"] * 60)
    per_day = min(ws["daily_send_limit"], per_hour * hours_per_day)
    days = (n / per_day) if per_day else 0
    return render_template("campaigns/dry_run.html", c=c, report=rep, samples=samples, per_hour=per_hour, per_day=per_day,
                           days=days, steps=steps_of(c["id"]))


@bp.route("/<int:campaign_id>/review", methods=["POST"])
@require("edit_campaigns")
def mark_reviewed(campaign_id):
    c = get_campaign(campaign_id)
    execute("UPDATE campaigns SET content_reviewed = ? WHERE id = ?", (int(request.form.get("reviewed") == "on"), c["id"]))
    audit("campaign.content_reviewed", target=c["id"])
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/test", methods=["POST"])
@require("edit_campaigns")
def test(campaign_id):
    c = get_campaign(campaign_id)
    ws = workspace()
    to = request.form.get("to", "").strip().lower()
    from .templates_lib import send_test, team_addresses
    if to not in team_addresses(ws["id"]):
        flash("Test emails can only go to members of your team.", "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    if not c["from_email"]:
        flash("Set a sender email address first.", "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    wsx = dict(ws, sender_name=c["from_name"], sender_email=c["from_email"], reply_to=c["reply_to"])
    sample = rep_contact = query("SELECT * FROM contacts WHERE workspace_id = ? AND sandbox = 0 ORDER BY id LIMIT 1",
                                 (ws["id"],), one=True)
    results = []
    for s in steps_of(c["id"]):
        status, error = send_test(wsx, to, s["subject"], s["html_body"], s["text_body"], s["preheader"],
                                  contact=dict(sample, email_normalized=to) if rep_contact else None, kind=f"campaign_{c['id']}")
        results.append((s["step_no"], status, error))
    failed = [r for r in results if r[1] == "failed"]
    if failed:
        flash(f"Test failed: {failed[0][2]}", "error")
    else:
        execute("UPDATE campaigns SET test_sent_at = ? WHERE id = ?", (now(), c["id"]))
        simulated = any(r[1] == "simulated" for r in results)
        flash(f"{len(results)} test email(s) " + ("written to the sandbox outbox (not sent)." if simulated else
                                                  f"accepted by the sending service for {to}."),
              "info" if simulated else "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/submit", methods=["POST"])
@require("edit_campaigns")
def submit(campaign_id):
    c = get_campaign(campaign_id)
    if c["status"] != "draft":
        abort(409)
    items, _ = readiness(workspace(), c)
    blocking = [i for i in items if i["blocking"]]
    if blocking:
        flash("Not ready yet: " + "; ".join(i["label"] for i in blocking), "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    execute("UPDATE campaigns SET status = 'pending_approval', updated_at = ? WHERE id = ?", (now(), c["id"]))
    audit("campaign.submitted", target=c["id"])
    flash("Submitted for approval.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/approve", methods=["POST"])
@require("approve_campaigns")
def approve(campaign_id):
    c = get_campaign(campaign_id)
    ws = workspace()
    if c["status"] != "pending_approval":
        abort(409)
    if ws["four_eyes_approval"] and c["created_by"] == current_user()["id"]:
        flash("Your workspace requires a different person to approve campaigns they did not create.", "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    items, _ = readiness(ws, c)
    if any(i["blocking"] for i in items):
        flash("The checklist has blocking problems; it can't be approved yet.", "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    execute("UPDATE campaigns SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?",
            (current_user()["id"], now(), now(), c["id"]))
    audit("campaign.approved", target=c["id"], detail="simulation" if c["dry_run"] else "real sending")
    flash("Campaign approved. You can now schedule or start it.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/reject", methods=["POST"])
@require("approve_campaigns")
def reject(campaign_id):
    c = get_campaign(campaign_id)
    if c["status"] not in ("pending_approval", "approved"):
        abort(409)
    execute("UPDATE campaigns SET status = 'draft', approved_by = NULL, approved_at = NULL WHERE id = ?", (c["id"],))
    audit("campaign.rejected", target=c["id"], detail=request.form.get("reason", "")[:300])
    flash("Sent back to draft.", "info")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


def launch_campaign(c, ws, start_at):
    """Snapshot eligible recipients and schedule step 0. Returns the number of recipients."""
    rep = segment_report(ws["id"], json.loads(c["segment"]))
    with transaction():
        for contact in rep["eligible"]:
            execute("INSERT OR IGNORE INTO campaign_recipients (campaign_id, contact_id, email, next_step, next_send_at, "
                    "created_at) VALUES (?, ?, ?, 0, ?, ?)", (c["id"], contact["id"], contact["email"], start_at, now()))
        execute("UPDATE campaigns SET status = 'scheduled', scheduled_at = ?, launched_at = ?, updated_at = ? WHERE id = ?",
                (start_at, now(), now(), c["id"]))
    return len(rep["eligible"])


@bp.route("/<int:campaign_id>/launch", methods=["POST"])
@require("approve_campaigns")
def launch(campaign_id):
    c = get_campaign(campaign_id)
    ws = workspace()
    if c["status"] != "approved":
        abort(409)
    when = request.form.get("scheduled_at", "").strip()
    start_at = now()
    if when:
        try:
            local = datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(c["timezone"]))
        except ValueError:
            flash("Enter a valid date and time.", "error")
            return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
        if local.astimezone(timezone.utc) > datetime.now(timezone.utc):
            start_at = fmt_ts(local)
    n = launch_campaign(c, ws, start_at)
    audit("campaign.launched", target=c["id"], detail=f"{n} recipients, start {start_at} UTC")
    flash(f"Scheduled for {n} recipients" + (" (practice run: nothing will really be sent)." if c["dry_run"] or ws["sandbox_mode"]
                                            else ". Sending starts within a minute of the start time, inside the sending window."),
          "success")
    jobs.enqueue("send_campaigns", dedupe_key="send_campaigns")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/send-now", methods=["POST"])
@bp.route("/<int:campaign_id>/send-later", methods=["POST"], endpoint="send_later")
@require("approve_campaigns")
def send_now(campaign_id):
    """One click for owners and admins: approve and start immediately. The same safety checks still apply,
    and clicking it is the human approval (recorded in the activity log)."""
    c = get_campaign(campaign_id)
    ws = workspace()
    if c["status"] not in EDITABLE:
        abort(409)
    if ws["four_eyes_approval"] and c["created_by"] == current_user()["id"]:
        flash("Your settings say a second person must approve campaigns, so ask a colleague to approve this one.", "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    items, _ = readiness(ws, c)
    blocking = [i["label"] for i in items if i["blocking"]]
    if blocking:
        flash("Not ready to send yet: " + "; ".join(blocking), "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    start_at = now()
    if request.form.get("scheduled_at"):
        try:
            local = datetime.fromisoformat(request.form["scheduled_at"]).replace(tzinfo=ZoneInfo(c["timezone"]))
        except ValueError:
            flash("Enter a valid date and time.", "error")
            return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
        if local.astimezone(timezone.utc) > datetime.now(timezone.utc):
            start_at = fmt_ts(local)
    execute("UPDATE campaigns SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?",
            (current_user()["id"], now(), now(), c["id"]))
    audit("campaign.approved", target=c["id"], detail="send now: " + ("practice run" if c["dry_run"] else "real sending"))
    c = get_campaign(campaign_id)
    n = launch_campaign(c, ws, start_at)
    audit("campaign.launched", target=c["id"], detail=f"{n} recipients, start {start_at} UTC")
    jobs.enqueue("send_campaigns", dedupe_key="send_campaigns")
    practice = c["dry_run"] or ws["sandbox_mode"]
    later = start_at > now()
    flash(f"{'Scheduled' if later else 'Started'} for {n} recipients." + (" Practice run: nothing is really sent." if practice else
          (f" Sending starts {start_at} UTC." if later else " The first emails go out within a minute (inside your sending hours).")),
          "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/pause", methods=["POST"])
@require("edit_campaigns")
def pause(campaign_id):
    c = get_campaign(campaign_id)
    if c["status"] not in ("scheduled", "sending"):
        abort(409)
    execute("UPDATE campaigns SET status = 'paused', pause_reason = ? WHERE id = ?",
            (f"Paused by {current_user()['full_name']}", c["id"]))
    audit("campaign.paused", target=c["id"])
    flash("Paused. Messages already accepted by the sending service can't be recalled.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/resume", methods=["POST"])
@require("approve_campaigns")
def resume(campaign_id):
    c = get_campaign(campaign_id)
    ws = workspace()
    if c["status"] != "paused":
        abort(409)
    if ws["sending_paused_reason"]:
        flash(f"Sending is paused for the whole workspace: {ws['sending_paused_reason']} Resolve it in the Deliverability Centre.",
              "error")
        return redirect(url_for("campaigns.detail", campaign_id=c["id"]))
    execute("UPDATE campaigns SET status = 'scheduled', pause_reason = '' WHERE id = ?", (c["id"],))
    audit("campaign.resumed", target=c["id"])
    flash("Resumed.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/cancel", methods=["POST"])
@require("edit_campaigns")
def cancel(campaign_id):
    c = get_campaign(campaign_id)
    if c["status"] in ("completed", "cancelled"):
        abort(409)
    with transaction():
        execute("UPDATE campaigns SET status = 'cancelled', cancelled_at = ? WHERE id = ?", (now(), c["id"]))
        execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = 'Campaign cancelled', next_send_at = NULL "
                "WHERE campaign_id = ? AND status = 'active'", (c["id"],))
        execute("UPDATE messages SET status = 'cancelled', next_attempt_at = NULL WHERE campaign_id = ? AND status = 'queued'",
                (c["id"],))
    audit("campaign.cancelled", target=c["id"])
    flash("Campaign cancelled. No further emails will be sent.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


@bp.route("/<int:campaign_id>/duplicate", methods=["POST"])
@require("edit_campaigns")
def duplicate(campaign_id):
    c = get_campaign(campaign_id)
    with transaction():
        cid = execute("INSERT INTO campaigns (workspace_id, name, template_id, segment, from_name, from_email, reply_to, timezone, "
                      "send_window_start, send_window_end, send_days, rate_per_minute, dry_run, ai_generated, created_by, "
                      "created_at, updated_at) SELECT workspace_id, name || ' (copy)', template_id, segment, from_name, "
                      "from_email, reply_to, timezone, send_window_start, send_window_end, send_days, rate_per_minute, 1, "
                      "ai_generated, ?, ?, ? FROM campaigns WHERE id = ?", (current_user()["id"], now(), now(), c["id"]))
        execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, preheader, html_body, text_body) "
                "SELECT ?, step_no, delay_days, subject, preheader, html_body, text_body FROM campaign_steps WHERE campaign_id = ?",
                (cid, c["id"]))
    flash("Copied as a new draft (set as a practice run).", "success")
    return redirect(url_for("campaigns.detail", campaign_id=cid))


@bp.route("/<int:campaign_id>/recipients/<int:recipient_id>/replied", methods=["POST"])
@require("edit_campaigns")
def mark_replied(campaign_id, recipient_id):
    c = get_campaign(campaign_id)
    r = query("SELECT * FROM campaign_recipients WHERE id = ? AND campaign_id = ?", (recipient_id, c["id"]), one=True)
    if not r:
        abort(404)
    record_reply(r, "Marked as replied by " + current_user()["full_name"])
    flash("Marked as replied. No more follow-ups will go to this contact in this campaign.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=c["id"]))


def record_reply(recipient, how):
    execute("UPDATE campaign_recipients SET replied_at = COALESCE(replied_at, ?), status = CASE WHEN status = 'active' "
            "THEN 'stopped' ELSE status END, stop_reason = CASE WHEN status = 'active' THEN 'Replied' ELSE stop_reason END, "
            "next_send_at = NULL WHERE id = ?", (now(), recipient["id"]))
    ws = query("SELECT workspace_id FROM campaigns WHERE id = ?", (recipient["campaign_id"],), one=True)["workspace_id"]
    add_event(ws, recipient["contact_id"], "replied", how, campaign_id=recipient["campaign_id"])


# ---------------------------------------------------------------- sending engine
def schedule_sending():
    """Called every worker cycle: queue one sending job if any campaign or retry needs attention."""
    due = query("SELECT 1 FROM campaigns WHERE status IN ('scheduled', 'sending') AND (scheduled_at IS NULL OR scheduled_at <= ?) "
                "LIMIT 1", (now(),), one=True) or \
        query("SELECT 1 FROM messages WHERE status = 'queued' AND category = 'marketing' AND (next_attempt_at <= ? OR "
              "(next_attempt_at IS NULL AND created_at < datetime('now', '-5 minutes'))) LIMIT 1", (now(),), one=True)
    if due:
        jobs.enqueue("send_campaigns", dedupe_key="send_campaigns", max_attempts=1)


def in_window(c, tz_name, at=None):
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(c["timezone"])
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    return str(local.weekday()) in c["send_days"].split(",") and c["send_window_start"] <= local.hour < c["send_window_end"]


def _sent_since(workspace_id, interval):
    return scalar("SELECT COUNT(*) FROM messages WHERE workspace_id = ? AND category = 'marketing' AND simulated = 0 "
                  "AND status NOT IN ('queued', 'cancelled', 'suppressed') AND created_at > datetime('now', ?)",
                  (workspace_id, interval))


def pause_workspace(ws, reason):
    execute("UPDATE workspaces SET sending_paused_reason = ? WHERE id = ?", (reason[:300], ws["id"]))
    execute("UPDATE campaigns SET status = 'paused', pause_reason = ? WHERE workspace_id = ? AND status IN ('scheduled', 'sending')",
            (reason[:300], ws["id"]))
    from .security import audit as _audit
    _audit("sending.auto_paused", detail=reason, workspace_id=ws["id"], user_id=0)
    try:
        from .transactional import send_admin_alert
        send_admin_alert(ws["id"], "Global Marketing paused your campaigns", reason)
    except Exception:
        current_app.logger.exception("Could not send the pause alert")


def check_thresholds(ws):
    """Pause the workspace if real bounce or complaint rates over the last 7 days exceed its thresholds."""
    row = query("SELECT COUNT(*) AS sent, SUM(status = 'bounced') AS bounced, SUM(status = 'complained' OR complained_at IS NOT NULL) "
                "AS complained FROM messages WHERE workspace_id = ? AND category = 'marketing' AND simulated = 0 "
                "AND status NOT IN ('queued', 'cancelled', 'suppressed', 'failed') AND created_at > datetime('now', '-7 days')",
                (ws["id"],), one=True)
    sent = row["sent"] or 0
    if sent < 50:  # too few messages for a meaningful rate
        return None
    bounce_pct = 100.0 * (row["bounced"] or 0) / sent
    complaint_pct = 100.0 * (row["complained"] or 0) / sent
    if bounce_pct > ws["bounce_pause_pct"]:
        return f"Hard bounce rate {bounce_pct:.1f}% over the last 7 days is above your {ws['bounce_pause_pct']}% limit."
    if complaint_pct > ws["complaint_pause_pct"]:
        return f"Spam complaint rate {complaint_pct:.2f}% over the last 7 days is above your {ws['complaint_pause_pct']}% limit."
    return None


def _payload(c, ws, contact, step, message_id):
    wsx = dict(ws, sender_name=c["from_name"] or ws["sender_name"])
    subj, html_out, text_out, headers = render.build_marketing(wsx, contact, step["subject"], step["html_body"],
                                                               step["text_body"], step["preheader"], message_id=message_id,
                                                               style=c["style"])
    payload = {"from": f"{c['from_name'] or ws['sender_name'] or ws['name']} <{c['from_email']}>", "to": [contact["email"]],
               "subject": subj, "html": html_out, "text": text_out, "headers": headers,
               "tags": [{"name": "category", "value": "marketing"}, {"name": "campaign", "value": str(c["id"])},
                        {"name": "step", "value": str(step["step_no"])}]}
    if c["reply_to"]:
        payload["reply_to"] = c["reply_to"]
    return payload


def _advance(c, recipient, step_no, simulated):
    nxt = query("SELECT delay_days FROM campaign_steps WHERE campaign_id = ? AND step_no = ?", (c["id"], step_no + 1), one=True)
    if nxt:
        execute("UPDATE campaign_recipients SET next_step = ?, next_send_at = datetime('now', ?) WHERE id = ? AND status = 'active'",
                (step_no + 1, f"+{nxt['delay_days']} days", recipient["id"]))
    else:
        execute("UPDATE campaign_recipients SET status = 'completed', next_send_at = NULL WHERE id = ? AND status = 'active'",
                (recipient["id"],))
    if not simulated:
        execute("UPDATE contacts SET last_contacted_at = ? WHERE id = ?", (now(), recipient["contact_id"]))


def _send_step(c, ws, provider, recipient, contact, step):
    key = f"camp-{c['id']}-rcp-{recipient['id']}-step-{step['step_no']}"
    mid, created = mailer.record_message(category="marketing", kind="campaign", to_email=contact["email"],
                                         subject=step["subject"], idempotency_key=key, workspace_id=ws["id"],
                                         campaign_id=c["id"], recipient_id=recipient["id"], contact_id=contact["id"],
                                         step_no=step["step_no"])
    if not created:
        existing = query("SELECT status FROM messages WHERE id = ?", (mid,), one=True)
        if existing["status"] != "queued":
            _advance(c, recipient, step["step_no"], existing["status"] == "simulated")  # already sent earlier
        return existing["status"], None
    result = provider.send(_payload(c, ws, contact, step, mid), key)
    status = mailer.apply_result(mid, provider, result)
    if status in ("accepted", "simulated"):
        _advance(c, recipient, step["step_no"], status == "simulated")
        add_event(ws["id"], contact["id"], "email_simulated" if status == "simulated" else "email_accepted",
                  f"{'Simulated' if status == 'simulated' else 'Accepted by the sending service'}: {step['subject'][:120]}",
                  campaign_id=c["id"], message_id=mid, simulated=status == "simulated")
    elif status == "failed":
        execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = ? WHERE id = ?",
                (f"Send failed: {result.error}"[:200], recipient["id"]))
        add_event(ws["id"], contact["id"], "email_failed", result.error[:300], campaign_id=c["id"], message_id=mid)
    return status, result


def _precheck(c, ws, recipient):
    """Re-check a recipient immediately before sending. Returns (contact, None) or (None, reason)."""
    contact = query("SELECT * FROM contacts WHERE id = ?", (recipient["contact_id"],), one=True)
    if not contact:
        return None, "Contact deleted"
    if query("SELECT 1 FROM suppressions WHERE workspace_id = ? AND email_normalized = ?", (ws["id"], contact["email_normalized"]),
             one=True):
        return None, "Suppressed"
    ok, why = eligibility(contact, json.loads(c["segment"]).get("include_risky"))
    return (contact, None) if ok else (None, why)


def process_campaign(c, ws, budget):
    """Send what is due for one campaign within the given budget. Returns (sent_count, stop_everything_reason)."""
    simulate = bool(c["dry_run"] or ws["sandbox_mode"])
    provider = mailer.SandboxProvider() if simulate else mailer.workspace_provider(ws)
    if not simulate and provider.name == "sandbox":
        return 0, "Real sending needs a Resend API key (Integrations) or the campaign must be a practice run."
    if c["status"] == "scheduled":
        execute("UPDATE campaigns SET status = 'sending' WHERE id = ? AND status = 'scheduled'", (c["id"],))
    limit = min(budget, c["rate_per_minute"])
    due = query("SELECT * FROM campaign_recipients WHERE campaign_id = ? AND status = 'active' AND next_send_at <= ? "
                "ORDER BY next_send_at, id LIMIT ?", (c["id"], now(), limit * 3))
    sent = 0
    for r in due:
        if sent >= limit:
            break
        if query("SELECT status FROM campaigns WHERE id = ?", (c["id"],), one=True)["status"] not in ("sending", "scheduled"):
            break  # paused or cancelled meanwhile
        contact, why = _precheck(c, ws, r)
        if why:
            execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = ?, next_send_at = NULL WHERE id = ?",
                    (why[:200], r["id"]))
            continue
        if not simulate:
            if not in_window(c, contact["timezone"] or c["timezone"]):
                continue
            last = parse_ts(contact["last_contacted_at"])
            gap = timedelta(hours=ws["min_hours_between_emails"])
            if last and datetime.now(timezone.utc) - last < gap:
                execute("UPDATE campaign_recipients SET next_send_at = ? WHERE id = ?", (fmt_ts(last + gap), r["id"]))
                continue
        step = query("SELECT * FROM campaign_steps WHERE campaign_id = ? AND step_no = ?", (c["id"], r["next_step"]), one=True)
        if not step:
            execute("UPDATE campaign_recipients SET status = 'completed', next_send_at = NULL WHERE id = ?", (r["id"],))
            continue
        status, result = _send_step(c, ws, provider, r, contact, step)
        if result is not None:
            sent += 1
            if result.auth_error:
                return sent, f"The sending service rejected the API key: {result.error}"
    remaining = scalar("SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'active'", (c["id"],))
    queued = scalar("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'", (c["id"],))
    if not remaining and not queued:
        execute("UPDATE campaigns SET status = 'completed', completed_at = ? WHERE id = ? AND status = 'sending'", (now(), c["id"]))
    return sent, None


def retry_messages(ws, budget):
    """Resend messages left 'queued' by a transient failure or a crashed worker, with the same idempotency key."""
    rows = query("SELECT * FROM messages WHERE workspace_id = ? AND category = 'marketing' AND status = 'queued' AND "
                 "(next_attempt_at <= ? OR (next_attempt_at IS NULL AND created_at < datetime('now', '-5 minutes'))) "
                 "ORDER BY id LIMIT ?", (ws["id"], now(), budget))
    n = 0
    for m in rows:
        c = query("SELECT * FROM campaigns WHERE id = ?", (m["campaign_id"],), one=True)
        r = query("SELECT * FROM campaign_recipients WHERE id = ?", (m["recipient_id"],), one=True)
        if not c or not r or c["status"] not in ("sending", "scheduled") or r["status"] != "active":
            execute("UPDATE messages SET status = 'cancelled', next_attempt_at = NULL WHERE id = ?", (m["id"],))
            continue
        contact, why = _precheck(c, ws, r)
        if why:
            execute("UPDATE messages SET status = 'suppressed', last_error = ?, next_attempt_at = NULL WHERE id = ?", (why, m["id"]))
            execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = ? WHERE id = ?", (why[:200], r["id"]))
            continue
        step = query("SELECT * FROM campaign_steps WHERE campaign_id = ? AND step_no = ?", (c["id"], m["step_no"]), one=True)
        simulate = bool(c["dry_run"] or ws["sandbox_mode"])
        provider = mailer.SandboxProvider() if simulate else mailer.workspace_provider(ws)
        result = provider.send(_payload(c, ws, contact, step, m["id"]), m["idempotency_key"])
        status = mailer.apply_result(m["id"], provider, result)
        n += 1
        if status in ("accepted", "simulated"):
            _advance(c, r, m["step_no"], status == "simulated")
        elif status == "failed":
            execute("UPDATE campaign_recipients SET status = 'stopped', stop_reason = ? WHERE id = ?",
                    (f"Send failed: {result.error}"[:200], r["id"]))
        if result.auth_error:
            pause_workspace(ws, f"The sending service rejected the API key: {result.error}")
            break
    return n


@jobs.handler("send_campaigns")
def _send_campaigns(ctx):
    total = 0
    for ws in query("SELECT * FROM workspaces WHERE id IN (SELECT workspace_id FROM campaigns WHERE status IN "
                    "('scheduled', 'sending'))  OR id IN (SELECT workspace_id FROM messages WHERE status = 'queued' AND "
                    "category = 'marketing')"):
        if ws["sending_paused_reason"]:
            continue
        reason = check_thresholds(ws)
        if reason:
            pause_workspace(ws, reason)
            continue
        hourly = ws["hourly_send_limit"] - _sent_since(ws["id"], "-1 hour")
        daily = ws["daily_send_limit"] - _sent_since(ws["id"], "-1 day")
        real_budget = max(0, min(hourly, daily))
        total += retry_messages(ws, max(real_budget, 0) or 0)
        for c in query("SELECT * FROM campaigns WHERE workspace_id = ? AND status IN ('scheduled', 'sending') AND "
                       "(scheduled_at IS NULL OR scheduled_at <= ?) ORDER BY id", (ws["id"], now())):
            ctx.check_cancel()
            simulate = bool(c["dry_run"] or ws["sandbox_mode"])
            budget = 500 if simulate else real_budget
            if budget <= 0:
                continue
            sent, stop = process_campaign(c, ws, budget)
            total += sent
            if not simulate:
                real_budget -= sent
            if stop:
                pause_workspace(ws, stop) if "API key" in stop else execute(
                    "UPDATE campaigns SET status = 'paused', pause_reason = ? WHERE id = ?", (stop[:300], c["id"]))
                break
    return {"processed": total}
