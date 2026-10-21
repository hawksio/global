# Production readiness

**Verdict: ready for a careful first deployment, not yet "production-ready".** The code-level requirements below are
implemented and tested. What remains depends on your accounts, hosting, DNS and a legal review, which can't be done
from here. Don't send real campaigns until the blockers in section 2 are cleared.

## 1. Checklist

| Requirement | State | Evidence |
|---|---|---|
| No known critical security defects | ✅ none known | Security review during build; `tests/test_security.py` |
| No secrets committed | ✅ | `.gitignore` excludes `.env`, `instance/`, keys, databases; API keys only stored Fernet-encrypted; test checks `.gitignore` |
| Debug off in production | ✅ | `debug=False` everywhere; production refuses to start without secret key, encryption key, HTTPS base URL and secure cookies (`config.startup_errors`, tested) |
| No development admin in production | ✅ | `admin@gm.test` only created when `GM_ENV` is not production; startup refuses `GM_DEV_ADMIN=1` in production |
| Migrations safe | ✅ | Versioned, transactional, automatic pre-migration backup, `flask --app gm migrate`; tested |
| Backups documented | ✅ documented / ⚠️ restore not yet tested on your host | `flask --app gm backup` + cron; restore steps in DEPLOYMENT.md. **Do one restore drill after deploying.** |
| Uploads stored securely | ✅ | Random names in `GM_INSTANCE_DIR/imports` (outside the web root), size, type, binary and row checks; deleted after use or 30 days; tested |
| API keys protected | ✅ | Encrypted at rest, never rendered back, redacted from logs and errors; tested |
| Permissions enforced | ✅ | Role checks on every route; every query scoped to the workspace; cross-workspace tests |
| Authentication and rate limits | ✅ | Lockout, IP and address rate limits, hashed single-use codes, TOTP, server-side sessions with idle and absolute expiry; tested |
| Unsubscribe and suppression | ✅ | Visible link, RFC 8058 one-click headers, immediate suppression, re-check right before every send, follow-ups stop; tested |
| Webhooks authenticated and idempotent | ✅ | Svix signature and timestamp via the official SDK; stored by event id; status never downgrades; tested |
| Accurate delivery states | ✅ | queued / accepted / delivered / bounced / complained / failed / simulated kept distinct; inbox placement never claimed |
| Errors logged without sensitive data | ✅ | Log redaction filter; transactional bodies never stored; generic error pages |
| Deployment and rollback documented | ✅ | DEPLOYMENT.md, `deploy/` templates |
| Privacy, retention, legal | ⚠️ partly | Retention setting with automatic clean-up, account export and deletion, lawful basis per contact. **Your privacy notice, terms and legal review are still needed.** |
| Hosting limitations identified | ✅ | DEPLOYMENT.md §1 and §4 |

## 2. Blockers before real sending (need you)

1. **Hosting**: confirm Python 3.10+, Setup Python App, cron and SSH on your cPanel plan (DEPLOYMENT.md §1). If any
   is missing, use a VPS.
2. **Resend account, verified sending domain and DNS** (SPF, DKIM, return-path MX, DMARC) and the two webhooks
   (DEPLOYMENT.md §3).
