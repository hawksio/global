"""Discover Contacts: find email addresses that businesses publish on their own public websites.

Rules this crawler follows:
  * Only the target company's own site (same host), only public pages, only http(s) on ports 80/443.
  * robots.txt is fetched and obeyed (including Crawl-delay); pages it disallows are never fetched.
  * Conservative limits: a few pages per domain, a delay between requests, short timeouts, a size cap, and a
    daily per-workspace domain quota. Pages are cached for a day so re-runs don't hit sites again.
  * No logins, CAPTCHAs, paywalls or other protections are bypassed. Requests to private or internal network
    addresses are refused (SSRF protection), including after redirects.
  * Only addresses actually written on a page (text or mailto: links) are collected. Nothing is guessed from
    name patterns and nothing is attributed to a person.
  * Results wait for human review before they become contacts, and keep the page they came from.

Users remain responsible for checking each site's terms and the law that applies to their outreach.
"""
import html
import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for

from . import jobs, validation
from .contacts import LAWFUL_BASES, RECIPIENT_TYPES, apply_tags, create_contact, csv_safe
from .db import execute, now, query, scalar
from .security import audit, current_user, require, ws_id

bp = Blueprint("discovery", __name__, url_prefix="/discover")

MAX_DOMAINS_PER_JOB = 50
MAX_BYTES = 2 * 1024 * 1024
LINK_HINTS = re.compile(r"contact|about|team|people|staff|impressum|imprint|legal|kontakt|company|support|office|locations",
                        re.I)
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
                       "yourcompany.com", "company.com", "test.com", "sentry.io", "sentry-next.wixpress.com",
                       "wixpress.com", "localhost"}
ASSET_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?)$", re.I)


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- safe fetching
def _public_host(host):
    """Refuse hosts that resolve to private, loopback, link-local or otherwise internal addresses."""
    if current_app.config.get("CRAWL_ALLOW_PRIVATE"):  # tests only
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise Blocked("Host name does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise Blocked("Refusing to connect to a private or internal network address")


def _check_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked("Only http and https URLs are allowed")
    if parts.port not in (None, 80, 443):
        raise Blocked("Only standard web ports are allowed")
    if parts.username or parts.password:
        raise Blocked("URLs with credentials are not allowed")
    _public_host(parts.hostname)


def fetch(url, accept_html=True):
    """GET with redirect checks, timeouts and a size cap. Returns (status, final_url, text)."""
    fake = current_app.config.get("CRAWL_FETCH")
    if fake:
        return fake(url)
    import requests
    session = requests.Session()
    headers = {"User-Agent": current_app.config["CRAWL_USER_AGENT"], "Accept": "text/html,text/plain;q=0.8"}
    timeout = float(current_app.config["CRAWL_TIMEOUT"])
    for _ in range(4):
        _check_url(url)
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, resp.headers.get("Location", ""))
            resp.close()
            continue
        ctype = resp.headers.get("Content-Type", "")
        if accept_html and "html" not in ctype and "text/plain" not in ctype:
            resp.close()
            return resp.status_code, url, ""
        body = b""
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        resp.close()
        return resp.status_code, url, body.decode(resp.encoding or "utf-8", errors="replace")
    raise Blocked("Too many redirects")


def cached_fetch(url):
    row = query("SELECT * FROM page_cache WHERE url = ? AND fetched_at > datetime('now', '-1 day')", (url,), one=True)
    if row:
        return row["status"], url, row["body"], True
    status, final, text = fetch(url)
    execute("INSERT INTO page_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
            "status = excluded.status, body = excluded.body, fetched_at = excluded.fetched_at", (url, status, text[:MAX_BYTES], now()))
    return status, final, text, False


def robots_for(base):
    rp = urllib.robotparser.RobotFileParser()
    try:
        status, _, text = fetch(urljoin(base, "/robots.txt"), accept_html=False)
    except Blocked:
        raise
    except Exception:
        status, text = 0, ""
    if status in (401, 403):
        rp.disallow_all = True          # robots.txt itself is protected: treat the site as off limits
    elif 200 <= status < 300:
        rp.parse(text.splitlines())
    else:
        rp.allow_all = True             # no robots.txt (404) or unreachable: standard practice is to allow
    return rp


