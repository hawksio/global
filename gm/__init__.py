"""Global Marketing: contact discovery, email verification, AI-assisted writing and campaign automation.

Run locally:    .venv\\Scripts\\python run.py                 (http://localhost:4321)
Background:     .venv\\Scripts\\flask --app gm worker --once  (cron, every minute, in production)
Create admin:   .venv\\Scripts\\flask --app gm create-admin --email you@example.com --name "You" --workspace "Company"
"""
import logging
import os
import secrets
from datetime import timedelta

import click
from flask import Flask, g, render_template, request
from werkzeug.security import generate_password_hash

from . import config

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "font-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'self'")


class _RedactSecrets(logging.Filter):
    """Belt and braces: never let API keys or signing secrets reach the logs."""
    import re as _re
    PATTERN = _re.compile(r"(re_[A-Za-z0-9_]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|whsec_[A-Za-z0-9+/=]{8,})")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self.PATTERN.search(msg):
            record.msg, record.args = self.PATTERN.sub("[redacted]", msg), ()
        return True


def create_app(test_config=None):
    config.load_dotenv()
    problems = config.startup_errors()
    if problems:
        raise RuntimeError("Unsafe production configuration:\n- " + "\n- ".join(problems))
    instance = config.instance_dir()
    for sub in ("imports", "reports", "outbox", "backups"):
        os.makedirs(os.path.join(instance, sub), exist_ok=True)
    app = Flask(__name__, instance_path=instance, static_folder="static", template_folder="templates")
    production = config.is_production()
    max_mb = int(config.env("GM_MAX_UPLOAD_MB"))
    app.config.update(
        ENV_NAME=config.env("GM_ENV"), PRODUCTION=production,
        SECRET_KEY=config.secret_key(instance), ENCRYPTION_KEY=config.encryption_key(instance),
        DATABASE=os.path.join(instance, "gm.sqlite3"), IMPORT_DIR=os.path.join(instance, "imports"),
        REPORT_DIR=os.path.join(instance, "reports"), OUTBOX_DIR=os.path.join(instance, "outbox"),
        MAX_UPLOAD_BYTES=max_mb * 1024 * 1024, MAX_CONTENT_LENGTH=(max_mb + 1) * 1024 * 1024,
        MAX_IMPORT_ROWS=int(config.env("GM_MAX_IMPORT_ROWS")),
        SESSION_COOKIE_NAME="__Host-gm" if production else "gm_session", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=config.flag("GM_SECURE_COOKIES"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(config.env("GM_SESSION_HOURS"))),
        SESSION_HOURS=int(config.env("GM_SESSION_HOURS")), SESSION_IDLE_MINUTES=int(config.env("GM_SESSION_IDLE_MINUTES")),
        BASE_URL=config.env("GM_BASE_URL").rstrip("/"), ALLOW_SIGNUP=config.flag("GM_ALLOW_SIGNUP"),
        RESEND_API_KEY=config.env("GM_RESEND_API_KEY"), RESEND_WEBHOOK_SECRET=config.env("GM_RESEND_WEBHOOK_SECRET"),
        TRANSACTIONAL_FROM=config.env("GM_TRANSACTIONAL_FROM"), TRANSACTIONAL_REPLY_TO=config.env("GM_TRANSACTIONAL_REPLY_TO"),
        AI_PROVIDER=config.env("GM_AI_PROVIDER"), ANTHROPIC_API_KEY=config.env("GM_ANTHROPIC_API_KEY"),
        AI_MODEL=config.env("GM_AI_MODEL"), AI_EFFORT=config.env("GM_AI_EFFORT"),
        CRAWL_USER_AGENT=config.env("GM_CRAWL_USER_AGENT"), CRAWL_MAX_PAGES=config.env("GM_CRAWL_MAX_PAGES"),
        CRAWL_DELAY=config.env("GM_CRAWL_DELAY_SECONDS"), CRAWL_TIMEOUT=config.env("GM_CRAWL_TIMEOUT_SECONDS"),
        DNS_TIMEOUT=config.env("GM_DNS_TIMEOUT_SECONDS"),
    )
    if test_config:
        app.config.update(test_config)
    proxies = int(config.env("GM_TRUST_PROXY") or 0)
    if proxies:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactSecrets())
    app.logger.addFilter(_RedactSecrets())

    from .db import init_db
    init_db(app)

    from . import (admin, ai, analytics, auth, campaigns, contacts, deliverability, discovery, main, public, settings,
                   suppression, templates_lib, verification, webhooks)
    for bp in (auth.bp, main.bp, contacts.bp, suppression.bp, verification.bp, discovery.bp, ai.bp, templates_lib.bp,
               campaigns.bp, deliverability.bp, analytics.bp, settings.bp, admin.bp, webhooks.bp, public.bp):
        app.register_blueprint(bp)

    from .security import can, check_csrf, csrf_token, current_user, workspace

    @app.before_request
    def _fresh_request_state():
        # The signed-in user is cached on g; make sure nothing survives from an earlier request in a shared context.
        for key in ("user", "user_session", "workspace"):
            g.pop(key, None)

    app.before_request(check_csrf)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if current_user() is not None and resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from .contacts import LAWFUL_BASES, RECIPIENT_TYPES, SOURCES, STATUSES, VERIFICATION
    from .nav import navigation
    from .security import ROLES
    app.jinja_env.globals.update(csrf_token=csrf_token, current_user=current_user, can=can, workspace=workspace, navigation=navigation,
                                 VERIFICATION=VERIFICATION, STATUSES=STATUSES, LAWFUL_BASES=LAWFUL_BASES,
                                 RECIPIENT_TYPES=RECIPIENT_TYPES, SOURCES=SOURCES, ROLES=ROLES,
                                 production=production, env_name=app.config["ENV_NAME"])

    @app.template_filter("dt")
    def _dt(value):
        return (value or "")[:16]

    @app.template_filter("fromjson")
    def _fromjson(value):
        import json
        try:
            return json.loads(value or "{}")
        except ValueError:
            return {}

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def _error(e):
        messages = {400: getattr(e, "description", None) or "That request wasn't valid.",
                    403: "You don't have permission to do that. Ask your workspace owner if you need access.",
                    404: "We couldn't find that page.", 409: "That action isn't possible in the item's current state.",
                    413: "The upload is too large."}
        code = getattr(e, "code", 500)
        if request.path.startswith("/webhooks/"):
            return {"error": messages.get(code, "error")}, code
        return render_template("error.html", code=code, message=messages.get(code, str(e))), code

    @app.errorhandler(500)
    def _server_error(_e):
        return render_template("error.html", code=500, message="Something went wrong on our side. It has been logged."), 500

    _register_cli(app)
    if not production and config.flag("GM_DEV_ADMIN") and not app.config.get("TESTING"):
        with app.app_context():
            _ensure_dev_admin(app)
    inline = config.env("GM_INLINE_WORKER")
    if (inline == "1" or (inline == "" and not production)) and not app.config.get("TESTING") and test_config is None:
        app.config["START_INLINE_WORKER"] = True
    return app


