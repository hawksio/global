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

# "unverified" = syntax and disposable-domain checked at import; the full (DNS) check runs in the background afterwards.
READY_STATUSES = ["validated", "unknown", "unverified"]
SPEEDS = {"2": "Gently: about 2 a minute (best for a new domain or a big list)",
          "10": "Normal: about 10 a minute", "30": "Fast: about 30 a minute (only for an established domain)"}
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


PROMO_WORDS = ["free", "offer", "discount", "% off", "sale", "deal", "limited time", "buy now", "order now", "promo",
               "coupon", "special price", "best price", "save ", "act now", "click here", "subscribe", "newsletter",
               "unsubscribe", "winner", "bonus", "cheap"]


def promotions_hints(subject, body):
    """Plain-language reasons an email may be sorted into Gmail's Promotions tab (none means it reads like a
    personal email). Gmail decides on its own; these only reduce the chance."""
    import re
    text = f"{subject}\n{body}".lower()
    hints = []
    words = [w for w in PROMO_WORDS if w in text]
    if words:
        hints.append("Salesy words like " + ", ".join(f"“{w.strip()}”" for w in words[:4]) +
                     " make Gmail think it's a promotion. Say it the way you would to a colleague.")
    risky = [w for w in ("loan", "credit", "debt", "investment", "guaranteed", "cash", "earn", "income", "crypto",
                         "bitcoin", "casino", "viagra") if re.search(rf"\b{w}", text)]
    if risky:
        hints.append("Words like " + ", ".join(f"“{w}”" for w in risky[:3]) + " are watched closely by spam filters. "
                     "Describe what you do in plain terms, e.g. “business financing” instead of “loan”, and avoid it in the subject.")
    links = len(re.findall(r"https?://|www\.", text))
    if links > 1:
        hints.append(f"{links} links: keep it to one (or none) in a first email.")
    if re.search(r"[$€£₦]\s?\d|\d+\s?%", text):
        hints.append("Prices or percentages read like an advert; save them for when they reply.")
    if subject.isupper() or re.search(r"\b[A-Z]{4,}\b", subject) or "!" in subject:
        hints.append("Keep the subject calm: no capitals or exclamation marks.")
    if len(body.split()) > 180:
        hints.append("It's long for a first email; 50–120 words reads more personal.")
    if "{{first_name" not in body:
        hints.append("Greet people by name: use “Insert first name”.")
    return hints