# ---------------------------------------------------------------- parsing
class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.mailtos, self.text, self.title = [], [], [], ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0])
            else:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.text.append(data)


def parse_page(body):
    p = _PageParser()
    try:
        p.feed(body)
    except Exception:
        pass
    return p


def extract_emails(parser):
    found = []
    for raw in parser.mailtos:
        from urllib.parse import unquote
        found.extend(a.strip() for a in unquote(raw).split(","))
    found.extend(EMAIL_RE.findall(html.unescape(" ".join(parser.text))))
    clean = []
    for e in found:
        e = e.strip().strip(".")
        domain = e.rsplit("@", 1)[-1].lower() if "@" in e else ""
        if not domain or domain in PLACEHOLDER_DOMAINS or ASSET_SUFFIX.search(e):
            continue
        norm = validation.normalize(e)
        if norm and norm not in clean:
            clean.append(norm)
    return clean


def normalize_target(value):
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return None
    return host


def _related(email_domain, site_host):
    site = site_host[4:] if site_host.startswith("www.") else site_host
    return email_domain == site or email_domain.endswith("." + site) or site.endswith("." + email_domain)


# ---------------------------------------------------------------- crawl job
@jobs.handler("discover")
def _discover(ctx):
    targets = ctx.params["targets"][:MAX_DOMAINS_PER_JOB]
    max_pages = int(current_app.config["CRAWL_MAX_PAGES"])
    base_delay = float(current_app.config["CRAWL_DELAY"])
    totals = {"domains": len(targets), "pages": 0, "emails": 0, "blocked_by_robots": 0, "errors": 0}
    for i, target in enumerate(targets):
        ctx.check_cancel()
        ctx.progress(i, len(targets), f"Crawling {target['host']} ({i + 1} of {len(targets)})")
        host, company = target["host"], target.get("company", "")

        def log(url, outcome, status=None, n=0):
            execute("INSERT INTO discovery_pages (job_id, domain, url, outcome, http_status, emails_found, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (ctx.id, host, url[:500], outcome, status, n, now()))

        base = f"https://{host}/"
        try:
            robots = robots_for(base)
        except Blocked as exc:
            log(base, f"blocked: {exc}")
            totals["errors"] += 1
            continue
        delay = max(base_delay, float(robots.crawl_delay(current_app.config["CRAWL_USER_AGENT"]) or 0))
        delay = min(delay, 10.0)
        queue, visited = [base], set()
        while queue and len(visited) < max_pages:
            ctx.check_cancel()
            url = queue.pop(0).split("#")[0]
            if url in visited:
                continue
            visited.add(url)
            if not robots.can_fetch(current_app.config["CRAWL_USER_AGENT"], url):
                log(url, "skipped: disallowed by robots.txt")
                totals["blocked_by_robots"] += 1
                continue
            try:
                status, final, body, was_cached = cached_fetch(url)
            except Blocked as exc:
                log(url, f"blocked: {exc}")
                totals["errors"] += 1
                continue
            except Exception as exc:
                log(url, f"error: {type(exc).__name__}")
                totals["errors"] += 1
                continue
            final_host = (urlsplit(final).hostname or "").lower()
            if final_host != host and final_host != "www." + host and "www." + final_host != host:
                log(url, f"skipped: redirected off-site to {final_host}", status)
                continue
            if status >= 400 or not body:
                log(url, "no usable content", status)
                continue
            totals["pages"] += 1
            page = parse_page(body)
            if not company and page.title:
                company = page.title.strip().split("|")[0].split(" - ")[0].strip()[:120]
            emails = extract_emails(page)
            for email in emails:
                d = email.rsplit("@", 1)[1]
                existing = query("SELECT id FROM contacts WHERE workspace_id = ? AND email_normalized = ?",
                                 (ctx.workspace_id, email), one=True)
                execute("INSERT OR IGNORE INTO discovery_results (workspace_id, job_id, domain, company, email, "
                        "email_normalized, source_url, is_role, off_domain, status, contact_id, found_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ctx.workspace_id, ctx.id, host, company, email, email, final[:500],
                         int(validation.is_role(email.split("@")[0])), int(not _related(d, host)),
                         "existing" if existing else "pending", existing["id"] if existing else None, now()))
            totals["emails"] += len(emails)
            log(url, "fetched (cached)" if was_cached else "fetched", status, len(emails))
            if url == base or len(visited) == 1:
                for href in page.links:
                    link = urljoin(final, href).split("#")[0]
                    lp = urlsplit(link)
                    if (lp.scheme in ("http", "https") and (lp.hostname or "").lower() in (host, "www." + host, final_host)
                            and LINK_HINTS.search(lp.path) and not ASSET_SUFFIX.search(lp.path) and link not in queue
                            and link not in visited):
                        queue.append(link)
            if not was_cached and queue:
                time.sleep(delay if not current_app.config.get("TESTING") else 0)
    ctx.progress(len(targets), len(targets), "Finished")
    totals["unique_emails"] = scalar("SELECT COUNT(*) FROM discovery_results WHERE job_id = ?", (ctx.id,))
    return totals


