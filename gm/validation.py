"""Free, local email hygiene: syntax, normalisation, DNS mail routing, disposable and role-based domains, and the
application's own history (bounces, complaints, unsubscribes).

What these checks can and cannot tell you:
  * "validated" means the address passed every check that was actually performed: correct syntax, a domain that is
    configured to receive mail, not disposable and not suppressed. It does NOT prove the mailbox exists or will
    accept a message. Only sending (and the bounce that may follow) or a specialist verification provider can
    give stronger evidence, and even then not with certainty.
  * We never probe SMTP servers (RCPT TO) to guess whether a mailbox exists.

Final categories: validated, invalid, risky, unknown, suppressed.
"""
import json
import os
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from flask import current_app

from .db import execute, now, query

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

ROLE_LOCALS = {
    "abuse", "accounts", "admin", "administrator", "billing", "careers", "contact", "contactus", "customerservice",
    "enquiries", "enquiry", "finance", "hello", "help", "helpdesk", "hostmaster", "hr", "info", "inquiries", "inquiry",
    "jobs", "legal", "mail", "marketing", "media", "news", "newsletter", "noc", "noreply", "no-reply", "office",
    "orders", "postmaster", "press", "privacy", "recruitment", "reception", "root", "sales", "security", "service",
    "shop", "support", "team", "webmaster", "welcome",
}

DNS_TTL_HOURS = {"ok": 24, "nxdomain": 24, "null_mx": 24, "no_mx": 12, "error": 1}
_disposable = None


# ---------------------------------------------------------------- reference data
def disposable_path():
    override = os.path.join(current_app.instance_path, "disposable_domains.txt")
    return override if os.path.exists(override) else os.path.join(DATA_DIR, "disposable_domains.txt")


def disposable_meta():
    for path in (os.path.join(current_app.instance_path, "disposable_domains.json"),
                 os.path.join(DATA_DIR, "disposable_domains.json")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
            meta["count"] = len(disposable_domains())
            return meta
    return {"source": "unknown", "count": len(disposable_domains())}


def disposable_domains():
    global _disposable
    if _disposable is None:
        with open(disposable_path(), encoding="utf-8") as fh:
            _disposable = {line.strip().lower() for line in fh if line.strip() and not line.startswith("#")}
    return _disposable


def is_disposable(domain):
    domain = domain.lower()
    domains = disposable_domains()
    parts = domain.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts) - 1))