def _friendly_problems(items, report):
    """Turn checklist items that block sending into plain instructions."""
    out = []
    for it in items:
        if not it["blocking"]:
            continue
        n = it["label"].split(".")[0]
        if n == "1":
            why = "; ".join(f"{k} ({v})" for k, v in report["excluded"].items())
            out.append("Nobody on this list can be emailed yet" + (f": {why}." if why else ". Add contacts first, or choose "
                       "“Type email addresses”."))
        elif n == "5":
            out.append("The email needs a fix: " + it["detail"])
        elif n == "7":
            out.append("Fill in your business address in the box at the top. It appears in small print at the bottom of "
                       "your emails, which the law requires.")
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
    f = request.form
    draft = None
    draft_id = f.get("draft_id", type=int) or request.args.get("draft", type=int)
    if draft_id:
        draft = query("SELECT * FROM campaigns WHERE id = ? AND workspace_id = ? AND status = 'draft' AND style = 'plain'",
                      (draft_id, wid), one=True)
        if not draft:
            abort(404)

    # The business-contact statement stays visible under typed addresses but starts ticked; unticking it blocks sending.
    confirmed_before = True

    # --- what's on the form
    if request.method == "POST":
        form = {"to_mode": f.get("to_mode", "all"), "pasted": f.get("pasted", ""), "from_name": f.get("from_name", ""),
                "subject": f.get("subject", ""), "body": f.get("body", ""), "follow_up": f.get("follow_up") == "on",
                "follow_days": f.get("follow_days", "3"), "follow_body": f.get("follow_body", FOLLOW_UP_TEXT),
                "when": f.get("when", ""), "permission": f.get("permission") == "on",
                "speed": f.get("speed", "10") if f.get("speed") in SPEEDS else "10",
                "company_address": f.get("company_address", ""), "sender_email": f.get("sender_email", "")}
    elif draft:
        seg = json.loads(draft["segment"])
        steps = steps_of(draft["id"])
        form = {"to_mode": seg.get("_to_mode", "all"), "pasted": seg.get("_pasted", ""), "from_name": draft["from_name"],
                "subject": steps[0]["subject"] if steps else draft["name"], "body": steps[0]["text_body"] if steps else "",
                "follow_up": len(steps) > 1, "follow_days": str(steps[1]["delay_days"]) if len(steps) > 1 else "3",
                "follow_body": steps[1]["text_body"] if len(steps) > 1 else FOLLOW_UP_TEXT, "when": "",
                "permission": confirmed_before, "company_address": "", "sender_email": "",
                "speed": str(draft["rate_per_minute"]) if str(draft["rate_per_minute"]) in SPEEDS else "10"}
    else:
        ready = _ready_count(wid)
        form = {"to_mode": request.args.get("to", "all" if ready else "paste"), "pasted": "",
                "from_name": ws["sender_name"] or user["full_name"], "subject": "", "body": "", "follow_up": False,
                "follow_days": "3", "follow_body": FOLLOW_UP_TEXT, "when": "", "permission": confirmed_before,
                "company_address": "", "sender_email": "", "speed": "10"}

    def page(problems=(), code=200, hints=None):
        current = workspace()
        tags = [dict(name=t["name"], n=_ready_count(wid, t["name"]))
                for t in query("SELECT name FROM tags WHERE workspace_id = ? AND name NOT LIKE 'compose-%' ORDER BY name", (wid,))]
        no_permission = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND sandbox = 0 AND status = 'active' "
                               "AND lawful_basis = 'unknown'", (wid,))
        return render_template("simple/compose.html", form=form, tags=tags, problems=list(problems), ws=current, SPEEDS=SPEEDS,
                               ready_all=_ready_count(wid), draft=draft, no_permission=no_permission, hints=hints), code

    if request.method != "POST":
        return page()

    action = f.get("action", "send")

    # --- details that may be missing from Settings can be filled in right here
    updates = {}
    if form["company_address"].strip() and not ws["company_address"]:
        updates["company_address"] = form["company_address"].strip()[:300]
    if form["sender_email"].strip() and not ws["sender_email"]:
        ok, value = validation.syntax_check(form["sender_email"])
        if ok:
            updates["sender_email"] = value.lower()
            updates["reply_to"] = ws["reply_to"] or value.lower()
    if not ws["sender_name"] and form["from_name"].strip():
        updates["sender_name"] = form["from_name"].strip()[:100]
    if updates:
        execute("UPDATE workspaces SET " + ", ".join(f"{k} = ?" for k in updates) + " WHERE id = ?", (*updates.values(), wid))
        audit("settings.from_compose", detail=", ".join(updates))
        from flask import g
        g.pop("workspace", None)  # settings changed: don't reuse the cached copy
        ws = workspace()

    subject, body = form["subject"].strip()[:200], form["body"].strip()

    # --- Save draft: keep whatever is there, no checks
    if action == "save":
        cid = _save_draft(ws, user, form, draft)
        flash("Draft saved. Find it in Sent → Drafts.", "success")
        return redirect(url_for("simple.compose", draft=cid))

    problems = []
    if not form["from_name"].strip():
        problems.append("Add your name in From (people see it in their inbox).")
    if not subject:
        problems.append("Add a subject.")
    if not body:
        problems.append("Write your message.")
    bad = render.unknown_fields(subject, body, form["follow_body"] if form["follow_up"] else "")
    if bad:
        problems.append("These names in {{ }} aren't recognised: " + ", ".join(bad) +
                        ". Use the “Insert first name” button instead.")
    if not ws["sender_email"]:
        problems.append("Fill in the email address you send from (box at the top).")
    if problems:
        return page(problems, 400)

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
        return page(hints=promotions_hints(subject, body) or ["Reads like a personal email. Nothing to change."])

    if form["to_mode"] == "paste":
        addresses, invalid = _parse_addresses(form["pasted"])
        if not addresses:
            problems.append("Type or paste at least one email address.")
        if not form["permission"] and not _already_ok(wid, addresses):
            problems.append("Tick “These are business contacts I have a genuine reason to email” under the addresses.")
        if invalid:
            problems.append("These don't look like email addresses: " + ", ".join(invalid[:10]))
        if problems:
            return page(problems, 400)

    cid = _save_draft(ws, user, form, draft)
    if form["to_mode"] == "paste":
        audit("compose.business_confirmed", detail=f"{len(addresses)} typed addresses")
    if form["to_mode"] == "paste":
        tag = f"compose-{cid}"
        for email in addresses:
            contact_id, created = create_contact(wid, email, {}, "manual", purpose="Business email sent from Compose",
                                                 source_detail=f"Typed in Compose by {user['full_name']}",
                                                 recipient_type="business", lawful_basis="legitimate_interest")
            if created:
                validation.store_on_contact(contact_id, validation.check(email, wid, dns=True))
            apply_tags(wid, contact_id, [tag])
        seg = json.loads(query("SELECT segment FROM campaigns WHERE id = ?", (cid,), one=True)["segment"])
        seg["tag"] = tag
        execute("UPDATE campaigns SET segment = ? WHERE id = ?", (json.dumps(seg), cid))

    if not ws["sandbox_mode"]:
        from .deliverability import ensure_recent_check
        ensure_recent_check(ws, ws["sender_email"].rsplit("@", 1)[-1])
    c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
    items, report = readiness(workspace(), c)
    problems = _friendly_problems(items, report)
    if problems:
        draft = c  # keep their work: it stays as a draft
        return page(problems, 400)

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
    execute("UPDATE campaigns SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?", (user["id"], now(), cid))
    audit("campaign.approved", target=cid, detail="sent from Compose")
    n = launch_campaign(query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True), workspace(), start_at)
    audit("campaign.launched", target=cid, detail=f"{n} recipients, start {start_at} UTC")
    jobs.enqueue("send_campaigns", dedupe_key="send_campaigns")
    first = 0
    if start_at <= now():
        from .campaigns import send_first_batch
        first = send_first_batch(cid)
    practice = " (practice mode: nothing is really sent)" if ws["sandbox_mode"] else ""
    held = scalar("SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'active' "
                  "AND wait_reason LIKE 'Already emailed%'", (cid,))
    if held:
        flash(f"{held} {'person was' if held == 1 else 'people were'} emailed in the last "
              f"{ws['min_hours_between_emails']} hours, so they'll get this later. You can send now anyway from the "
              "list below, or change the gap in Settings > Advanced.", "info")
    rate = int(form["speed"])
    left = max(0, n - first)
    eta = "" if not left else f" The other {left} follow at about {rate} a minute (around {max(1, -(-left // rate))} min)."
    flash((f"Scheduled for {n} people{practice}." if start_at > now() else
           f"Sending to {n} people{practice}. {first} sent straight away.{eta}"), "success")
    return redirect(url_for("simple.sent_detail", campaign_id=cid))


