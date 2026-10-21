# Global Marketing

A web application for lawful B2B email outreach. It covers public business contact discovery, free local email
verification and list cleaning, contact management, an AI writing assistant, templates, campaigns with
follow-up sequences, Resend sending with webhooks, deliverability monitoring and analytics.

It runs on Python 3.10+ (developed on 3.12) and Flask 3, using SQLite. Long tasks run as background jobs
started by cron, which suits shared cPanel hosting.

> Status: feature-complete for a first release and covered by 87 automated tests. Read
> [PRODUCTION-READINESS.md](PRODUCTION-READINESS.md) before sending real email: some items need your accounts
> (Resend, DNS, hosting) and a legal review.

## Quick start (Windows PowerShell)

```powershell
cd "C:\Users\hawks\Desktop\claud new\global-marketing"
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

Open http://localhost:4322 and sign in as `admin@gm.test`. The password is in `instance\dev-admin.txt`; this
account is created only in development. The demo workspace contains fictional sample contacts (reserved
`example.*` domains), a sample template and a sample simulation campaign. **Sandbox mode** is on, so nothing is
sent: every message is written to `instance\outbox\` and labelled *simulated*.

In development the web process also runs the background worker. To run jobs by hand:
`.venv\Scripts\flask --app gm worker --once`.

## Features by section

| # | Section | What it does |
|---|---------|--------------|
| 1 | Overview | Real counts only, a getting-started checklist, running jobs, sender health |
| 2 | Contacts | Search, filter (company, domain, country, source, industry, tag, verification, status), sort, paginate, bulk tag/re-check/suppress/export/delete, contact timeline, formula-safe CSV export, suppression list |
| – | CSV import | Upload → column mapping with preview → background import with an accepted / review / duplicate / invalid / suppressed / rejected summary and downloadable report. Every contact records its source, purpose, recipient type and lawful basis |
| 3 | Discover Contacts | Crawls a company's own public site (home page plus contact, about, team and imprint pages). Obeys robots.txt and crawl delays, blocks private networks (SSRF), applies page, time and size caps and a daily quota, caches pages for a day. Collects only addresses actually published, and you review every result before it becomes a contact |
| 4 | Email Verification | Free local stages A–G: syntax, normalisation, DNS MX (null-MX and A-record fallback), disposable list (CC0), role addresses, own bounce, complaint and unsubscribe history. Shows evidence and timestamps. Bulk CSV validation with report and cleaned export. No SMTP probing |
| 5 | List Cleaning | Groups (invalid, risky, unknown, never checked, disposable, role, suppressed, no lawful basis) with re-check/tag/suppress/delete, look-alike detection (review only), cleaned export |
| 6 | AI Email Assistant | Drafts, subject lines, tone rewrites, proofreading, follow-ups, variations, campaign summaries and recommendations from real numbers, content checks. Uses Claude when configured and otherwise a free rule-based assistant. Monthly cap and cost tracking |
| 7 | Templates | Categories, versions with restore, validated personalisation fields with fallbacks, desktop and mobile preview in a sandboxed frame, preview as a real contact, content check, test send to team members only, approval |
| 8 | Campaigns | Segment → 10-point pre-launch checklist → side-effect-free dry-run report → test email → approval (optional four-eyes rule) → schedule → monitor → pause/resume/cancel. Simulation mode by default |
| 9 | Follow-up Sequences | Up to 5 follow-ups. They stop automatically on reply (manual mark or inbound email), unsubscribe, bounce, complaint or suppression |
| 10 | Transactional Email | Separate sender, key, templates and log for verification codes, sign-in codes, resets, security alerts, invitations and campaign alerts. Bodies are never stored |
| 11 | Deliverability Centre | Live DNS checks (SPF, DKIM `resend._domainkey`, DMARC, return-path MX, inbound MX) plus Resend domain status, a transparent 0–100 readiness score, bounce, complaint, unsubscribe, failure and retry rates, automatic pause above your thresholds |
| 12 | Analytics | Contacts, verification, discovery, campaigns, queued/accepted/delivered/bounced/complaints/unsubscribes/replies/opens (approximate)/clicks/failures/retries, AI usage and estimated cost. Filters by date, campaign, template and tag. CSV report. Unavailable metrics are labelled; simulated data is kept separate |
| 13 | Integrations | Resend API key (encrypted), connection test, sandbox switch, webhook URL and signing secret, AI status, disposable-list update |
| 14 | Settings | Company, postal address, branding colour and logo, sender identity, send limits, frequency cap, safety thresholds, AI and discovery quotas, retention period, emailed sign-in codes for everyone |
| 15 | Administration | Team, roles and invitations, ownership transfer, audit log. Superadmins also get the system console: workspaces and quotas, all users, jobs, health, system keys and backups |

Accounts: registration with a 6-digit email code, sign-in with lockout and rate limits, optional authenticator-app
(TOTP) or emailed codes, password reset links (single use, 30 minutes), server-side sessions with idle and absolute
expiry, sign out of other devices, account export (JSON) and deletion.

Roles: **owner**, **admin** (approve and launch campaigns, integrations, settings, team), **marketer** (create
everything, but can't approve or launch), **analyst** (read only). Every query is scoped to the user's workspace.

## Architecture

```
gm/
  __init__.py      app factory, security headers, log redaction, CLI (worker, migrate, backup, create-admin, …)
  config.py        environment / .env settings and production safety checks
  db.py            SQLite, versioned migrations with automatic pre-migration backups, transactions
  security.py      roles and permissions, server-side sessions, CSRF, rate limits, TOTP, Fernet secret storage, audit
  auth.py          registration, verification, sign-in, MFA, reset, invitations, account export and deletion
  transactional.py hashed single-use codes and reset tokens; transactional sending
  mailer.py        provider interface: ResendProvider (official SDK) and SandboxProvider; message log; bounded retries
  validation.py    stages A–G, DNS cache, disposable list
  contacts.py      contacts UI, filters, bulk actions, export, CSV import job
  suppression.py   suppression list; stops campaigns immediately
  verification.py  verification and list-cleaning UI and jobs
  discovery.py     robots-aware, SSRF-safe crawler and review flow
  render.py        personalisation, escaping, footer, unsubscribe tokens and List-Unsubscribe headers
  ai.py            AI provider abstraction (Claude / rules / mock) and content checker
  templates_lib.py templates, versions, previews, test sends
  campaigns.py     workflow, eligibility rules, checklist, sending engine, follow-ups
  webhooks.py      Svix-verified, idempotent Resend webhooks; bounces, complaints, inbound replies
  deliverability.py DNS authentication checks, readiness score, sender health, sandbox event simulation
  analytics.py     metrics and CSV report
  jobs.py          persistent job queue (cron-friendly), progress, cancellation, stale-job recovery, retention
  main.py / settings.py / admin.py / public.py   overview, onboarding, search, settings, administration, unsubscribe, /healthz
  templates/ static/ data/
