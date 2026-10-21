# Deployment

Global Marketing is a WSGI application (`wsgi.py` / `passenger_wsgi.py`) plus a **background worker** that must be
run by cron (shared hosting) or as a service (VPS). Don't use `run.py` (Flask's development server) in production.

## 1. Check the hosting plan first

Confirm each item in your hosting control panel or with your host before you start:

| Requirement | Why | How to check (cPanel) |
|---|---|---|
| Python 3.10 or newer | `anthropic` 1.x needs it; developed on 3.12 | *Setup Python App*: pick the newest version offered |
| "Setup Python App" (Passenger) | Runs the WSGI app | Present under *Software* |
| Git Version Control | Pull from GitHub and deploy | Under *Files* |
| SSH access | Deploy key for a **private** GitHub repo; pip; one-off commands | *SSH Access* / *Terminal* |
| Cron jobs at 1-minute (or 5-minute) intervals | The worker sends campaigns, runs imports, verification and discovery | *Cron Jobs* |
| Outbound HTTPS | Resend API, AI API, contact discovery | Ask the host if unsure |
| Outbound DNS lookups | MX, SPF, DKIM, DMARC checks | Usually allowed |
| Persistent disk outside `public_html` | Database, uploads, keys, backups | Your home directory |

**If the plan has no Python app support or no cron**, it can't run this application properly. Use a small VPS or a
Python PaaS instead (section 4). Some shared hosts kill long-running processes or limit CPU per process. The worker is
built for this: it does at most `--budget` seconds of work per cron run and resumes next time.

**SQLite on shared hosting** is fine for one server and moderate volume (tens of thousands of contacts, a few
thousand emails a day). Keep `GM_INSTANCE_DIR` on local disk, not network storage. For multi-server or much higher
volume, migrate to PostgreSQL. That needs code changes in `gm/db.py` and isn't included.

## 2. cPanel step by step

Replace `CPANELUSER` with your account name. Suggested layout (all outside `public_html`):

```
/home/CPANELUSER/repositories/global-marketing   ← Git clone (cPanel Git Version Control)
/home/CPANELUSER/global-marketing                ← deployed app (Python App root)
/home/CPANELUSER/gm-data                         ← GM_INSTANCE_DIR: database, uploads, keys, backups
/home/CPANELUSER/logs                            ← worker and backup logs
```

1. **Generate secrets** on your PC: `.venv\Scripts\python -m gm.keys`. Store both values in your password manager.
   If you lose `GM_ENCRYPTION_KEY`, the stored API keys and MFA secrets can't be decrypted and must be entered again.
2. **Private repo access**: in cPanel *SSH Access*, generate a key pair. Add the public key to the GitHub repo as a
   read-only *Deploy key* (Settings → Deploy keys). Clone URL: `git@github.com:<account>/<repo>.git`.
3. **Git Version Control → Create**: clone into `/home/CPANELUSER/repositories/global-marketing`, branch `main`.
4. **Setup Python App → Create**: Python 3.12 (or the newest 3.10+), *Application root* `global-marketing`,
   *Application URL* your domain or subdomain, *Startup file* `passenger_wsgi.py`, *Entry point* `application`.
   Add the environment variables from `.env.example` here. At minimum: `GM_ENV=production`, `GM_SECRET_KEY`,
   `GM_ENCRYPTION_KEY`, `GM_BASE_URL=https://your-domain`, `GM_SECURE_COOKIES=1`, `GM_TRUST_PROXY=1`,
   `GM_INSTANCE_DIR=/home/CPANELUSER/gm-data`, `GM_DEV_ADMIN=0`. You can put them in
   `/home/CPANELUSER/global-marketing/.env` instead; it is not web-accessible because the app root is outside
   `public_html`, and `chmod 600` it.
5. **Deployment file**: copy `deploy/cpanel.yml.example` to `.cpanel.yml` at the repository root, replace
   `CPANELUSER` and the Python version (check the virtualenv path *Setup Python App* shows), commit and push.
   **Only commit it once you've confirmed the paths on your account.**
6. In *Git Version Control → Manage → Pull or Deploy*: **Update from Remote**, then **Deploy HEAD Commit**. The
   tasks copy the code, `pip install` the requirements, run `flask --app gm migrate` and restart Passenger.
   Pulling alone does **not** install dependencies or migrate.