# ---------------------------------------------------------------- views
@bp.route("/", methods=["GET", "POST"])
@require("discover")
def index():
    wid = ws_id()
    if request.method == "POST":
        raw = request.form.get("targets", "")
        file = request.files.get("file")
        if file and file.filename:
            raw += "\n" + file.read(1024 * 1024).decode("utf-8-sig", errors="replace")
        if request.form.get("confirm") != "on":
            flash("Please confirm that you will use the results lawfully and respect each website's terms.", "error")
            return redirect(url_for("discovery.index"))
        targets, seen = [], set()
        for line in raw.splitlines():
            parts = [p.strip() for p in re.split(r"[,;\t]", line) if p.strip()]
            if not parts:
                continue
            # A line may be "domain", "url" or "company, domain".
            host = next((normalize_target(p) for p in reversed(parts) if normalize_target(p)), None)
            if host and host not in seen and not host.endswith((".local", ".internal", ".localhost")):
                seen.add(host)
                company = parts[0] if len(parts) > 1 and not normalize_target(parts[0]) else ""
                targets.append({"host": host[4:] if host.startswith("www.") else host, "company": company[:120]})
        if not targets:
            flash("Enter at least one website or domain, one per line.", "error")
            return redirect(url_for("discovery.index"))
        if len(targets) > MAX_DOMAINS_PER_JOB:
            flash(f"One run can cover up to {MAX_DOMAINS_PER_JOB} domains. Only the first {MAX_DOMAINS_PER_JOB} were queued.", "info")
            targets = targets[:MAX_DOMAINS_PER_JOB]
        from .security import workspace
        used = scalar("SELECT COALESCE(SUM(total), 0) FROM jobs WHERE workspace_id = ? AND kind = 'discover' "
                      "AND created_at > datetime('now', '-1 day')", (wid,))
        if used + len(targets) > workspace()["discovery_daily_limit"]:
            flash(f"That would exceed your daily discovery limit of {workspace()['discovery_daily_limit']} domains "
                  f"({used} used in the last 24 hours).", "error")
            return redirect(url_for("discovery.index"))
        job = jobs.enqueue("discover", {"targets": targets}, workspace_id=wid, user_id=current_user()["id"],
                           total=len(targets), max_attempts=1)
        audit("discovery.started", detail=f"{len(targets)} domains")
        return redirect(url_for("discovery.run", job_id=job))
    runs = query("SELECT j.*, (SELECT COUNT(*) FROM discovery_results r WHERE r.job_id = j.id) AS found FROM jobs j "
                 "WHERE j.workspace_id = ? AND j.kind = 'discover' ORDER BY j.id DESC LIMIT 25", (wid,))
    return render_template("discovery/index.html", runs=runs, max_domains=MAX_DOMAINS_PER_JOB)


def _run(job_id):
    job = query("SELECT * FROM jobs WHERE id = ? AND workspace_id = ? AND kind = 'discover'", (job_id, ws_id()), one=True)
    if not job:
        abort(404)
    return job


