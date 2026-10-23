"""Overview dashboard, onboarding wizard, global search and the Transactional Email section."""
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import mailer
from .db import execute, query, scalar
from .security import audit, can, current_user, login_required, require, workspace, ws_id

bp = Blueprint("main", __name__)


def onboarding_steps(ws):
    wid = ws["id"]
    from .security import get_secret
    return [
        {"key": "company", "label": "Add your company name and address", "done": bool(ws["company_name"] and ws["company_address"]),
         "help": "Your company name and postal address appear in every marketing email (a legal requirement in many countries).",
         "url": url_for("main.onboarding", step="company")},
        {"key": "sender", "label": "Choose who your emails come from", "done": bool(ws["sender_email"] and ws["sender_name"]),
         "help": "The name and address your emails come from. Use a domain you control.",
         "url": url_for("main.onboarding", step="sender")},
        {"key": "resend", "label": "Connect your email service (or keep practising)", "done": bool(get_secret(wid, "resend_api_key")),
         "help": "Until you connect Resend, everything is a practice run and nothing is really sent.",
         "url": url_for("settings.integrations")},
        {"key": "domain", "label": "Check your email domain is set up", "done": bool(scalar("SELECT 1 FROM dns_checks WHERE workspace_id = ? "
                                                                                  "AND score >= 60", (wid,))),
         "help": "A few DNS records let Gmail, Outlook and others trust your email.", "url": url_for("deliverability.index")},
        {"key": "contacts", "label": "Add your contacts", "done": bool(scalar("SELECT 1 FROM contacts WHERE workspace_id = ? "
                                                                                     "AND sandbox = 0", (wid,))),
         "help": "Upload a CSV or find addresses published on company websites.", "url": url_for("contacts.import_upload")},
        {"key": "template", "label": "Write your first email", "done": bool(scalar("SELECT 1 FROM templates WHERE workspace_id = ?", (wid,))),
         "help": "Write it yourself or let the AI assistant draft one for you to edit.", "url": url_for("templates.new")},
        {"key": "campaign", "label": "Send your first campaign (start with a practice run)",
         "done": bool(scalar("SELECT 1 FROM campaigns WHERE workspace_id = ? AND launched_at IS NOT NULL", (wid,))),
         "help": "New campaigns start as practice runs, so nothing is sent until you're ready.", "url": url_for("campaigns.edit")},
    ]


@bp.route("/")
@login_required
def overview():
    """Home looks like Gmail's Sent folder: what you've sent, with a big Compose button."""
    user = current_user()
    if not user["workspace_id"]:
        return redirect(url_for("admin.system"))
    from .simple import sent_page
    return sent_page()


@bp.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if not user["workspace_id"]:
        return redirect(url_for("admin.system"))
    ws = workspace()
    wid = ws["id"]
    from .deliverability import health
    stats = {
        "contacts": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ?", (wid,)),
        "eligible_hint": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND status = 'active' AND "
                                "verification_status = 'validated' AND lawful_basis != 'unknown'", (wid,)),
        "needs_check": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND verification_status IN "
                              "('unverified', 'unknown')", (wid,)),
        "review": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND verification_status = 'risky'", (wid,)),
        "suppressed": scalar("SELECT COUNT(*) FROM suppressions WHERE workspace_id = ?", (wid,)),
        "pending_discovery": scalar("SELECT COUNT(*) FROM discovery_results WHERE workspace_id = ? AND status = 'pending'", (wid,)),
        "awaiting_approval": scalar("SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND status = 'pending_approval'", (wid,)),
        "sending": scalar("SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND status IN ('scheduled', 'sending')", (wid,)),
        "sandbox_contacts": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND sandbox = 1", (wid,)),
    }
    active_jobs = query("SELECT * FROM jobs WHERE workspace_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 5",
                        (wid,))
    campaigns = query("SELECT * FROM campaigns WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 5", (wid,))
    steps = onboarding_steps(ws)
    return render_template("main/overview.html", ws=ws, stats=stats, h=health(wid, 30), active_jobs=active_jobs,
                           campaigns=campaigns, steps=steps, steps_done=sum(s["done"] for s in steps))


@bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    ws = workspace()
    if ws is None:
        return redirect(url_for("admin.system"))
    step = request.values.get("step", "company")
    if request.method == "POST":
        if not can("manage_settings"):
            flash("Only owners and administrators can change these settings.", "error")
            return redirect(url_for("main.onboarding"))
        f = request.form
        if step == "company":
            execute("UPDATE workspaces SET company_name = ?, company_address = ?, website = ?, timezone = ? WHERE id = ?",
                    (f.get("company_name", "").strip()[:120] or ws["name"], f.get("company_address", "").strip()[:300],
                     f.get("website", "").strip()[:200], f.get("timezone", "UTC") if _tz_ok(f.get("timezone", "UTC")) else "UTC",
                     ws["id"]))
            audit("settings.company")
            return redirect(url_for("main.onboarding", step="sender"))
        if step == "sender":
            from .validation import syntax_check
            email = f.get("sender_email", "").strip().lower()
            if email and not syntax_check(email)[0]:
                flash("Enter a valid sender email address.", "error")
                return redirect(url_for("main.onboarding", step="sender"))
            execute("UPDATE workspaces SET sender_name = ?, sender_email = ?, reply_to = ? WHERE id = ?",
                    (f.get("sender_name", "").strip()[:100], email, f.get("reply_to", "").strip()[:200], ws["id"]))
            audit("settings.sender")
            execute("UPDATE users SET onboarding_done = 1 WHERE id = ?", (current_user()["id"],))
            flash("Saved. Next, connect Resend or keep using the sandbox to try things out.", "success")
            return redirect(url_for("main.overview"))
    return render_template("main/onboarding.html", ws=ws, step=step, steps=onboarding_steps(ws))


def _tz_ok(name):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


@bp.route("/search")
@login_required
def search():
    wid = ws_id()
    q = request.args.get("q", "").strip()
    results = {"contacts": [], "campaigns": [], "templates": []}
    if len(q) >= 2:
        like = f"%{q.lower()}%"
        if can("view_contacts"):
            results["contacts"] = query("SELECT id, email, first_name, last_name, company FROM contacts WHERE workspace_id = ? AND "
                                        "(email_normalized LIKE ? OR lower(first_name || ' ' || last_name) LIKE ? OR "
                                        "lower(company) LIKE ?) LIMIT 20", (wid, like, like, like))
        results["campaigns"] = query("SELECT id, name, status FROM campaigns WHERE workspace_id = ? AND lower(name) LIKE ? LIMIT 10",
                                     (wid, like))
        results["templates"] = query("SELECT id, name, subject FROM templates WHERE workspace_id = ? AND (lower(name) LIKE ? OR "
                                     "lower(subject) LIKE ?) LIMIT 10", (wid, like, like))
    return render_template("main/search.html", q=q, results=results)


@bp.route("/transactional", methods=["GET", "POST"])
@require("transactional")
def transactional():
    """Transactional email: separate configuration, templates and log. Bodies are never stored."""
    user = current_user()
    if request.method == "POST":
        kind = request.form.get("kind")
        from . import transactional as tx
        sent = {"security_alert": lambda: tx.send_security_alert(user, "This is a test of security notifications."),
                "admin_alert": lambda: tx.send(user["email"], "Test campaign alert", "admin_alert", "admin_alert_test",
                                               user_id=user["id"], name=user["full_name"],
                                               body="This is a test of campaign administration alerts.")}.get(kind)
        if not sent:
            flash("Choose a notification type.", "error")
        else:
            status = sent()
            flash({"simulated": "Written to the local outbox (no transactional sending key is configured).",
                   "accepted": "Accepted by the sending service. Check your inbox.",
                   "failed": "The email could not be sent. See the log below."}.get(status, status),
                  "error" if status == "failed" else "success")
        return redirect(url_for("main.transactional"))
    log = query("SELECT * FROM messages WHERE category = 'transactional' AND (workspace_id = ? OR to_email IN (SELECT email FROM "
                "users WHERE workspace_id = ?)) ORDER BY id DESC LIMIT 50", (user["workspace_id"], user["workspace_id"]))
    provider = mailer.transactional_provider()
    sender = current_app.config["TRANSACTIONAL_FROM"]
    m = re.search(r"<([^>]+)>", sender)
    return render_template("main/transactional.html", log=log, provider=provider.name, sender=sender,
                           sender_domain=(m.group(1) if m else sender).rsplit("@", 1)[-1])
