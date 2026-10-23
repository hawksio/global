"""Email templates: categories, versions, previews (desktop and mobile), variable testing, test sends and approval.

Every save creates a new version, so earlier wording can be viewed and restored. AI-generated templates start
unapproved; editing an approved template clears the approval.
"""
import html
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import mailer, render
from .ai import content_check
from .db import execute, now, query, transaction
from .security import audit, can, current_user, require, workspace, ws_id

bp = Blueprint("templates", __name__, url_prefix="/templates")

CATEGORIES = ["General", "First contact", "Follow-up", "Newsletter", "Announcement", "Event", "AI drafts"]


def text_to_html(text):
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "\n".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def create_template(workspace_id, name, category, subject, html_body, text_body="", preheader="", ai_generated=False):
    user = current_user()
    with transaction():
        tid = execute("INSERT INTO templates (workspace_id, name, category, subject, preheader, html_body, text_body, "
                      "ai_generated, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (workspace_id, name, category, subject, preheader, render.strip_scripts(html_body), text_body,
                       int(ai_generated), user["id"] if user else None, now(), now()))
        execute("INSERT INTO template_versions (template_id, version, subject, preheader, html_body, text_body, created_by, "
                "created_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?)", (tid, subject, preheader, render.strip_scripts(html_body),
                                                                text_body, user["id"] if user else None, now()))
    return tid


def get_template(template_id):
    t = query("SELECT * FROM templates WHERE id = ? AND workspace_id = ?", (template_id, ws_id()), one=True)
    if not t:
        abort(404)
    return t


@bp.route("/")
@require("view_templates")
def index():
    wid = ws_id()
    q, cat = request.args.get("q", "").strip(), request.args.get("category", "")
    where, args = ["workspace_id = ?", "archived = ?"], [wid, int(request.args.get("archived") == "1")]
    if q:
        where.append("(lower(name) LIKE ? OR lower(subject) LIKE ?)")
        args += [f"%{q.lower()}%"] * 2
    if cat:
        where.append("category = ?")
        args.append(cat)
    rows = query(f"SELECT * FROM templates WHERE {' AND '.join(where)} ORDER BY updated_at DESC", args)
    return render_template("templates/list.html", templates=rows, q=q, category=cat, CATEGORIES=CATEGORIES)


def _read_form():
    f = request.form
    data = {"name": f.get("name", "").strip()[:120], "category": f.get("category", "General")[:40],
            "subject": f.get("subject", "").strip()[:300], "preheader": f.get("preheader", "").strip()[:200],
            "html_body": render.strip_scripts(f.get("html_body", ""))[:200000], "text_body": f.get("text_body", "")[:100000]}
    if not data["html_body"].strip() and data["text_body"].strip():
        data["html_body"] = text_to_html(data["text_body"])
    errors = []
    if not data["name"]:
        errors.append("Give the template a name.")
    if not data["subject"]:
        errors.append("Enter a subject line.")
    if not data["html_body"].strip():
        errors.append("Write the email body.")
    bad = render.unknown_fields(data["subject"], data["preheader"], data["html_body"], data["text_body"])
    if bad:
        errors.append("Unknown personalisation fields: " + ", ".join("{{" + b + "}}" for b in bad) +
                      ". Use only the fields listed beside the editor.")
    return data, errors


@bp.route("/new", methods=["GET", "POST"])
@require("edit_templates")
def new():
    if request.method == "POST":
        data, errors = _read_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("templates/edit.html", t=None, form=data, CATEGORIES=CATEGORIES, FIELDS=render.FIELDS), 400
        tid = create_template(ws_id(), data["name"], data["category"], data["subject"], data["html_body"], data["text_body"],
                              data["preheader"])
        audit("template.created", target=tid)
        flash("Template saved.", "success")
        return redirect(url_for("templates.edit", template_id=tid))
    return render_template("templates/edit.html", t=None, form={"category": request.args.get("category", "General")},
                           CATEGORIES=CATEGORIES, FIELDS=render.FIELDS)


