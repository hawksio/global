"""Workspace settings (company, branding, sender, limits, compliance, retention) and Integrations (Resend, AI)."""
import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import mailer, validation
from .db import execute, query
from .security import audit, current_user, get_secret, require, secret_hint, set_secret, workspace

bp = Blueprint("settings", __name__, url_prefix="/settings")

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LIMITS = {  # field: (min, max)
    "daily_send_limit": (1, 100000), "hourly_send_limit": (1, 20000), "min_hours_between_emails": (0, 720),
    "ai_monthly_limit": (0, 100000), "discovery_daily_limit": (0, 5000), "retention_days": (30, 3650),
}


@bp.route("/", methods=["GET", "POST"])
@require("manage_settings")
def index():
    ws = workspace()
    if request.method == "POST":
        f = request.form
        section = f.get("section")
        if section == "details":
            values = {k: f.get(k, "").strip() for k in ("sender_name", "sender_email", "reply_to", "company_name",
                                                        "company_address")}
            for key in ("sender_email", "reply_to"):
                values[key] = values[key].lower()
                if values[key] and not validation.syntax_check(values[key])[0]:
                    flash(f"{values[key]} is not a valid email address.", "error")
                    return redirect(url_for("settings.index"))
            values["reply_to"] = values["reply_to"] or values["sender_email"]
            execute("UPDATE workspaces SET sender_name = ?, sender_email = ?, reply_to = ?, company_name = ?, "
                    "company_address = ? WHERE id = ?",
                    (values["sender_name"][:100], values["sender_email"], values["reply_to"], values["company_name"][:120],
                     values["company_address"][:300], ws["id"]))
        elif section == "company":
            color = f.get("brand_color", "#2563eb")
            logo = f.get("logo_url", "").strip()[:300]
            if not HEX.match(color):
                flash("Brand colour must be a hex colour like #2563eb.", "error")
                return redirect(url_for("settings.index"))
            if logo and not logo.startswith("https://"):
                flash("The logo address must start with https://", "error")
                return redirect(url_for("settings.index"))
            from .main import _tz_ok
            tz = f.get("timezone", "UTC")
            execute("UPDATE workspaces SET name = ?, company_name = ?, company_address = ?, website = ?, brand_color = ?, "
                    "logo_url = ?, timezone = ?, marketing_footer = ? WHERE id = ?",
                    (f.get("name", "").strip()[:120] or ws["name"], f.get("company_name", "").strip()[:120],
                     f.get("company_address", "").strip()[:300], f.get("website", "").strip()[:200], color, logo,
                     tz if _tz_ok(tz) else ws["timezone"], f.get("marketing_footer", "").strip()[:500], ws["id"]))
        elif section == "sender":
            email = f.get("sender_email", "").strip().lower()
            reply = f.get("reply_to", "").strip().lower()
            for value in (email, reply):
                if value and not validation.syntax_check(value)[0]:
                    flash(f"{value} is not a valid email address.", "error")
                    return redirect(url_for("settings.index"))
            execute("UPDATE workspaces SET sender_name = ?, sender_email = ?, reply_to = ? WHERE id = ?",
                    (f.get("sender_name", "").strip()[:100], email, reply, ws["id"]))
        elif section == "limits":
            values = {}
            for field, (lo, hi) in LIMITS.items():
                v = f.get(field, type=int)
                if v is None or not lo <= v <= hi:
                    flash(f"{field.replace('_', ' ').capitalize()} must be between {lo} and {hi}.", "error")
                    return redirect(url_for("settings.index"))
                values[field] = v
            if values["hourly_send_limit"] > values["daily_send_limit"]:
                flash("The hourly limit can't be higher than the daily limit.", "error")
                return redirect(url_for("settings.index"))
            bounce, complaint = f.get("bounce_pause_pct", type=float), f.get("complaint_pause_pct", type=float)
            if bounce is None or not 0.5 <= bounce <= 20 or complaint is None or not 0.01 <= complaint <= 5:
                flash("Safety thresholds: bounces 0.5–20%, complaints 0.01–5%.", "error")
                return redirect(url_for("settings.index"))
            execute("UPDATE workspaces SET " + ", ".join(f"{k} = ?" for k in values) + ", bounce_pause_pct = ?, "
                    "complaint_pause_pct = ?, four_eyes_approval = ?, email_login_codes = ? WHERE id = ?",
                    (*values.values(), bounce, complaint, int(f.get("four_eyes_approval") == "on"),
                     int(f.get("email_login_codes") == "on"), ws["id"]))
        elif section == "remove_samples":
            from .sandbox import remove
            n = remove(ws["id"])
            audit("settings.samples_removed", detail=str(n))
            flash(f"Practice samples removed: {n['contacts']} contacts, {n['templates']} saved emails, {n['campaigns']} "
                  "campaigns. Your own data was not touched.", "success")
            return redirect(url_for("settings.index"))
        else:
            abort(400)
        audit(f"settings.{section}")
        flash("Settings saved.", "success")
        return redirect(url_for("settings.index"))
    from .db import scalar
    samples = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND sandbox = 1", (ws["id"],)) +         scalar("SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND name LIKE '[Sample]%'", (ws["id"],))
    return render_template("settings/index.html", ws=ws, samples=samples)