def _already_ok(wid, addresses):
    """True when every typed address is a teammate or a contact already confirmed as a business contact."""
    for email in addresses:
        e = email.strip().lower()
        if not (scalar("SELECT 1 FROM users WHERE workspace_id = ? AND lower(email) = ?", (wid, e)) or
                scalar("SELECT 1 FROM contacts WHERE workspace_id = ? AND email_normalized = ? AND recipient_type = 'business' "
                       "AND lawful_basis IN ('legitimate_interest', 'consent', 'contract')", (wid, e))):
            return False
    return True


def _save_draft(ws, user, form, draft):
    """Create or update the draft behind this Compose page. Returns its id."""
    subject = form["subject"].strip()[:200] or "(no subject)"
    body = form["body"].strip()
    segment = {"verification_status": READY_STATUSES, "status": ["active"], "exclude_sandbox": True,
               "_to_mode": form["to_mode"], "_pasted": form["pasted"][:20000] if form["to_mode"] == "paste" else ""}
    if form["to_mode"].startswith("tag:"):
        segment["tag"] = form["to_mode"][4:]
    with transaction():
        if draft:
            cid = draft["id"]
            execute("UPDATE campaigns SET name = ?, segment = ?, from_name = ?, from_email = ?, reply_to = ?, "
                    "rate_per_minute = ?, updated_at = ? WHERE id = ?",
                    (subject, json.dumps(segment), form["from_name"].strip()[:100], ws["sender_email"],
                     ws["reply_to"] or ws["sender_email"], int(form["speed"]), now(), cid))
            execute("DELETE FROM campaign_steps WHERE campaign_id = ?", (cid,))
        else:
            cid = execute("INSERT INTO campaigns (workspace_id, name, status, segment, from_name, from_email, reply_to, timezone, "
                          "send_window_start, send_window_end, send_days, rate_per_minute, dry_run, style, created_by, "
                          "created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, 0, 24, '0,1,2,3,4,5,6', ?, 0, 'plain', "
                          "?, ?, ?)", (ws["id"], subject, json.dumps(segment), form["from_name"].strip()[:100],
                                       ws["sender_email"], ws["reply_to"] or ws["sender_email"], ws["timezone"],
                                       int(form["speed"]), user["id"], now(), now()))
        execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) "
                "VALUES (?, 0, 0, ?, ?, ?)", (cid, subject, text_to_html(body), body))
        if form["follow_up"] and form["follow_body"].strip():
            days = max(1, min(int(form["follow_days"]) if str(form["follow_days"]).isdigit() else 3, 30))
            execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body, text_body) "
                    "VALUES (?, 1, ?, ?, ?, ?)", (cid, days, subject, text_to_html(form["follow_body"]),
                                                 form["follow_body"].strip()))
    return cid


