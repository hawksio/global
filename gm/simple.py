"""Compose and Sent: the everyday, Gmail-like way to send.

Compose is one page (To, Subject, Message, optional follow-up). Behind it a normal campaign is created and sent by the
background engine, so every protection still applies: the do-not-email list is checked right before each email, an
unsubscribe link is included, people without permission are skipped, sending is spread out, and bounces and spam
reports pause sending automatically.

Emails from Compose use the 'plain' style: they look like a message typed in Gmail (no banner, images or tracking),
which is what gives one-to-one business email the best chance of reaching the inbox.
"""
import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import jobs, render, validation
from .campaigns import launch_campaign, readiness, segment_report, steps_of
from .contacts import apply_tags, create_contact
from .db import execute, now, query, scalar, transaction
from .security import audit, can, current_user, require, workspace, ws_id

bp = Blueprint("simple", __name__)

READY_STATUSES = ["validated", "unknown"]
SIMPLE_STATUS = {"draft": "Draft", "pending_approval": "Waiting for approval", "approved": "Ready", "scheduled": "Scheduled",
                 "sending": "Sending", "paused": "Paused", "completed": "Sent", "cancelled": "Cancelled"}
FOLLOW_UP_TEXT = ("Hi {{first_name|there}},\n\nI'm following up on my last email in case it got buried. "
                  "Would it be worth a quick chat?\n\nBest regards,\n{{sender_name}}")


