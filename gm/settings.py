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
        if section == "company":
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
        else:
            abort(400)
        audit(f"settings.{section}")
        flash("Settings saved.", "success")
        return redirect(url_for("settings.index"))
    return render_template("settings/index.html", ws=ws)


@bp.route("/integrations", methods=["GET", "POST"])
@require("manage_integrations")
def integrations():
    ws = workspace()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "resend_key":
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
            if not on and not get_secret(ws["id"], "resend_api_key"):
                flash("Add a Resend API key before turning off practice mode.", "error")
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
    return render_template("settings/integrations.html", ws=ws, resend_hint=secret_hint(ws["id"], "resend_api_key"),
                           webhook_hint=secret_hint(ws["id"], "resend_webhook_secret"),
                           webhook_url=current_app.config["BASE_URL"] + url_for("webhooks.resend_workspace",
                                                                                public_id=ws["public_id"]),
                           ai_provider=p.name if p else "rules", ai_model=current_app.config["AI_MODEL"],
                           disposable=validation.disposable_meta())