@bp.route("/compose/drafts/<int:campaign_id>/delete", methods=["POST"])
@require("edit_campaigns")
def delete_draft(campaign_id):
    execute("DELETE FROM campaigns WHERE id = ? AND workspace_id = ? AND status = 'draft' AND style = 'plain'",
            (campaign_id, ws_id()))
    flash("Draft deleted.", "success")
    return redirect(url_for("simple.sent"))


@bp.route("/compose/mark-business", methods=["POST"])
@require("edit_contacts")
def mark_business():
    """One click for contacts added without a recorded permission: confirm they're business contacts."""
    wid = ws_id()
    n = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND sandbox = 0 AND status = 'active' "
               "AND lawful_basis = 'unknown'", (wid,))
    execute("UPDATE contacts SET lawful_basis = 'legitimate_interest', recipient_type = CASE WHEN recipient_type = 'unknown' "
            "THEN 'business' ELSE recipient_type END, updated_at = ? WHERE workspace_id = ? AND sandbox = 0 AND status = 'active' "
            "AND lawful_basis = 'unknown'", (now(), wid))
    audit("contacts.marked_business", detail=f"{n} contacts", workspace_id=wid)
    flash(f"{n} contacts marked as business contacts you can email.", "success")
    return redirect(request.referrer or url_for("simple.compose"))


SENT_FOLDERS = {"all": "All", "sending": "Sending", "scheduled": "Scheduled", "sent": "Sent", "drafts": "Drafts",
                "problems": "Stopped / paused"}
FOLDER_SQL = {"all": "(c.status != 'draft' OR c.style = 'plain')", "sending": "c.status = 'sending'", "scheduled": "c.status IN ('scheduled', "
              "'approved', 'pending_approval')", "sent": "c.status = 'completed'", "drafts": "c.status = 'draft' AND c.style = 'plain'",
              "problems": "c.status IN ('paused', 'cancelled')"}


def sent_page():
    wid = ws_id()
    folder = request.args.get("folder", "all")
    folder = folder if folder in SENT_FOLDERS else "all"
    q = request.args.get("q", "").strip()
    where, args = [f"c.workspace_id = ?", FOLDER_SQL[folder]], [wid]
    if q:
        where.append("(lower(c.name) LIKE ? OR c.id IN (SELECT campaign_id FROM campaign_steps WHERE lower(text_body) LIKE ?) "
                     "OR c.id IN (SELECT campaign_id FROM campaign_recipients WHERE lower(email) LIKE ?))")
        args += [f"%{q.lower()}%"] * 3
    rows = query(
        "SELECT c.*, (SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id = c.id) AS people, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND m.status NOT IN ('queued','cancelled','failed','suppressed')) AS sent, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND (m.status = 'delivered' OR m.delivered_at IS NOT NULL)) AS delivered, "
        "(SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id = c.id AND r.replied_at IS NOT NULL) AS replied, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id AND m.status IN ('bounced','failed')) AS problems, "
        "(SELECT text_body FROM campaign_steps s WHERE s.campaign_id = c.id AND s.step_no = 0) AS preview "
        f"FROM campaigns c WHERE {' AND '.join(where)} ORDER BY COALESCE(c.launched_at, c.updated_at) DESC LIMIT 200", args)
    counts = {k: scalar(f"SELECT COUNT(*) FROM campaigns c WHERE c.workspace_id = ? AND {v}", (wid,)) for k, v in FOLDER_SQL.items()}
    from .main import onboarding_steps
    steps = onboarding_steps(workspace())
    next_step = next((s for s in steps if not s["done"]), None)
    advanced_drafts = scalar("SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND status = 'draft' AND style != 'plain'", (wid,))
    return render_template("simple/sent.html", rows=rows, SIMPLE_STATUS=SIMPLE_STATUS, next_step=next_step,
                           drafts=advanced_drafts, folder=folder, q=q, FOLDERS=SENT_FOLDERS, counts=counts, ws=workspace())