def text_to_html(text):
    import html
    paragraphs = [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "\n".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def _ready_count(wid, tag=None):
    seg = {"verification_status": READY_STATUSES, "status": ["active"], "exclude_sandbox": True}
    if tag:
        seg["tag"] = tag
    return len(segment_report(wid, seg)["eligible"])


def _parse_addresses(raw):
    found, bad = [], []
    for token in raw.replace(";", "\n").replace(",", "\n").split():
        token = token.strip().strip("<>")
        if not token:
            continue
        ok, value = validation.syntax_check(token)
        if ok and value.lower() not in found:
            found.append(value.lower())
        elif not ok:
            bad.append(token)
    return found, bad


def _friendly_problems(items, report):
    """Turn checklist items that block sending into plain instructions."""
    out = []
    for it in items:
        if not it["blocking"]:
            continue
        n = it["label"].split(".")[0]
        if n == "1":
            why = "; ".join(f"{k} ({v})" for k, v in report["excluded"].items())
            out.append("Nobody on this list can be emailed yet" + (f": {why}." if why else "."))
        elif n == "5":
            out.append("The email needs a fix: " + it["detail"])
        elif n == "7":
            out.append("Add your company's postal address in Settings → Company & sender (the law requires it in every "
                       "marketing email).")
        elif n == "9":
            out.append("Your sending domain isn't set up yet: " + it["detail"] + " See More tools → Reports → Email health.")
        elif n == "10":
            out.append("Sending is paused: " + it["detail"])
        else:
            out.append(f"{it['label'][3:]}: {it['detail']}")
    return out


@bp.route("/compose", methods=["GET", "POST"])
@require("edit_campaigns")
def compose():
    ws = workspace()
    wid = ws["id"]
    user = current_user()
    tags = [dict(name=t["name"], n=_ready_count(wid, t["name"]))
            for t in query("SELECT name FROM tags WHERE workspace_id = ? ORDER BY name", (wid,))]
    f = request.form
    ready_all = _ready_count(wid)
    form = {"to_mode": f.get("to_mode", request.args.get("to", "all" if ready_all else "paste")), "pasted": f.get("pasted", ""),
            "from_name": f.get("from_name", ws["sender_name"] or user["full_name"]), "subject": f.get("subject", ""),
            "body": f.get("body", ""), "follow_up": f.get("follow_up") == "on", "follow_days": f.get("follow_days", "3"),
            "follow_body": f.get("follow_body", FOLLOW_UP_TEXT), "when": f.get("when", "")}
    problems = []
    if request.method == "POST":
        action = f.get("action", "send")
        subject, body = form["subject"].strip()[:200], form["body"].strip()
        if not subject:
            problems.append("Add a subject.")
        if not body:
            problems.append("Write your message.")
        bad = render.unknown_fields(subject, body, form["follow_body"] if form["follow_up"] else "")
        if bad:
            problems.append("These names in {{ }} aren't recognised: " + ", ".join(bad) +
                            ". Use the “Insert first name” button instead.")
        if not ws["sender_email"]:
            problems.append("Set the email address you send from in Settings → Company & sender.")
        if problems:
            return render_template("simple/compose.html", form=form, tags=tags, problems=problems, ws=ws,
                                   ready_all=_ready_count(wid)), 400

        if action == "test":
            from .templates_lib import send_test
            wsx = dict(ws, sender_name=form["from_name"] or ws["sender_name"], reply_to=ws["reply_to"] or ws["sender_email"])
            status, error = send_test(wsx, user["email"], subject, text_to_html(body), body, kind="compose", style="plain")
            if status == "failed":
                flash(f"The test couldn't be sent: {error}", "error")
            elif status == "simulated":
                flash("Practice mode is on, so the test was saved, not sent. Turn it off in Settings → Connections.", "info")
            else:
                flash(f"Test sent to {user['email']}. Check your inbox (and the spam folder).", "success")
            return render_template("simple/compose.html", form=form, tags=tags, problems=[], ws=ws,
                                   ready_all=_ready_count(wid))

        # Who gets it
        segment = {"verification_status": READY_STATUSES, "status": ["active"], "exclude_sandbox": True}
        if form["to_mode"] == "paste":
            addresses, invalid = _parse_addresses(form["pasted"])
            if not addresses:
                problems.append("Type or paste at least one email address.")
            if f.get("permission") != "on":
                problems.append("Tick the box to confirm these are business contacts you have a reason to email.")
            if invalid:
                problems.append("These don't look like email addresses: " + ", ".join(invalid[:10]))
            if problems:
                return render_template("simple/compose.html", form=form, tags=tags, problems=problems, ws=ws,
                                       ready_all=_ready_count(wid)), 400
            tag = f"compose-{now()[:16].replace(' ', '-').replace(':', '')}"
            for email in addresses:
                cid, created = create_contact(wid, email, {}, "manual", purpose="Business email sent from Compose",
                                              source_detail=f"Typed in Compose by {user['full_name']}",
                                              recipient_type="business", lawful_basis="legitimate_interest")
                if created:
                    validation.store_on_contact(cid, validation.check(email, wid, dns=True))
                apply_tags(wid, cid, [tag])
            segment["tag"] = tag
        elif form["to_mode"].startswith("tag:"):
            segment["tag"] = form["to_mode"][4:]

        with transaction():
            cid = execute("INSERT INTO campaigns (workspace_id, name, status, segment, from_name, from_email, reply_to, timezone, "
                          "send_window_start, send_window_end, send_days, rate_per_minute, dry_run, style, created_by, "
                          "created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, 0, 24, '0,1,2,3,4,5,6', 2, 0, 'plain', "
                          "?, ?, ?)", (wid, subject, json.dumps(segment), form["from_name"].strip()[:100], ws["sender_email"],
                                       ws["reply_to"] or ws["sender_email"], ws["timezone"], user["id"], now(), now()))
            execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) "
                    "VALUES (?, 0, 0, ?, ?, ?)", (cid, subject, text_to_html(body), body))
            if form["follow_up"] and form["follow_body"].strip():
                days = max(1, min(int(form["follow_days"]) if str(form["follow_days"]).isdigit() else 3, 30))
                execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) "
                        "VALUES (?, 1, ?, ?, ?, ?)", (cid, days, subject, text_to_html(form["follow_body"]),
                                                     form["follow_body"].strip()))
        if not ws["sender_name"] and form["from_name"].strip():
            execute("UPDATE workspaces SET sender_name = ? WHERE id = ?", (form["from_name"].strip()[:100], wid))

        if not ws["sandbox_mode"]:
            from .deliverability import ensure_recent_check
            ensure_recent_check(ws, ws["sender_email"].rsplit("@", 1)[-1])
        c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
        items, report = readiness(workspace(), c)
        problems = _friendly_problems(items, report)
        if problems:
            execute("DELETE FROM campaigns WHERE id = ?", (cid,))  # nothing was sent; keep Sent tidy
            return render_template("simple/compose.html", form=form, tags=tags, problems=problems, ws=ws,
                                   ready_all=_ready_count(wid)), 400

        if not can("approve_campaigns"):
            execute("UPDATE campaigns SET status = 'pending_approval' WHERE id = ?", (cid,))
            audit("campaign.submitted", target=cid, detail="from Compose")
            flash("Sent to your administrator for approval. It goes out as soon as they approve it.", "success")
            return redirect(url_for("simple.sent_detail", campaign_id=cid))
        start_at = now()
        if action == "schedule" and form["when"]:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            from .db import fmt_ts
            try:
                local = datetime.fromisoformat(form["when"]).replace(tzinfo=ZoneInfo(ws["timezone"]))
                if local.astimezone(timezone.utc) > datetime.now(timezone.utc):
                    start_at = fmt_ts(local)
            except ValueError:
                pass
        execute("UPDATE campaigns SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
                (user["id"], now(), cid))
        audit("campaign.approved", target=cid, detail="sent from Compose")
        n = launch_campaign(query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True), workspace(), start_at)
        audit("campaign.launched", target=cid, detail=f"{n} recipients, start {start_at} UTC")
        jobs.enqueue("send_campaigns", dedupe_key="send_campaigns")
        practice = " (practice mode: nothing is really sent)" if ws["sandbox_mode"] else ""
        flash((f"Scheduled for {n} people{practice}." if start_at > now() else
               f"Sending to {n} people{practice}. Emails go out gently, about 2 a minute.") , "success")
        return redirect(url_for("simple.sent_detail", campaign_id=cid))
    return render_template("simple/compose.html", form=form, tags=tags, problems=problems, ws=ws, ready_all=_ready_count(wid))