def _ensure_dev_admin(app):
    from .auth import create_workspace
    from .db import query
    from .sandbox import seed
    if query("SELECT 1 FROM users LIMIT 1", one=True):
        return  # only ever on a brand-new, empty database
    password = secrets.token_urlsafe(12)
    ws_id, uid = create_workspace("Global Marketing Demo", "admin@gm.test", "Demo Administrator",
                                  generate_password_hash(password), verified=True, superadmin=True)
    from .db import execute
    execute("UPDATE workspaces SET company_address = '[Sample] 1 Example Street, Exampletown', "
            "sender_email = 'sender@example.com' WHERE id = ?", (ws_id,))
    seed(ws_id, uid)
    path = os.path.join(app.instance_path, "dev-admin.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"DEVELOPMENT ONLY\nemail: admin@gm.test\npassword: {password}\n")
    app.logger.info("Development admin created: admin@gm.test (password in %s)", path)


def _register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.option("--name", required=True)
    @click.option("--workspace", "workspace_name", default="", help="Create a workspace owned by this person")
    @click.option("--superadmin/--no-superadmin", default=True)
    def create_admin(email, name, workspace_name, superadmin):
        """Create an administrator. Prints a one-time password-setting link (nothing is stored in plain text)."""
        from .auth import create_workspace
        from .db import execute, now, query
        from .transactional import new_reset_token
        if query("SELECT 1 FROM users WHERE email = ?", (email.lower(),), one=True):
            raise click.ClickException("That email already has an account.")
        placeholder = generate_password_hash(secrets.token_urlsafe(32))
        if workspace_name:
            _, uid = create_workspace(workspace_name, email, name, placeholder, verified=True, superadmin=superadmin)
        else:
            uid = execute("INSERT INTO users (workspace_id, email, password_hash, full_name, role, is_superadmin, "
                          "email_verified_at, created_at) VALUES (NULL, ?, ?, ?, 'admin', ?, ?, ?)",
                          (email.lower(), placeholder, name, int(superadmin), now(), now()))
        token = new_reset_token(uid, minutes=24 * 60)
        with app.test_request_context():
            from flask import url_for
            link = app.config["BASE_URL"] + url_for("auth.reset", uid=uid, token=token, invite=1)
        click.echo(f"Created {email}. Set the password within 24 hours using this one-time link:\n{link}")

    @app.cli.command("worker")
    @click.option("--once", is_flag=True, help="Process due jobs for up to --budget seconds, then exit (for cron)")
    @click.option("--budget", default=50, help="Seconds to work in --once mode")
    def worker(once, budget):
        """Run background jobs (imports, verification, discovery, sending, retention)."""
        import time
        from .jobs import run_pending
        if once:
            n = run_pending(time_budget=budget)
            click.echo(f"Processed {n} job(s).")
            return
        click.echo("Worker running. Press Ctrl+C to stop.")
        while True:
            run_pending(time_budget=55)
            time.sleep(5)

    @app.cli.command("migrate")
    def migrate_cmd():
        """Apply pending database migrations (a backup is taken first). Safe to run repeatedly."""
        from .db import MIGRATIONS, applied_versions, connect, migrate
        done = migrate(app.config["DATABASE"], os.path.join(app.instance_path, "backups"))
        conn = connect(app.config["DATABASE"])
        current = max(applied_versions(conn) or {0})
        conn.close()
        click.echo(f"Applied: {done or 'nothing pending'}. Schema version {current} of {MIGRATIONS[-1][0]}.")

    @app.cli.command("backup")
    def backup():
        """Write a consistent copy of the database to instance/backups/."""
        from .db import backup_database
        click.echo(backup_database(app.config["DATABASE"], os.path.join(app.instance_path, "backups"), "manual"))

    @app.cli.command("seed-sandbox")
    @click.option("--workspace-id", type=int, required=True)
    def seed_sandbox(workspace_id):
        """Add clearly labelled fictional sample data to a workspace."""
        from .sandbox import seed
        click.echo("Sample data added." if seed(workspace_id) else "This workspace already has sample data.")

    @app.cli.command("remove-sample-data")
    @click.option("--workspace-id", type=int, required=True)
    def remove_sample_data(workspace_id):
        """Delete the practice samples (fictional contacts, sample email and campaign) from a workspace."""
        from .sandbox import remove
        click.echo(f"Removed: {remove(workspace_id)}")

    @app.cli.command("update-disposable")
    def update_disposable():
        """Download the latest disposable-domain list (CC0) into instance/."""
        from .validation import update_disposable_list
        click.echo(f"{update_disposable_list():,} domains.")