def _delete_campaigns(wid, ids):
    """Delete emails from Sent: stop anything still going, then remove the email and its sending log.
    Do-not-email entries and contacts are not touched."""
    n = 0
    for cid in ids:
        c = query("SELECT * FROM campaigns WHERE id = ? AND workspace_id = ?", (cid, wid), one=True)
        if not c:
            continue
        with transaction():
            execute("UPDATE messages SET status = 'cancelled', next_attempt_at = NULL WHERE campaign_id = ? AND status = 'queued'",
                    (cid,))
            execute("DELETE FROM messages WHERE campaign_id = ?", (cid,))
            execute("DELETE FROM campaigns WHERE id = ?", (cid,))
        audit("campaign.deleted", target=cid, detail=c["name"][:100])
        n += 1
    return n


@bp.route("/sent/delete", methods=["POST"])
@require("edit_campaigns")
def sent_delete():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if request.form.get("folder_all") in FOLDER_SQL:
        ids = [r["id"] for r in query(f"SELECT c.id FROM campaigns c WHERE c.workspace_id = ? AND "
                                      f"{FOLDER_SQL[request.form['folder_all']]}", (ws_id(),))]
    if not ids:
        flash("Tick at least one email first.", "error")
        return redirect(request.referrer or url_for("simple.sent"))
    n = _delete_campaigns(ws_id(), ids)
    flash(f"Deleted {n} email{'s' if n != 1 else ''}. Anything still sending was stopped first.", "success")
    return redirect(url_for("simple.sent", folder=request.form.get("folder", "all")))


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


@bp.route("/sent/<int:campaign_id>/people/<int:recipient_id>/now", methods=["POST"])
@require("approve_campaigns")
def person_now(campaign_id, recipient_id):
    """Skip the minimum-gap rule for this one person and send right away."""
    from .campaigns import send_first_batch
    c = _campaign(campaign_id)
    r = query("SELECT * FROM campaign_recipients WHERE id = ? AND campaign_id = ? AND status = 'active'",
              (recipient_id, c["id"]), one=True)
    if not r:
        abort(404)
    execute("UPDATE campaign_recipients SET ignore_gap = 1, next_send_at = ?, wait_reason = '' WHERE id = ?", (now(), r["id"]))
    audit("campaign.gap_override", target=r["email"], detail=f"campaign {c['id']}")
    sent = send_first_batch(c["id"])
    flash(f"Sent to {r['email']}." if sent else "Queued: it goes out within a minute.", "success")
    return redirect(url_for("simple.sent_detail", campaign_id=c["id"]))


@bp.route("/sent/<int:campaign_id>/people/<int:recipient_id>/stop", methods=["POST"])
@require("edit_campaigns")
def person_stop(campaign_id, recipient_id):
    """They replied asking not to be emailed again (the opt-out offered in every personal email)."""
    from .suppression import add_suppression
    c = _campaign(campaign_id)
    r = query("SELECT r.*, ct.email_normalized FROM campaign_recipients r JOIN contacts ct ON ct.id = r.contact_id "
              "WHERE r.id = ? AND r.campaign_id = ?", (recipient_id, c["id"]), one=True)
    if not r:
        abort(404)
    add_suppression(c["workspace_id"], r["email_normalized"], "unsubscribe", "Replied asking to stop")
    audit("contact.opted_out_by_reply", target=r["email_normalized"])
    flash(f"{r['email']} won't be emailed again.", "success")
    return redirect(url_for("simple.sent_detail", campaign_id=c["id"]))


@bp.route("/sent/<int:campaign_id>/<action>", methods=["POST"])
@require("edit_campaigns")
def sent_action(campaign_id, action):
    from . import campaigns as cm
    c = _campaign(campaign_id)
    if action == "delete":
        _delete_campaigns(c["workspace_id"], [c["id"]])
        flash("Email deleted.", "success")
        return redirect(url_for("simple.sent"))
    handlers = {"pause": cm.pause, "resume": cm.resume, "cancel": cm.cancel, "approve": cm.send_now}
    if action not in handlers:
        abort(404)
    if action in ("resume", "approve") and not can("approve_campaigns"):
        abort(403)
    handlers[action](c["id"])  # same rules as the full campaign page; we only change where you land afterwards
    return redirect(url_for("simple.sent_detail", campaign_id=c["id"]))