tests/             87 unittest tests (no network: fake DNS, fake Resend, fake web)
deploy/            cPanel .cpanel.yml template, crontab, systemd worker
```

Message states are kept distinct: **queued** (in our queue) → **accepted** (Resend took it) → **delivered** (the
recipient's server accepted it; from webhook) or **bounced** / **failed** / **complained**. Simulated messages
are **simulated**. Inbox placement can't be measured and isn't claimed.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

The tests need no network and never send email. They cover registration and codes (hashing, expiry, attempts,
cooldown), sign-in, lockout, rate limits, TOTP, emailed codes, password reset, invitations, roles, workspace
isolation, account export and deletion, migrations and backups. They also cover validation stages, DNS failures,
disposable domains, CSV import outcomes and file validation, export formula safety, list cleaning, and
discovery (robots.txt, script and placeholder filtering, SSRF, quota, cancellation). The rest cover rendering and
escaping, unknown fields, template versions, test-send restrictions, the full campaign UI flow, eligibility,
suppression checked at send time, idempotent no-duplicate sending, bounded retries with the same key, permanent
failures, auth-error pause, hourly limits, frequency caps, send windows and time zones, cancellation, follow-ups and
reply stop, bounce-rate pause, webhook signatures (including stale timestamps), duplicate events, hard and soft
bounces, complaints, inbound replies, unsubscribe (GET confirms, POST and one-click act), AI fallback and limits,
content checks, Resend error mapping, CSRF, encrypted keys, log redaction, cookies and headers, session revocation
and idle expiry, private files and production config checks.

## GitHub (private repository)

This folder sits inside the existing `claud new` repository, whose remote is `hawksio/lmd` (the loan-management
project). Global Marketing is a different product, so it should get **its own private repository**. Create an
empty private repository on GitHub first (no README), then:

```powershell
cd "C:\Users\hawks\Desktop\claud new\global-marketing"
git init -b main
git add .
git status            # check: no .env, instance\, .venv\ or *.sqlite3 listed
git commit -m "Global Marketing: initial version"
git remote add origin https://github.com/<your-account>/<your-private-repo>.git
git push -u origin main
git switch -c develop
git push -u origin develop
```

Work on `develop` (or feature branches) and merge into `main` only when you're ready to deploy. cPanel deploys
from `main`. `.gitignore` excludes `.env`, `instance/` (database, uploads, keys, outbox, backups), `.venv/`, logs
and `dev-admin.txt`. Run `git status` before every commit anyway.

If you prefer to keep it inside the LMD repository instead, commit the folder there on a separate branch. Note that
the LMD repository currently has many uncommitted changes of its own on `main`.

## More

* [DEPLOYMENT.md](DEPLOYMENT.md): cPanel Git deployment, VPS, Resend DNS, cron, backups, rollback
* [PRODUCTION-READINESS.md](PRODUCTION-READINESS.md): checklist, known limitations, open-source licences, costs,
  compliance notes and remaining account setup
* [.env.example](.env.example): every setting