def update_disposable_list(url="https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/main/"
                               "disposable_email_blocklist.conf"):
    """Download the maintained CC0 list into instance/ (the bundled copy stays as a fallback)."""
    import requests
    global _disposable
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    lines = [l.strip().lower() for l in resp.text.splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) < 1000:
        raise ValueError(f"Downloaded list looks wrong ({len(lines)} entries); keeping the current list.")
    with open(os.path.join(current_app.instance_path, "disposable_domains.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(current_app.instance_path, "disposable_domains.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": url, "license": "CC0-1.0", "updated": now() + " UTC"}, fh)
    _disposable = None
    return len(lines)


def is_role(local):
    base = local.lower().split("+")[0]
    return base in ROLE_LOCALS


# ---------------------------------------------------------------- stages A and B
def syntax_check(email):
    """Stage A + B. Returns (ok, normalized_or_error)."""
    email = (email or "").strip()
    if not email or len(email) > 254:
        return False, "Empty or too long"
    try:
        # Reserved domains (.test, .invalid, ...) are rejected; tests use .test addresses via test_environment.
        result = validate_email(email, check_deliverability=False, allow_smtputf8=True,
                                test_environment=bool(current_app.config.get("TESTING")))
    except EmailNotValidError as exc:
        return False, str(exc)
    return True, result.normalized


def normalize(email):
    """Key used for de-duplication: the validator's normal form, lower-cased.

    We do not strip dots or +tags: whether those reach the same mailbox depends on the provider, so merging them
    would be an unsafe assumption about mailbox identity.
    """
    ok, value = syntax_check(email)
    return value.lower() if ok else None


# ---------------------------------------------------------------- stage C
def _resolve(domain, rdtype):
    fake = current_app.config.get("DNS_RESOLVER")
    if fake:
        return fake(domain, rdtype)
    import dns.resolver
    resolver = dns.resolver.Resolver()
    resolver.lifetime = float(current_app.config["DNS_TIMEOUT"])
    answer = resolver.resolve(domain, rdtype)
    return [r.to_text() for r in answer]


def dns_mail_check(domain, use_cache=True):
    """Classify a domain's mail routing: ok | no_mx (A record only) | null_mx | nxdomain | error."""
    domain = domain.lower().rstrip(".")
    if use_cache:
        row = query("SELECT * FROM dns_cache WHERE domain = ? AND kind = 'mail'", (domain,), one=True)
        if row:
            age_h = (datetime.now(timezone.utc) -
                     datetime.strptime(row["checked_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                     ).total_seconds() / 3600
            if age_h < DNS_TTL_HOURS.get(row["status"], 1):
                return {"status": row["status"], "records": json.loads(row["records"]), "error": row["error"],
                        "checked_at": row["checked_at"], "cached": True}
    import dns.exception
    import dns.resolver
    status, records, error = "error", [], ""
    try:
        records = _resolve(domain, "MX")
        if any(r.split()[-1] == "." for r in records) and len(records) == 1:
            status = "null_mx"
        else:
            status = "ok"
    except dns.resolver.NXDOMAIN:
        status = "nxdomain"
    except dns.resolver.NoAnswer:
        # No MX: RFC 5321 falls back to the domain's address records ("implicit MX").
        status = "null_mx"  # unless an A/AAAA record is found below, nothing can receive mail here
        for rdtype in ("A", "AAAA"):
            try:
                records = _resolve(domain, rdtype)
                status = "no_mx"
                break
            except dns.resolver.NoAnswer:
                continue
            except dns.resolver.NXDOMAIN:
                status = "nxdomain"
                break
            except dns.exception.DNSException as exc:
                status, error = "error", type(exc).__name__
                break
    except dns.exception.DNSException as exc:  # timeouts, no nameservers, SERVFAIL
        error = type(exc).__name__
    execute("INSERT INTO dns_cache (domain, kind, status, records, error, checked_at) VALUES (?, 'mail', ?, ?, ?, ?) "
            "ON CONFLICT(domain, kind) DO UPDATE SET status = excluded.status, records = excluded.records, "
            "error = excluded.error, checked_at = excluded.checked_at", (domain, status, json.dumps(records[:10]), error, now()))
    return {"status": status, "records": records[:10], "error": error, "checked_at": now(), "cached": False}


# ---------------------------------------------------------------- stage F
def history(workspace_id, email_normalized):
    """Reasons the application must not send to this address (empty list = none)."""
    reasons = []
    row = query("SELECT reason FROM suppressions WHERE workspace_id = ? AND email_normalized = ?",
                (workspace_id, email_normalized), one=True)
    if row:
        reasons.append(f"On the suppression list ({row['reason'].replace('_', ' ')})")
    c = query("SELECT status, marketing_opt_out FROM contacts WHERE workspace_id = ? AND email_normalized = ?",
              (workspace_id, email_normalized), one=True)
    if c and c["status"] in ("unsubscribed", "bounced", "complained", "suppressed"):
        reasons.append(f"Contact status is {c['status']}")
    if c and c["marketing_opt_out"]:
        reasons.append("Opted out of marketing")
    return reasons


# ---------------------------------------------------------------- stage G
def check(email, workspace_id=None, dns=True):
    """Run every stage and classify. Returns a dict with the outcome and the evidence for each check."""
    checks = []

    def add(stage, name, result, detail=""):
        checks.append({"stage": stage, "check": name, "result": result, "detail": detail, "at": now()})

    ok, value = syntax_check(email)
    if not ok:
        add("A", "Syntax", "fail", value)
        return {"status": "invalid", "email": (email or "").strip(), "normalized": None, "checks": checks,
                "is_role": False, "is_disposable": False, "summary": f"Invalid syntax: {value}"}
    add("A", "Syntax", "pass", "Address follows supported email syntax")
    normalized = value.lower()
    add("B", "Normalisation", "pass", f"Normalised to {normalized}")
    local, domain = normalized.rsplit("@", 1)
    status, summary = "validated", "Passed the checks performed (this does not prove the mailbox exists)"

    if dns:
        res = dns_mail_check(domain)
        when = f" (checked {res['checked_at']} UTC{', cached' if res.get('cached') else ''})"
        if res["status"] == "ok":
            add("C", "Mail routing (DNS)", "pass", "MX: " + ", ".join(res["records"][:3]) + when)
        elif res["status"] == "no_mx":
            add("C", "Mail routing (DNS)", "warn", "No MX record; mail would fall back to the domain's A/AAAA address" + when)
            status, summary = "risky", "Domain has no MX record"
        elif res["status"] == "null_mx":
            add("C", "Mail routing (DNS)", "fail", "Domain publishes no way to receive email (null MX or no address records)" + when)
            status, summary = "invalid", "Domain does not accept email"
        elif res["status"] == "nxdomain":
            add("C", "Mail routing (DNS)", "fail", "Domain does not exist" + when)
            status, summary = "invalid", "Domain does not exist"
        else:
            add("C", "Mail routing (DNS)", "error", f"DNS lookup failed ({res['error'] or 'unknown error'}); try again later" + when)
            status, summary = "unknown", "DNS could not be checked right now"
    else:
        add("C", "Mail routing (DNS)", "skip", "Not checked")
        status, summary = "unknown", "Syntax only; domain mail routing not checked"

    disposable = is_disposable(domain)
    if disposable:
        add("D", "Disposable domain", "warn", f"{domain} is on the disposable-domain list")
        if status in ("validated", "unknown"):
            status, summary = "risky", "Disposable (temporary) email domain"
    else:
        add("D", "Disposable domain", "pass", "Not on the disposable-domain list")

    role = is_role(local)
    add("E", "Role-based address", "warn" if role else "pass",
        "Generic role address (e.g. info@, sales@): fine for business enquiries, but nobody personally opted in"
        if role else "Not a generic role address")

    if workspace_id is not None:
        reasons = history(workspace_id, normalized)
        if reasons:
            add("F", "Sending history", "fail", "; ".join(reasons))
            status, summary = "suppressed", reasons[0]
        else:
            add("F", "Sending history", "pass", "No bounces, complaints or unsubscribes on record")

    return {"status": status, "email": email.strip(), "normalized": normalized, "checks": checks, "is_role": role,
            "is_disposable": disposable, "summary": summary}


def store_on_contact(contact_id, result):
    execute("UPDATE contacts SET verification_status = ?, verification_checked_at = ?, verification_detail = ?, "
            "is_role = ?, is_disposable = ?, updated_at = ? WHERE id = ?",
            (result["status"], now(), json.dumps({"summary": result["summary"], "checks": result["checks"]}),
             int(result["is_role"]), int(result["is_disposable"]), now(), contact_id))
