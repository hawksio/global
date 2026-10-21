"""Analytics and reporting from the application's own records. Nothing is estimated or invented: a metric that
can't be measured is shown as unavailable. Opens are approximate (privacy features and image proxies distort them).
Simulated (sandbox) activity is reported separately and never mixed into real figures.
"""
import csv
import io

from flask import Blueprint, Response, render_template, request

from .ai import PRICE_PER_MTOK
from .contacts import csv_safe
from .db import query, scalar
from .security import require, ws_id

bp = Blueprint("analytics", __name__, url_prefix="/analytics")


def _msg_filter(wid, f, simulated):
    where, args = ["m.workspace_id = ?", "m.category = 'marketing'", "m.simulated = ?"], [wid, int(simulated)]
    if f.get("from"):
        where.append("m.created_at >= ?")
        args.append(f["from"] + " 00:00:00")
    if f.get("to"):
        where.append("m.created_at <= ?")
        args.append(f["to"] + " 23:59:59")
    if f.get("campaign"):
        where.append("m.campaign_id = ?")
        args.append(int(f["campaign"]))
    if f.get("template"):
        where.append("m.campaign_id IN (SELECT id FROM campaigns WHERE template_id = ?)")
        args.append(int(f["template"]))
    if f.get("tag"):
        where.append("m.contact_id IN (SELECT ct.contact_id FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id "
                     "WHERE t.workspace_id = ? AND t.name = ?)")
        args += [wid, f["tag"]]
    return " AND ".join(where), args


def message_stats(wid, f, simulated=False):
    where, args = _msg_filter(wid, f, simulated)
    r = query(f"SELECT COUNT(*) AS total, SUM(status = 'queued') AS queued, SUM(status NOT IN ('queued', 'cancelled', "
              f"'suppressed', 'failed')) AS accepted, SUM(status = 'delivered' OR delivered_at IS NOT NULL) AS delivered, "
              f"SUM(status = 'bounced') AS hard, SUM(status = 'soft_bounced') AS soft, SUM(complained_at IS NOT NULL) AS complaints, "
              f"SUM(status = 'failed') AS failed, SUM(status = 'cancelled') AS cancelled, SUM(status = 'suppressed') AS suppressed, "
              f"SUM(opened_at IS NOT NULL) AS opened, SUM(clicked_at IS NOT NULL) AS clicked, "
              f"SUM(CASE WHEN attempts > 1 THEN attempts - 1 ELSE 0 END) AS retries FROM messages m WHERE {where}", args, one=True)
    out = {k: (r[k] or 0) for k in r.keys()}
    out["webhooks_seen"] = bool(scalar("SELECT 1 FROM webhook_events WHERE workspace_id = ? LIMIT 1", (wid,))) or simulated
    return out


def campaign_stats(campaign_id, wid):
    s = message_stats(wid, {"campaign": campaign_id}, simulated=False)
    sim = message_stats(wid, {"campaign": campaign_id}, simulated=True)
    r = query("SELECT COUNT(*) AS recipients, SUM(status = 'active') AS active, SUM(status = 'completed') AS completed, "
              "SUM(status = 'stopped') AS stopped, SUM(replied_at IS NOT NULL) AS replied FROM campaign_recipients "
              "WHERE campaign_id = ?", (campaign_id,), one=True)
    rec = {k: (r[k] or 0) for k in r.keys()}
    unsub = scalar("SELECT COUNT(*) FROM contact_events WHERE campaign_id IS NULL AND kind = 'suppressed' AND detail LIKE "
                   "'Unsubscribed%' AND contact_id IN (SELECT contact_id FROM campaign_recipients WHERE campaign_id = ?)",
                   (campaign_id,))
    return {"real": s, "simulated": sim, "recipients": rec, "unsubscribes": unsub}


