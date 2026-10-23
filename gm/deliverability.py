"""Deliverability Centre: sender authentication checks (real DNS lookups), Resend domain status, sender-health
rates and the automatic safety pause.

The readiness score is the weighted sum of the explicit checks listed on the page. It measures configuration, not
inbox placement: no score can guarantee where a message lands.
"""
import json
import random

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import mailer
from .db import execute, now, query, scalar
from .security import audit, can, current_user, require, workspace, ws_id

bp = Blueprint("deliverability", __name__, url_prefix="/deliverability")

WEIGHTS = {"spf": 20, "dkim": 30, "dmarc": 20, "return_path": 10, "mx": 5, "provider": 15}
DOCS = {
    "resend_domains": "https://resend.com/docs/dashboard/domains/introduction",
    "dmarc": "https://resend.com/docs/dashboard/domains/dmarc",
    "spf": "https://datatracker.ietf.org/doc/html/rfc7208",
    "bulk_senders": "https://support.google.com/a/answer/81126",
    "yahoo": "https://senders.yahooinc.com/best-practices/",
}


def _txt(name):
    from .validation import _resolve
    import dns.exception
    import dns.resolver
    try:
        return "ok", [r.strip('"').replace('" "', "") for r in _resolve(name, "TXT")]
    except dns.resolver.NXDOMAIN:
        return "missing", []
    except dns.resolver.NoAnswer:
        return "missing", []
    except dns.exception.DNSException as exc:
        return "error", [type(exc).__name__]