@bp.route("/runs/<int:job_id>")
@require("discover")
def run(job_id):
    job = _run(job_id)
    status = request.args.get("status", "")
    domain = request.args.get("domain", "")
    where, args = ["job_id = ?"], [job_id]
    if status in ("pending", "added", "dismissed", "existing"):
        where.append("status = ?")
        args.append(status)
    if domain:
        where.append("domain = ?")
        args.append(domain)
    if request.args.get("hide_role") == "1":
        where.append("is_role = 0")
    results = query(f"SELECT * FROM discovery_results WHERE {' AND '.join(where)} ORDER BY domain, email", args)
    pages = query("SELECT * FROM discovery_pages WHERE job_id = ? ORDER BY id", (job_id,))
    domains = [r["domain"] for r in query("SELECT DISTINCT domain FROM discovery_results WHERE job_id = ? ORDER BY domain",
                                          (job_id,))]
    return render_template("discovery/run.html", job=job, results=results, pages=pages, domains=domains,
                           params=json.loads(job["params"]), result=json.loads(job["result"] or "{}"),
                           status=status, domain=domain, LAWFUL_BASES=LAWFUL_BASES, RECIPIENT_TYPES=RECIPIENT_TYPES)


@bp.route("/runs/<int:job_id>/review", methods=["POST"])
@require("discover")
def review(job_id):
    job = _run(job_id)
    wid = job["workspace_id"]
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    action = request.form.get("action")
    if not ids:
        flash("Select at least one result.", "error")
        return redirect(url_for("discovery.run", job_id=job_id))
    marks = ",".join("?" * len(ids))
    rows = query(f"SELECT * FROM discovery_results WHERE job_id = ? AND id IN ({marks}) AND status = 'pending'", (job_id, *ids))
    if action == "dismiss":
        execute(f"UPDATE discovery_results SET status = 'dismissed' WHERE job_id = ? AND id IN ({marks})", (job_id, *ids))
        flash(f"Dismissed {len(rows)} results.", "success")
    elif action == "add":
        purpose = request.form.get("purpose", "").strip()
        basis, rtype = request.form.get("lawful_basis"), request.form.get("recipient_type")
        if not purpose or basis not in LAWFUL_BASES or rtype not in RECIPIENT_TYPES:
            flash("Record the purpose, recipient type and lawful basis before adding contacts.", "error")
            return redirect(url_for("discovery.run", job_id=job_id))
        tag = request.form.get("tag", "").strip()
        added = 0
        for r in rows:
            cid, created = create_contact(wid, r["email"], {"company": r["company"], "website": f"https://{r['domain']}"},
                                          "discovery", source_url=r["source_url"],
                                          source_detail=f"Published on {r['source_url']} (found {r['found_at'][:10]})",
                                          purpose=purpose[:300], recipient_type=rtype, lawful_basis=basis)
            if created:
                added += 1
                if tag:
                    apply_tags(wid, cid, [tag])
                validation.store_on_contact(cid, validation.check(r["email"], wid, dns=True))
            execute("UPDATE discovery_results SET status = ?, contact_id = ? WHERE id = ?",
                    ("added" if created else "existing", cid, r["id"]))
        audit("discovery.added", detail=f"{added} contacts from run {job_id}")
        flash(f"Added {added} contacts. Each records the page it was published on.", "success")
    else:
        abort(400)
    return redirect(url_for("discovery.run", job_id=job_id))


@bp.route("/runs/<int:job_id>/export.csv")
@require("export_contacts")
def export(job_id):
    _run(job_id)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "domain", "company", "source_url", "found_at", "role_based", "different_domain", "status"])
    for r in query("SELECT * FROM discovery_results WHERE job_id = ? ORDER BY domain, email", (job_id,)):
        w.writerow([csv_safe(r["email"]), r["domain"], csv_safe(r["company"]), csv_safe(r["source_url"]), r["found_at"],
                    r["is_role"], r["off_domain"], r["status"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=discovery-{job_id}.csv"})