3. **Secrets**: generate and safely store `GM_SECRET_KEY` and `GM_ENCRYPTION_KEY`.
4. **Legal review** of your outreach for each target country. Typical points: PECR/UK GDPR and EU ePrivacy/GDPR
   (B2B rules differ by country; individuals' work addresses often count as personal data and may need consent);
   CAN-SPAM in the US (postal address, honest headers, opt-out within 10 days, which the app does immediately); CASL in
   Canada (usually needs consent). The app records lawful basis and blocks consumers without consent, but it can't
   decide what is lawful for you.
5. **Publish a privacy notice** that explains contact discovery (source pages are recorded on every contact) and how
   people can object. Handle objections by adding them to the suppression list.
6. **Restore drill**: restore one backup on the server and start the app against it.
7. **Second factor for owners and admins**: turn it on before inviting your team.

## 3. Known limitations (honest list)

* **Verification is local.** It checks syntax, DNS, disposable domains, roles and history. It can't confirm a
  mailbox exists; no free method can do so reliably without SMTP probing, which is deliberately not done. Paid
  verification providers can be added as an adapter (`validation.py` stage C/G), but none is built in.
* **Discovery** covers static HTML only. Pages rendered by JavaScript, PDFs and images are not read. The SSRF check
  resolves the host before connecting, but a hostile DNS server could still rebind between the check and the
  connection (accepted for this threat model; run the app on a host without sensitive internal services).
  robots.txt is honoured, but **site terms of service can't be read automatically**, so you must respect them.
* **Reply detection** works through a manual "Replied" button, or automatically when Resend inbound receiving
  (`email.received`) is set up for your reply address. Without inbound, automatic reply stopping isn't available.
* **Opens and clicks** depend on Resend tracking being enabled for the domain and are approximate.
* **SQLite** suits a single server; there's no built-in PostgreSQL support.
* **Concurrency**: two copies of the same webhook arriving in the same instant could both add a timeline entry
  (statuses and suppressions stay correct). Resend's idempotency keys protect sends for 24 hours; the message log's
  unique key protects them permanently.
* **Cron granularity**: with 5-minute cron, sending happens in 5-minute bursts within the per-minute rate.
* **Accessibility**: semantic HTML, labels, focus styles, keyboard-usable dialogs and a skip link are in place, but no
  formal WCAG audit has been done.
* **No paid verification adapter, no PostgreSQL backend, no SSO** were built.
* The previous LMD application in the parent folder is untouched and still has its own uncommitted changes.

## 4. Open-source components and licences (verified from installed package metadata, 2026-10-21)

| Component | Version | Licence | Use |
|---|---|---|---|
| Flask / Werkzeug / Jinja2 / itsdangerous | 3.1.3 / 3.1.9 / 3.1.6 / 2.2.0 | BSD-3-Clause | Web framework, templates, signed tokens |
| email-validator | 2.3.0 | Unlicense | Syntax and normalisation |
| dnspython | 2.9.0 | ISC | MX, TXT (SPF, DKIM, DMARC) lookups |
| resend (official SDK) | 2.50.0 | MIT | Sending, idempotency keys, webhook verification, domains |
| requests | 2.34.2 | Apache-2.0 | Crawler HTTP |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Fernet encryption of stored secrets |
| segno | 1.6.6 | BSD-3-Clause | Authenticator QR code |
| tzdata | 2026.5 | Apache-2.0 | Time zones |
| anthropic (optional) | 1.17.0 | MIT | Claude AI assistant |
| gunicorn (VPS only) | 23.x | MIT | Production WSGI server |
| disposable-email-domains list | commit b63df8b (2026-10-19) | CC0-1.0 | Bundled in `gm/data/` |

All are permissive and allow commercial use. Repositories evaluated but **not used**:

| Repository | Licence | Why not |
|---|---|---|
| scrapy/scrapy | BSD-3-Clause, very active | Needs a long-running Twisted reactor, which doesn't fit cron-driven shared hosting. A small requests-based crawler with the same politeness rules was built instead |
| adbar/trafilatura | Apache-2.0, active | Removes boilerplate, and business contact addresses are often in exactly that boilerplate (footers); adds lxml. The stdlib HTML parser is used |
| koprjaa/email-verifier | MIT, 1 star, Rust | Works by SMTP RCPT probing, which the brief rules out |
| OpenOSINT/OpenOSINT | MIT | A people-focused OSINT agent; out of scope and a privacy risk for this product |
| blankarrayy/n8n-nodes-emailscraper | MIT, 0 stars, TypeScript | An n8n node with no robots.txt handling. Its ideas (same-domain crawl, page cap, placeholder filtering) informed the design; no code was copied |

## 5. Costs

| Item | Free | Paid when |
|---|---|---|
| Discovery, parsing, de-duplication, syntax, DNS/MX, disposable and role checks, suppression, analytics | ✅ always, runs on your server | – |
| Rule-based AI assistant | ✅ | – |
| Resend | Free tier with daily/monthly caps | Higher volume, dedicated IPs, more domains. See resend.com/pricing (changes over time) |
| Claude AI (optional) | – | Per token. Default `claude-opus-5-5`: $4 / $20 per million input/output tokens. A typical draft is roughly 1–2k tokens, about $0.01–0.04. Capped per workspace; usage and estimate in Analytics |
| Hosting | – | Your cPanel plan or a small VPS (typically a few dollars to ~$20/month) |
| Specialist mailbox verification | – | Not included; usually priced per thousand addresses |

To keep everything free: leave the AI key empty (or `GM_AI_PROVIDER=rules`), stay on Resend's free tier, and use only
the local verification.

## 6. Environment variables

See `.env.example`. **Required in production:** `GM_ENV=production`, `GM_SECRET_KEY`, `GM_ENCRYPTION_KEY`,
`GM_BASE_URL` (https), `GM_SECURE_COOKIES=1`, `GM_INSTANCE_DIR`, `GM_DEV_ADMIN=0`. **Needed for real email:**
`GM_RESEND_API_KEY`, `GM_TRANSACTIONAL_FROM`, `GM_RESEND_WEBHOOK_SECRET`, plus the per-workspace key and webhook secret
in Integrations. **Optional:** `GM_ANTHROPIC_API_KEY`, `GM_AI_MODEL`, crawler and limit settings.