@bp.route("/<int:template_id>", methods=["GET", "POST"])
@require("view_templates")
def edit(template_id):
    t = get_template(template_id)
    if request.method == "POST":
        if not can("edit_templates"):
            abort(403)
        data, errors = _read_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("templates/edit.html", t=t, form=data, CATEGORIES=CATEGORIES, FIELDS=render.FIELDS,
                                   versions=_versions(t["id"])), 400
        changed = any(data[k] != t[k] for k in ("subject", "preheader", "html_body", "text_body"))
        with transaction():
            version = t["version"] + 1 if changed else t["version"]
            execute("UPDATE templates SET name = ?, category = ?, subject = ?, preheader = ?, html_body = ?, text_body = ?, "
                    "version = ?, approved_at = CASE WHEN ? THEN NULL ELSE approved_at END, "
                    "approved_by = CASE WHEN ? THEN NULL ELSE approved_by END, updated_at = ? WHERE id = ?",
                    (data["name"], data["category"], data["subject"], data["preheader"], data["html_body"], data["text_body"],
                     version, int(changed), int(changed), now(), t["id"]))
            if changed:
                execute("INSERT INTO template_versions (template_id, version, subject, preheader, html_body, text_body, "
                        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (t["id"], version, data["subject"], data["preheader"], data["html_body"], data["text_body"],
                         current_user()["id"], now()))
        audit("template.updated", target=t["id"], detail=f"version {version}")
        flash(f"Saved (version {version})." + (" Approval was cleared because the content changed." if changed and t["approved_at"]
                                               else ""), "success")
        return redirect(url_for("templates.edit", template_id=t["id"]))
    return render_template("templates/edit.html", t=t, form=t, CATEGORIES=CATEGORIES, FIELDS=render.FIELDS,
                           versions=_versions(t["id"]))


def _versions(tid):
    return query("SELECT v.*, u.full_name FROM template_versions v LEFT JOIN users u ON u.id = v.created_by "
                 "WHERE template_id = ? ORDER BY version DESC", (tid,))


@bp.route("/<int:template_id>/preview")
@require("view_templates")
def preview(template_id):
    t = get_template(template_id)
    ws = workspace()
    contact = None
    cid = request.args.get("contact_id", type=int)
    if cid:
        contact = query("SELECT * FROM contacts WHERE id = ? AND workspace_id = ?", (cid, ws["id"]), one=True)
    version = request.args.get("version", type=int)
    src = t
    if version:
        src = query("SELECT * FROM template_versions WHERE template_id = ? AND version = ?", (t["id"], version), one=True) or t
    subject, html_out, text_out, headers = render.preview(ws, src["subject"], src["html_body"], src["text_body"],
                                                          src["preheader"], contact)
    missing = []
    if contact:
        ctx = render.context_for(contact, ws)
        for m in render.TOKEN.finditer(src["subject"] + src["html_body"] + src["text_body"]):
            if not ctx.get(m.group(1)) and not m.group(2) and m.group(1) != "unsubscribe_url":
                missing.append(m.group(1))
    sample_contacts = query("SELECT id, email, first_name, last_name, company FROM contacts WHERE workspace_id = ? "
                            "ORDER BY id DESC LIMIT 30", (ws["id"],))
    return render_template("templates/preview.html", t=t, subject=subject, html_out=html_out, text_out=text_out,
                           headers=headers, contact=contact, sample_contacts=sample_contacts, missing=sorted(set(missing)),
                           findings=content_check(src["subject"], src["html_body"], src["text_body"]), version=version)


@bp.route("/<int:template_id>/restore/<int:version>", methods=["POST"])
@require("edit_templates")
def restore(template_id, version):
    t = get_template(template_id)
    v = query("SELECT * FROM template_versions WHERE template_id = ? AND version = ?", (t["id"], version), one=True)
    if not v:
        abort(404)
    new_version = t["version"] + 1
    with transaction():
        execute("UPDATE templates SET subject = ?, preheader = ?, html_body = ?, text_body = ?, version = ?, approved_at = NULL, "
                "approved_by = NULL, updated_at = ? WHERE id = ?",
                (v["subject"], v["preheader"], v["html_body"], v["text_body"], new_version, now(), t["id"]))
        execute("INSERT INTO template_versions (template_id, version, subject, preheader, html_body, text_body, created_by, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)", (t["id"], new_version, v["subject"], v["preheader"], v["html_body"],
                                                         v["text_body"], current_user()["id"], now()))
    flash(f"Version {version} restored as version {new_version}.", "success")
    return redirect(url_for("templates.edit", template_id=t["id"]))