@bp.route("/integrations", methods=["GET", "POST"])
@require("manage_integrations")
def integrations():
    ws = workspace()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "email_service":
            key = request.form.get("api_key", "").strip()
            if key and not re.fullmatch(r"re_[A-Za-z0-9_]{10,200}", key):
                flash("That doesn't look like a Resend API key (they start with re_).", "error")
                return redirect(url_for("settings.integrations"))
            if key:
                set_secret(ws["id"], "resend_api_key", key, current_user()["id"])
                audit("integration.resend_key", detail="set")
            has_key = bool(key or get_secret(ws["id"], "resend_api_key"))
            practice = request.form.get("sandbox_mode") == "on" or not (has_key or mailer.live_provider(ws))
            execute("UPDATE workspaces SET sandbox_mode = ? WHERE id = ?", (int(practice), ws["id"]))
            audit("integration.sandbox", detail="on" if practice else "off")
            if has_key:
                ok, message = mailer.ResendProvider(key or get_secret(ws["id"], "resend_api_key")).test_connection()
                flash(("Saved. " if ok else "Saved, but the connection test failed: ") + message +
                      (" Practice mode is on: nothing is really sent." if practice else " Live: emails really send."),
                      "success" if ok else "error")
            elif not practice:
                flash("Saved. Live: emails really send through SMTP.", "success")
            else:
                flash("Saved. Connect Resend or SMTP to send real email; until then everything is practice.", "info")
        elif action == "smtp":
            host = request.form.get("smtp_host", "").strip().lower()
            security = request.form.get("smtp_security", "starttls")
            port = request.form.get("smtp_port", "").strip()
            if host and not re.fullmatch(r"[a-z0-9.-]{3,253}", host):
                flash("Enter just the server name, e.g. mail.yourdomain.com (no https:// or slashes).", "error")
                return redirect(url_for("settings.integrations"))
            if security not in ("starttls", "ssl", "none") or (port and not port.isdigit()):
                abort(400)
            port = int(port) if port else (465 if security == "ssl" else 587)
            execute("UPDATE workspaces SET smtp_host = ?, smtp_port = ?, smtp_security = ?, smtp_username = ? WHERE id = ?",
                    (host, port, security, request.form.get("smtp_username", "").strip()[:200], ws["id"]))
            password = request.form.get("smtp_password", "")
            if password or not host:
                set_secret(ws["id"], "smtp_password", password if host else "", current_user()["id"])
            audit("integration.smtp", detail=f"{host}:{port} {security}" if host else "removed")
            if not host:
                flash("SMTP removed.", "success")
            else:
                ws = query("SELECT * FROM workspaces WHERE id = ?", (ws["id"],), one=True)
                ok, message = mailer.configured_provider(ws, "smtp").test_connection()
                flash(("Saved. " if ok else "Saved, but the test failed: ") + message, "success" if ok else "error")
        elif action == "default_service":
            choice = request.form.get("email_provider")
            if choice not in mailer.SERVICES:
                abort(400)
            if not mailer.configured_provider(ws, choice):
                flash(f"Set up {mailer.SERVICES[choice]} first, then make it the default.", "error")
                return redirect(url_for("settings.integrations"))
            execute("UPDATE workspaces SET email_provider = ? WHERE id = ?", (choice, ws["id"]))
            audit("integration.default_service", detail=choice)
            flash(f"{mailer.SERVICES[choice]} is now your default. If it stops working, set up the other one as a backup.",
                  "success")
        elif action == "resend_key":
            key = request.form.get("api_key", "").strip()
            if key and not re.fullmatch(r"re_[A-Za-z0-9_]{10,200}", key):
                flash("That doesn't look like a Resend API key (they start with re_).", "error")
                return redirect(url_for("settings.integrations"))
            set_secret(ws["id"], "resend_api_key", key, current_user()["id"])
            audit("integration.resend_key", detail="set" if key else "removed")
            flash("Resend API key " + ("saved (encrypted)." if key else "removed."), "success")
        elif action == "webhook_secret":
            secret = request.form.get("secret", "").strip()
            if secret and not secret.startswith("whsec_"):
                flash("Resend webhook signing secrets start with whsec_.", "error")
                return redirect(url_for("settings.integrations"))
            set_secret(ws["id"], "resend_webhook_secret", secret, current_user()["id"])
            audit("integration.webhook_secret", detail="set" if secret else "removed")
            flash("Webhook signing secret " + ("saved." if secret else "removed."), "success")
        elif action == "test":
            key = get_secret(ws["id"], "resend_api_key")
            ok, message = (mailer.ResendProvider(key).test_connection() if key else
                           (False, "No Resend API key saved yet."))
            flash(message, "success" if ok else "error")
        elif action == "sandbox":
            on = request.form.get("sandbox_mode") == "on"
            if not on and not mailer.live_provider(ws):
                flash("Connect Resend or SMTP before turning off practice mode.", "error")
                return redirect(url_for("settings.integrations"))
            execute("UPDATE workspaces SET sandbox_mode = ? WHERE id = ?", (int(on), ws["id"]))
            audit("integration.sandbox", detail="on" if on else "off")
            flash("Practice mode is " + ("on: nothing will be sent." if on else "off: approved, campaigns that aren't practice runs send "
                                                                              "real email."), "success")
        elif action == "update_disposable":
            if not current_user()["is_superadmin"]:
                abort(403)
            try:
                n = validation.update_disposable_list()
                flash(f"Disposable-domain list updated ({n:,} domains).", "success")
            except Exception as exc:
                flash(f"Could not update the list: {exc}", "error")
        else:
            abort(400)
        return redirect(url_for("settings.integrations"))
    from .ai import provider
    p = provider()
    active = mailer.live_provider(ws)
    return render_template("settings/integrations.html", ws=ws, resend_hint=secret_hint(ws["id"], "resend_api_key"),
                           smtp_hint=secret_hint(ws["id"], "smtp_password"), SERVICES=mailer.SERVICES,
                           active_service=active.name if active else None,
                           webhook_hint=secret_hint(ws["id"], "resend_webhook_secret"),
                           webhook_url=current_app.config["BASE_URL"] + url_for("webhooks.resend_workspace",
                                                                                public_id=ws["public_id"]),
                           ai_provider=p.name if p else "rules", ai_model=current_app.config["AI_MODEL"],
                           disposable=validation.disposable_meta())