def rate(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def rule_summary(stats):
    s, rec = stats["real"], stats["recipients"]
    if not s["total"] and stats["simulated"]["total"]:
        return (f"This campaign has only run as a practice run ({stats['simulated']['total']} simulated messages). No real "
                "results exist yet.")
    if not s["total"]:
        return "Nothing has been sent yet, so there is nothing to summarise."
    parts = [f"{s['accepted']} of {rec['recipients']} recipients' emails were accepted by the sending service."]
    if s["webhooks_seen"]:
        parts.append(f"Delivered to the receiving server: {s['delivered']} ({rate(s['delivered'], s['accepted'])}); "
                     f"hard bounces {s['hard']} ({rate(s['hard'], s['accepted'])}); complaints {s['complaints']}.")
        parts.append(f"Approximate opens: {s['opened']} ({rate(s['opened'], s['delivered'])}); clicks {s['clicked']}.")
    else:
        parts.append("Delivery, bounce and open figures are unavailable because no webhook events have been received.")
    parts.append(f"Replies recorded: {rec['replied']}. Unsubscribes: {stats['unsubscribes']}.")
    if s["accepted"] < 100:
        parts.append("The sample is small, so treat the rates as indicative only.")
    return " ".join(parts)


def rule_recommendations(stats):
    s = stats["real"]
    tips = []
    if not s["total"]:
        return ["Send to a small, well-qualified segment first; results will appear here once messages go out."]
    if not s["webhooks_seen"]:
        tips.append("Set up the Resend webhook (Integrations) so delivery, bounces and complaints are tracked.")
    if s["accepted"] and s["hard"] / s["accepted"] > 0.02:
        tips.append("Hard bounces are above 2%: verify the list (Email Verification) and remove old or guessed addresses.")
    if s["accepted"] and s["complaints"] / s["accepted"] > 0.001:
        tips.append("Complaints are above 0.1%: make sure recipients expect your email, and target more precisely.")
    if s["delivered"] and s["opened"] / s["delivered"] < 0.1:
        tips.append("Opens look low (approximate): test a clearer, more specific subject line on a small group.")
    if s["failed"]:
        tips.append(f"{s['failed']} messages failed permanently; check the error column on the campaign page.")
    if s["accepted"] < 100:
        tips.append("With fewer than 100 messages, differences are mostly noise; avoid big conclusions.")
    return tips or ["No problems stand out in the available data."]


@bp.route("/")
@require("view_analytics")
def index():
    wid = ws_id()
    f = {k: request.args.get(k, "").strip() for k in ("from", "to", "campaign", "template", "tag")}
    f = {k: v for k, v in f.items() if v and (k not in ("campaign", "template") or v.isdigit())}
    real = message_stats(wid, f)
    sim = message_stats(wid, f, simulated=True)
    date_where, date_args = "", []
    if f.get("from"):
        date_where += " AND created_at >= ?"
        date_args.append(f["from"] + " 00:00:00")
    if f.get("to"):
        date_where += " AND created_at <= ?"
        date_args.append(f["to"] + " 23:59:59")
    contacts = {
        "total": scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ?", (wid,)),
        "imported": scalar(f"SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND source = 'import'{date_where}", (wid, *date_args)),
        "discovered": scalar(f"SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND source = 'discovery'{date_where}",
                             (wid, *date_args)),
        "found_by_discovery": scalar(f"SELECT COUNT(*) FROM discovery_results WHERE workspace_id = ?"
                                     f"{date_where.replace('created_at', 'found_at')}", (wid, *date_args)),
        "duplicates_removed": scalar("SELECT COALESCE(SUM(duplicates), 0) FROM imports WHERE workspace_id = ?", (wid,)),
        "invalid_rejected": scalar("SELECT COALESCE(SUM(invalid), 0) FROM imports WHERE workspace_id = ?", (wid,)),
    }
    by_status = {r["verification_status"]: r["n"] for r in query(
        "SELECT verification_status, COUNT(*) AS n FROM contacts WHERE workspace_id = ? GROUP BY verification_status", (wid,))}
    camp = query("SELECT status, COUNT(*) AS n FROM campaigns WHERE workspace_id = ? GROUP BY status", (wid,))
    ai = query(f"SELECT provider, COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o, SUM(cost_micro_usd) AS cost "
               f"FROM ai_usage WHERE workspace_id = ?{date_where} GROUP BY provider", (wid, *date_args))
    verifications = scalar(f"SELECT COALESCE(SUM(total), 0) FROM jobs WHERE workspace_id = ? AND kind IN ('verify_csv', "
                           f"'verify_contacts') AND status = 'completed'{date_where}", (wid, *date_args))
    unsubscribes = scalar(f"SELECT COUNT(*) FROM suppressions WHERE workspace_id = ? AND reason = 'unsubscribe' AND simulated = 0"
                          f"{date_where}", (wid, *date_args))
    replies = scalar("SELECT COUNT(*) FROM campaign_recipients r JOIN campaigns c ON c.id = r.campaign_id WHERE "
                     "c.workspace_id = ? AND r.replied_at IS NOT NULL", (wid,))
    timeline = query("SELECT e.*, ct.email FROM contact_events e JOIN contacts ct ON ct.id = e.contact_id WHERE e.workspace_id = ? "
                     "ORDER BY e.id DESC LIMIT 40", (wid,))
    per_campaign = query("SELECT c.id, c.name, c.status, c.dry_run, (SELECT COUNT(*) FROM campaign_recipients r WHERE "
                         "r.campaign_id = c.id) AS recipients FROM campaigns c WHERE c.workspace_id = ? ORDER BY c.id DESC LIMIT 25",
                         (wid,))
    if request.args.get("format") == "csv":
        return _csv(wid, f, real, sim, contacts, by_status, unsubscribes, replies)
    return render_template("analytics/index.html", f=f, real=real, sim=sim, contacts=contacts, by_status=by_status, camp=camp,
                           ai=ai, verifications=verifications, unsubscribes=unsubscribes, replies=replies, timeline=timeline,
                           per_campaign=[dict(p, stats=campaign_stats(p["id"], wid)) for p in per_campaign],
                           campaigns=query("SELECT id, name FROM campaigns WHERE workspace_id = ? ORDER BY id DESC", (wid,)),
                           templates=query("SELECT id, name FROM templates WHERE workspace_id = ? ORDER BY name", (wid,)),
                           tags=query("SELECT name FROM tags WHERE workspace_id = ? ORDER BY name", (wid,)), rate=rate,
                           prices=PRICE_PER_MTOK)


def _csv(wid, f, real, sim, contacts, by_status, unsubscribes, replies):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "metric", "value", "note"])
    w.writerow(["filters", "applied", csv_safe(", ".join(f"{k}={v}" for k, v in f.items()) or "none"), ""])
    for k, v in contacts.items():
        w.writerow(["contacts", k, v, ""])
    for k, v in by_status.items():
        w.writerow(["verification", k, v, "local checks only"])
    for k, v in real.items():
        if k != "webhooks_seen":
            w.writerow(["messages (real)", k, v, "approximate" if k == "opened" else
                        ("" if real["webhooks_seen"] or k in ("total", "queued", "accepted", "failed", "cancelled", "suppressed",
                                                             "retries") else "unavailable: no webhook events received")])
    for k, v in sim.items():
        if k != "webhooks_seen":
            w.writerow(["messages (SIMULATED - not real)", k, v, "sandbox"])
    w.writerow(["engagement", "unsubscribes", unsubscribes, ""])
    w.writerow(["engagement", "replies recorded", replies, "manual marks and matched inbound replies only"])
    for c in query("SELECT id, name FROM campaigns WHERE workspace_id = ? ORDER BY id", (wid,)):
        s = campaign_stats(c["id"], wid)
        w.writerow([f"campaign {c['id']}", csv_safe(c["name"]), s["real"]["accepted"], f"delivered {s['real']['delivered']}, "
                    f"hard bounces {s['real']['hard']}, complaints {s['real']['complaints']}, replies {s['recipients']['replied']}"])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=analytics.csv"})