@bp.route("/<int:template_id>/duplicate", methods=["POST"])
@require("edit_templates")
def duplicate(template_id):
    t = get_template(template_id)
    tid = create_template(t["workspace_id"], f"{t['name']} (copy)"[:120], t["category"], t["subject"], t["html_body"],
                          t["text_body"], t["preheader"], ai_generated=bool(t["ai_generated"]))
    flash("Template duplicated.", "success")
    return redirect(url_for("templates.edit", template_id=tid))


@bp.route("/<int:template_id>/approve", methods=["POST"])
@require("approve_campaigns")
def approve(template_id):
    t = get_template(template_id)
    errors = [f for f in content_check(t["subject"], t["html_body"], t["text_body"]) if f["level"] == "error"]
    if errors:
        flash("Fix these first: " + "; ".join(e["title"] for e in errors), "error")
        return redirect(url_for("templates.preview", template_id=t["id"]))
    execute("UPDATE templates SET approved_at = ?, approved_by = ? WHERE id = ?", (now(), current_user()["id"], t["id"]))
    audit("template.approved", target=t["id"], detail=f"version {t['version']}")
    flash("Template approved for use in campaigns.", "success")
    return redirect(url_for("templates.edit", template_id=t["id"]))


@bp.route("/<int:template_id>/archive", methods=["POST"])
@require("edit_templates")
def archive(template_id):
    t = get_template(template_id)
    execute("UPDATE templates SET archived = ? WHERE id = ?", (0 if t["archived"] else 1, t["id"]))
    flash("Template " + ("restored." if t["archived"] else "archived."), "success")
    return redirect(url_for("templates.index"))


def send_test(ws, to_email, subject, html_body, text_body, preheader="", contact=None, kind="template_test", style="branded"):
    """Send one [TEST] copy to a team member's own address (never to contacts). Returns (status, error)."""
    sample = contact or dict(render.SAMPLE_CONTACT, email_normalized=to_email.lower())
    subj, html_out, text_out, headers = render.build_marketing(ws, sample, subject, html_body, text_body, preheader, test=True,
                                                               style=style)
    key = f"test-{kind}-{secrets.token_hex(10)}"
    mid, _ = mailer.record_message(category="test", kind=kind, to_email=to_email, subject=subj, idempotency_key=key,
                                   workspace_id=ws["id"])
    payload = {"from": f"{ws['sender_name'] or ws['name']} <{ws['sender_email']}>", "to": [to_email], "subject": subj,
               "html": html_out, "text": text_out, "headers": headers, "tags": [{"name": "category", "value": "test"}]}
    if ws["reply_to"]:
        payload["reply_to"] = ws["reply_to"]
    provider = mailer.workspace_provider(ws)
    result = provider.send(payload, key)
    result.transient = False
    return mailer.apply_result(mid, provider, result), result.error


def team_addresses(workspace_id):
    return [r["email"] for r in query("SELECT email FROM users WHERE workspace_id = ? AND is_active = 1", (workspace_id,))]


@bp.route("/<int:template_id>/test", methods=["POST"])
@require("edit_templates")
def test(template_id):
    t = get_template(template_id)
    ws = workspace()
    to = request.form.get("to", "").strip().lower()
    if to not in team_addresses(ws["id"]):
        flash("Test emails can only go to members of your team.", "error")
        return redirect(url_for("templates.preview", template_id=t["id"]))
    if not ws["sender_email"]:
        flash("Set your sender email address in Settings first.", "error")
        return redirect(url_for("templates.preview", template_id=t["id"]))
    status, error = send_test(ws, to, t["subject"], t["html_body"], t["text_body"], t["preheader"])
    if status == "failed":
        flash(f"The test email could not be sent: {error}", "error")
    elif status == "simulated":
        flash("Practice mode: the test was saved locally, not sent. Turn off practice mode in Settings → Connections to send for real.", "info")
    else:
        flash(f"Test email accepted by the sending service for {to}. Check the inbox (and spam folder).", "success")
    return redirect(url_for("templates.preview", template_id=t["id"]))