def sent_page():
    wid = ws_id()
    rows = query(
        "SELECT c.*, (SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id = c.id) AS people, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND m.status NOT IN ('queued','cancelled','failed','suppressed')) AS sent, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND (m.status = 'delivered' OR m.delivered_at IS NOT NULL)) AS delivered, "
        "(SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id = c.id AND r.replied_at IS NOT NULL) AS replied, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND m.status IN ('bounced','failed')) AS problems, "
        "(SELECT text_body FROM campaign_steps s WHERE s.campaign_id = c.id AND s.step_no = 0) AS preview "
        "FROM campaigns c WHERE c.workspace_id = ? AND c.status != 'draft' ORDER BY COALESCE(c.launched_at, c.updated_at) DESC "
        "LIMIT 100", (wid,))
    from .main import onboarding_steps
    steps = onboarding_steps(workspace())
    next_step = next((s for s in steps if not s["done"]), None)
    drafts = scalar("SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND status = 'draft'", (wid,))
    return render_template("simple/sent.html", rows=rows, SIMPLE_STATUS=SIMPLE_STATUS, next_step=next_step, drafts=drafts,
                           ws=workspace())


@bp.route("/sent")
@require("view_campaigns")
def sent():
    return sent_page()


def _campaign(campaign_id):
    c = query("SELECT * FROM campaigns WHERE id = ? AND workspace_id = ?", (campaign_id, ws_id()), one=True)
    if not c:
        abort(404)
    return c


@bp.route("/sent/<int:campaign_id>")
@require("view_campaigns")
def sent_detail(campaign_id):
    c = _campaign(campaign_id)
    people = query("SELECT r.*, ct.first_name, ct.last_name, ct.company, "
                   "(SELECT m.status FROM messages m WHERE m.recipient_id = r.id ORDER BY m.id DESC LIMIT 1) AS last_status, "
                   "(SELECT m.last_error FROM messages m WHERE m.recipient_id = r.id ORDER BY m.id DESC LIMIT 1) AS last_error "
                   "FROM campaign_recipients r JOIN contacts ct ON ct.id = r.contact_id WHERE r.campaign_id = ? ORDER BY r.id",
                   (c["id"],))
    total = len(people)
    done = sum(1 for p in people if p["status"] != "active" or (p["last_status"] and p["next_step"] > 0))
    return render_template("simple/sent_detail.html", c=c, steps=steps_of(c["id"]), people=people, total=total, done=done,
                           SIMPLE_STATUS=SIMPLE_STATUS)


@bp.route("/sent/<int:campaign_id>/<action>", methods=["POST"])
@require("edit_campaigns")
def sent_action(campaign_id, action):
    from . import campaigns as cm
    c = _campaign(campaign_id)
    handlers = {"pause": cm.pause, "resume": cm.resume, "cancel": cm.cancel, "approve": cm.send_now}
    if action not in handlers:
        abort(404)
    if action in ("resume", "approve") and not can("approve_campaigns"):
        abort(403)
    handlers[action](c["id"])  # same rules as the full campaign page; we only change where you land afterwards
    return redirect(url_for("simple.sent_detail", campaign_id=c["id"]))