7. **Create the first administrator** (Terminal / SSH, inside the virtualenv the app page shows):
   ```bash
   source /home/CPANELUSER/virtualenv/global-marketing/3.12/bin/activate
   cd /home/CPANELUSER/global-marketing
   flask --app gm create-admin --email you@yourcompany.com --name "Your Name" --workspace "Your Company"
   ```
   It prints a one-time link (valid 24 hours) to set the password. Then turn on an authenticator app under
   *Security & privacy*. Set `GM_ALLOW_SIGNUP=0` if only invited people should get accounts.
8. **Cron Jobs**: add the lines from `deploy/crontab.example`: the worker every minute, the daily backup and the
   optional weekly disposable-list refresh.
9. **HTTPS**: enable AutoSSL / Let's Encrypt for the domain. Check `https://your-domain/healthz` returns
   `"status": "ok"` and that `"worker"` isn't "behind" a few minutes after the cron job starts.

### Updating and rolling back

* Update: push to `main`, then *Update from Remote* → *Deploy HEAD Commit*. Migrations back up the database
  automatically (`gm-data/backups/gm-pre-migration-N-*.sqlite3`) before changing it.
* Roll back code: in Git Version Control check out the previous commit (or `git revert` and push), then deploy.
* Roll back data: stop the cron job, copy the matching backup over `gm-data/gm.sqlite3`, restore the code version
  that matches it, start the cron job again. Never edit a migration that has shipped; add a new one.

### Backups

`flask --app gm backup` writes a consistent online copy (SQLite backup API) to `gm-data/backups/`. Copy backups
**off the server** regularly (cPanel *Backup* or download). Test a restore at least once: copy a backup to a test
instance directory and start the app with `GM_INSTANCE_DIR` pointing at it. Back up `GM_ENCRYPTION_KEY` and
`GM_SECRET_KEY` separately from the database.

## 3. Resend setup and DNS

1. Create a Resend account and add a sending domain. A subdomain keeps reputations apart, for example
   `news.yourcompany.com` for marketing and `accounts.yourcompany.com` for transactional mail.
2. Add the records Resend shows in your DNS (cPanel *Zone Editor* if your DNS is at the host): the SPF TXT and MX on
   `send.<domain>`, the DKIM TXT at `resend._domainkey.<domain>`, and a DMARC TXT at `_dmarc.<domain>` (start with
   `v=DMARC1; p=none; rua=mailto:dmarc@yourcompany.com`). Click *Verify* in Resend.
3. Create an API key with **sending access** for that domain. Paste it in *Integrations* (per workspace,
   marketing) and in `GM_RESEND_API_KEY` (system, transactional). Use **Test connection**.
4. Webhooks: in Resend add `https://your-domain/webhooks/resend/<workspace id shown in Integrations>` (marketing) and
   `https://your-domain/webhooks/resend/system` (transactional). Select the `email.*` events and paste each signing
   secret (`whsec_…`) in the matching place.
5. In the app: run the Deliverability Centre checks, send test emails, then turn off sandbox mode. Start with a
   small, recently verified segment and grow volume gradually.

Docs: https://resend.com/docs · domains: https://resend.com/docs/dashboard/domains/introduction · webhooks:
https://resend.com/docs/webhooks/introduction

## 4. VPS or PaaS (when shared hosting can't run it)

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill it in; GM_ENV=production
.venv/bin/flask --app gm migrate
.venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 wsgi:app      # behind nginx with HTTPS; GM_TRUST_PROXY=1
```

Run the worker with `deploy/global-marketing-worker.service` (systemd) or the same cron line as above. Two gunicorn
workers and one background worker are plenty for SQLite.

## 5. AI provider

Optional. Set `GM_ANTHROPIC_API_KEY` (or save it under *System administration → keys*). The default model is
`claude-opus-5-5` with low effort and server-side refusal fallbacks enabled; change it with `GM_AI_MODEL`. Usage is
capped per workspace (*Settings*), and token use and estimated cost show in Analytics. With no key, the free
rule-based assistant is used. `GM_AI_PROVIDER=rules` forces it even when a key exists.