def _mx(name):
    from .validation import _resolve
    import dns.exception
    import dns.resolver
    try:
        return "ok", _resolve(name, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return "missing", []
    except dns.exception.DNSException as exc:
        return "error", [type(exc).__name__]


def run_checks(ws, domain):
    """Look up the sender domain's records now. Returns {check: {status, evidence, advice}}."""
    r = {}
    st, recs = _txt(domain)
    spf_root = [x for x in recs if x.lower().startswith("v=spf1")]
    st2, recs2 = _txt(f"send.{domain}")
    spf_send = [x for x in recs2 if x.lower().startswith("v=spf1")]
    if st == "error" and st2 == "error":
        r["spf"] = {"status": "unknown", "evidence": "DNS lookup failed: " + ", ".join(recs), "advice": "Try again later."}
    elif any("amazonses.com" in x for x in spf_send) or any("amazonses.com" in x or "resend" in x for x in spf_root):
        r["spf"] = {"status": "pass", "evidence": "; ".join(spf_send or spf_root),
                    "advice": "SPF authorises Resend's sending servers."}
    elif spf_root or spf_send:
        r["spf"] = {"status": "warn", "evidence": "; ".join(spf_root + spf_send),
                    "advice": "An SPF record exists but doesn't include Resend. Add the SPF record Resend shows for your domain "
                              "(on the send. subdomain)."}
    else:
        r["spf"] = {"status": "fail", "evidence": "No SPF record found on the domain or send. subdomain",
                    "advice": "Add the TXT record Resend shows for your domain."}
    st, recs = _txt(f"resend._domainkey.{domain}")
    dkim = [x for x in recs if "p=" in x]
    r["dkim"] = ({"status": "pass", "evidence": f"resend._domainkey.{domain}: {dkim[0][:60]}…",
                  "advice": "DKIM signing key published."} if dkim else
                 {"status": "unknown" if st == "error" else "fail", "evidence": f"No DKIM key at resend._domainkey.{domain}",
                  "advice": "Add the DKIM TXT record from your Resend domain settings. Gmail and Yahoo require DKIM for bulk senders."})
    st, recs = _txt(f"_dmarc.{domain}")
    dmarc = [x for x in recs if x.lower().startswith("v=dmarc1")]
    if dmarc:
        policy = next((p.split("=", 1)[1].strip().lower() for p in dmarc[0].split(";") if p.strip().lower().startswith("p=")), "")
        r["dmarc"] = {"status": "pass" if policy in ("quarantine", "reject") else "warn", "evidence": dmarc[0],
                      "advice": "DMARC is enforced." if policy in ("quarantine", "reject") else
                      "DMARC exists with p=none (monitoring only). That meets the minimum bulk-sender requirement; move to "
                      "quarantine once reports look clean."}
    else:
        r["dmarc"] = {"status": "unknown" if st == "error" else "fail", "evidence": f"No DMARC record at _dmarc.{domain}",
                      "advice": "Add a TXT record at _dmarc with at least: v=DMARC1; p=none; rua=mailto:you@yourdomain"}
    st, recs = _mx(f"send.{domain}")
    r["return_path"] = ({"status": "pass", "evidence": ", ".join(recs), "advice": "Bounce handling (return-path) is set up."}
                        if st == "ok" else {"status": "unknown" if st == "error" else "fail",
                                            "evidence": f"No MX record on send.{domain}",
                                            "advice": "Add the MX record Resend shows for the send subdomain so bounces are processed."})
    st, recs = _mx(domain)
    r["mx"] = ({"status": "pass", "evidence": ", ".join(recs[:3]), "advice": "Your domain can receive replies."}
               if st == "ok" else {"status": "warn", "evidence": "No MX record on the domain",
                                   "advice": "Without MX records, replies to your sender address can't be received."})
    domains = None
    if not ws["sandbox_mode"]:
        prov = mailer.workspace_provider(ws)
        domains = prov.domains() if prov.name == "resend" else None
    if domains is None:
        r["provider"] = {"status": "unknown", "evidence": "Practice mode or no Resend API key, so the provider was not asked",
                         "advice": "Connect Resend under Integrations to check the domain's verification status."}
    else:
        match = next((d for d in domains if d.get("name") == domain), None)
        r["provider"] = ({"status": "pass", "evidence": f"Resend reports {domain} as {match.get('status')}",
                          "advice": "Domain verified with Resend."} if match and match.get("status") == "verified" else
                         {"status": "fail", "evidence": f"Resend reports {domain} as {match.get('status')}" if match else
                          f"{domain} is not added in your Resend account", "advice": "Add and verify the domain in Resend."})
    return r


def score(results):
    pts = sum(WEIGHTS[k] * (1 if v["status"] == "pass" else 0.5 if v["status"] == "warn" else 0) for k, v in results.items())
    return int(round(pts))


def latest_check(workspace_id, domain):
    row = query("SELECT * FROM dns_checks WHERE workspace_id = ? AND domain = ? ORDER BY id DESC LIMIT 1",
                (workspace_id, domain), one=True)
    if not row:
        return None
    return {"score": row["score"], "results": json.loads(row["results"]), "checked_at": row["checked_at"]}


def health(workspace_id, days=30):
    row = query("SELECT COUNT(*) AS sent, SUM(status = 'delivered') AS delivered, SUM(status = 'bounced') AS hard, "
                "SUM(status = 'soft_bounced') AS soft, SUM(complained_at IS NOT NULL) AS complaints, "
                "SUM(status = 'failed') AS failed, SUM(attempts > 1) AS retried, SUM(status = 'suppressed') AS suppressed "
                "FROM messages WHERE workspace_id = ? AND category = 'marketing' AND simulated = 0 AND status NOT IN "
                "('queued', 'cancelled') AND created_at > datetime('now', ?)", (workspace_id, f"-{days} days"), one=True)
    sent = row["sent"] or 0

    def pct(n):
        return round(100.0 * (n or 0) / sent, 2) if sent else None
    unsub = scalar("SELECT COUNT(*) FROM suppressions WHERE workspace_id = ? AND reason = 'unsubscribe' AND simulated = 0 "
                   "AND created_at > datetime('now', ?)", (workspace_id, f"-{days} days"))
    daily = query("SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM messages WHERE workspace_id = ? AND "
                  "category = 'marketing' AND simulated = 0 AND created_at > datetime('now', '-14 days') GROUP BY day ORDER BY day",
                  (workspace_id,))
    return {"sent": sent, "delivered": row["delivered"] or 0, "hard": row["hard"] or 0, "soft": row["soft"] or 0,
            "complaints": row["complaints"] or 0, "failed": row["failed"] or 0, "retried": row["retried"] or 0,
            "unsubscribes": unsub, "bounce_pct": pct(row["hard"]), "complaint_pct": pct(row["complaints"]),
            "failure_pct": pct(row["failed"]), "retry_pct": pct(row["retried"]), "unsub_pct": pct(unsub), "daily": daily,
            "suppression_total": scalar("SELECT COUNT(*) FROM suppressions WHERE workspace_id = ?", (workspace_id,)),
            "webhook_events": scalar("SELECT COUNT(*) FROM webhook_events WHERE workspace_id = ? AND received_at > "
                                     "datetime('now', ?)", (workspace_id, f"-{days} days"))}


@bp.route("/", methods=["GET", "POST"])
@require("view_deliverability")
def index():
    ws = workspace()
    domain = (request.values.get("domain") or (ws["sender_email"].rsplit("@", 1)[-1] if "@" in ws["sender_email"] else "")).lower()
    if request.method == "POST":
        if not can("manage_integrations") and not can("edit_campaigns"):
            abort(403)
        from .discovery import normalize_target
        host = normalize_target(domain)
        if not host:
            flash("Enter your sending domain, e.g. example.com.", "error")
            return redirect(url_for("deliverability.index"))
        results = run_checks(ws, host)
        execute("INSERT INTO dns_checks (workspace_id, domain, results, score, checked_at) VALUES (?, ?, ?, ?, ?)",
                (ws["id"], host, json.dumps(results), score(results), now()))
        audit("deliverability.checked", target=host, detail=f"score {score(results)}")
        return redirect(url_for("deliverability.index", domain=host))
    check = latest_check(ws["id"], domain) if domain else None
    history = query("SELECT domain, score, checked_at FROM dns_checks WHERE workspace_id = ? ORDER BY id DESC LIMIT 10", (ws["id"],))
    return render_template("deliverability/index.html", domain=domain, check=check, history=history, h=health(ws["id"]),
                           WEIGHTS=WEIGHTS, DOCS=DOCS, ws=ws)


@bp.route("/resume", methods=["POST"])
@require("manage_settings")
def resume_sending():
    ws = workspace()
    if request.form.get("confirm") != "on":
        flash("Confirm that you have investigated the cause before resuming.", "error")
        return redirect(url_for("deliverability.index"))
    execute("UPDATE workspaces SET sending_paused_reason = '' WHERE id = ?", (ws["id"],))
    audit("sending.resumed", detail=ws["sending_paused_reason"])
    flash("Workspace sending resumed. Paused campaigns must each be resumed from their own page.", "success")
    return redirect(url_for("deliverability.index"))


@bp.route("/simulate-events", methods=["POST"])
@require("edit_campaigns")
def simulate_events():
    """Sandbox only: generate clearly labelled fake delivery events for simulated messages, to try the reporting."""
    from .webhooks import apply_event
    ws = workspace()
    rows = query("SELECT * FROM messages WHERE workspace_id = ? AND simulated = 1 AND status = 'simulated' ORDER BY id LIMIT 500",
                 (ws["id"],))
    rng = random.Random()
    for m in rows:
        roll = rng.random()
        etype, data = "email.delivered", {"email_id": m["provider_id"]}
        if roll < 0.03:
            etype, data["bounce"] = "email.bounced", {"type": "Permanent", "subType": "General",
                                                      "message": "Simulated hard bounce"}
        elif roll < 0.05:
            etype, data["bounce"] = "email.bounced", {"type": "Temporary", "subType": "General",
                                                      "message": "Simulated soft bounce"}
        elif roll < 0.052:
            etype = "email.complained"
        apply_event(ws["id"], etype, data, simulated=True)
        if etype == "email.delivered" and rng.random() < 0.3:
            apply_event(ws["id"], "email.opened", data, simulated=True)
    flash(f"Generated simulated events for {len(rows)} simulated messages. They are labelled 'simulated' everywhere and "
          "excluded from real sender-health figures.", "info")
    return redirect(request.referrer or url_for("analytics.index"))


def ensure_recent_check(ws, domain, max_age_hours=24):
    """Run the DNS checks for a sending domain if there's no check from the last day. Returns the latest check."""
    from .db import parse_ts
    from datetime import datetime, timezone
    check = latest_check(ws["id"], domain)
    fresh = check and (datetime.now(timezone.utc) - parse_ts(check["checked_at"])).total_seconds() < max_age_hours * 3600
    if domain and not fresh:
        results = run_checks(ws, domain)
        execute("INSERT INTO dns_checks (workspace_id, domain, results, score, checked_at) VALUES (?, ?, ?, ?, ?)",
                (ws["id"], domain, json.dumps(results), score(results), now()))
        check = latest_check(ws["id"], domain)
    return check
