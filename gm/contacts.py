"""Contacts: list, filters, detail and timeline, manual entry, bulk actions, CSV export and CSV import.

Importing a contact never makes it eligible for marketing by itself. Every contact records where it came from,
why it is held (purpose), whether the recipient is a business or a consumer, and the lawful basis claimed;
campaigns check those fields (see campaigns.eligibility) before anyone is emailed.
"""
import csv
import io
import json
import os
import secrets

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for)

from . import jobs, validation
from .db import execute, now, query, scalar, transaction
from .security import audit, can, current_user, require, ws_id

bp = Blueprint("contacts", __name__, url_prefix="/contacts")

FIELDS = ["email", "first_name", "last_name", "company", "job_title", "website", "phone", "country", "industry", "timezone",
          "notes"]
FIELD_LABELS = {"email": "Email", "first_name": "First name", "last_name": "Last name", "company": "Company",
                "job_title": "Job title", "website": "Website", "phone": "Phone", "country": "Country",
                "industry": "Industry", "timezone": "Time zone", "notes": "Notes", "tags": "Tags (comma separated)",
                "source_url": "Source URL"}
HEADER_GUESSES = {
    "email": ["email", "e-mail", "email address", "mail", "work email", "business email"],
    "first_name": ["first name", "firstname", "first", "given name", "forename"],
    "last_name": ["last name", "lastname", "surname", "family name", "last"],
    "company": ["company", "company name", "organisation", "organization", "business", "account"],
    "job_title": ["job title", "title", "position", "role"],
    "website": ["website", "web", "url", "site", "domain"],
    "phone": ["phone", "telephone", "phone number", "mobile"],
    "country": ["country", "country code", "nation"],
    "industry": ["industry", "sector", "vertical"],
    "timezone": ["timezone", "time zone", "tz"],
    "notes": ["notes", "note", "comments"],
    "tags": ["tags", "tag", "labels", "segment"],
    "source_url": ["source", "source url", "source page"],
}
# Labels shown to people. The keys are stored in the database and never change.
VERIFICATION = {"unverified": "Not checked yet", "validated": "Looks good", "invalid": "Bad address", "risky": "Check first",
                "unknown": "Couldn't check", "suppressed": "Do not email"}
STATUSES = {"active": "Active", "unsubscribed": "Unsubscribed", "bounced": "Email bounced", "complained": "Reported as spam",
            "suppressed": "Do not email"}
LAWFUL_BASES = {"consent": "They agreed to hear from us", "legitimate_interest": "Business contact (relevant to their job)",
                "existing_customer": "Existing customer", "unknown": "Not sure / not recorded"}
RECIPIENT_TYPES = {"business": "A business address", "consumer": "A private person", "unknown": "Not sure"}
SOURCES = {"manual": "Added by hand", "import": "Imported file", "discovery": "Found on a website", "api": "API",
           "sandbox": "Practice sample"}
SORTS = {"created": "c.created_at", "email": "c.email_normalized", "company": "c.company", "domain": "c.domain",
         "status": "c.verification_status", "name": "c.last_name"}
PAGE_SIZE = 50


def csv_safe(value):
    """Stop spreadsheet formula injection when exported files are opened in Excel or Sheets."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# ---------------------------------------------------------------- data helpers
def add_event(workspace_id, contact_id, kind, detail="", campaign_id=None, message_id=None, simulated=False):
    execute("INSERT INTO contact_events (workspace_id, contact_id, kind, detail, campaign_id, message_id, simulated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (workspace_id, contact_id, kind, detail[:500], campaign_id, message_id,
                                                int(simulated), now()))


def tag_ids(workspace_id, names):
    ids = []
    for name in {n.strip()[:40] for n in names if n and n.strip()}:
        execute("INSERT OR IGNORE INTO tags (workspace_id, name) VALUES (?, ?)", (workspace_id, name))
        ids.append(query("SELECT id FROM tags WHERE workspace_id = ? AND name = ?", (workspace_id, name), one=True)["id"])
    return ids


def apply_tags(workspace_id, contact_id, names):
    for tid in tag_ids(workspace_id, names):
        execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)", (contact_id, tid))


def domain_of(email_normalized):
    return email_normalized.rsplit("@", 1)[1] if "@" in email_normalized else ""


def create_contact(workspace_id, email, data, source, **meta):
    """Insert a contact. Returns (id, created); an existing address returns its id with created=False."""
    normalized = validation.normalize(email)
    if not normalized:
        raise ValueError("Invalid email address")
    existing = query("SELECT id FROM contacts WHERE workspace_id = ? AND email_normalized = ?", (workspace_id, normalized),
                     one=True)
    if existing:
        return existing["id"], False
    cols = {f: (data.get(f) or "").strip()[:500] for f in FIELDS if f != "email"}
    cid = execute(
        "INSERT INTO contacts (workspace_id, email, email_normalized, domain, " + ", ".join(cols) + ", source, source_url, "
        "source_detail, purpose, recipient_type, lawful_basis, consent_at, import_id, sandbox, is_role, is_disposable, "
        "created_at, updated_at) VALUES (" + ",".join("?" * (4 + len(cols) + 13)) + ")",
        (workspace_id, email.strip(), normalized, domain_of(normalized), *cols.values(), source,
         meta.get("source_url", "")[:500], meta.get("source_detail", "")[:300], meta.get("purpose", "")[:300],
         meta.get("recipient_type", "unknown"), meta.get("lawful_basis", "unknown"),
         now() if meta.get("lawful_basis") == "consent" else None, meta.get("import_id"), int(meta.get("sandbox", 0)),
         int(validation.is_role(normalized.split("@")[0])), int(validation.is_disposable(domain_of(normalized))),
         now(), now()))
    add_event(workspace_id, cid, "created", f"Added via {SOURCES.get(source, source)}"
              + (f" from {meta['source_url']}" if meta.get("source_url") else ""))
    return cid, True


def get_contact(contact_id):
    c = query("SELECT * FROM contacts WHERE id = ? AND workspace_id = ?", (contact_id, ws_id()), one=True)
    if not c:
        abort(404)
    return c


def filter_sql(workspace_id, f):
    """WHERE clause for a contact filter dict (also used for campaign segments)."""
    where, args = ["c.workspace_id = ?"], [workspace_id]
    if f.get("q"):
        like = f"%{f['q'].strip().lower()}%"
        where.append("(c.email_normalized LIKE ? OR lower(c.first_name || ' ' || c.last_name) LIKE ? OR lower(c.company) LIKE ?)")
        args += [like, like, like]
    for key in ("company", "country", "industry"):
        if f.get(key):
            where.append(f"lower(c.{key}) LIKE ?")
            args.append(f"%{f[key].strip().lower()}%")
    if f.get("domain"):
        where.append("c.domain = ?")
        args.append(f["domain"].strip().lower())
    for key, allowed in (("source", SOURCES), ("verification_status", VERIFICATION), ("status", STATUSES),
                         ("recipient_type", RECIPIENT_TYPES), ("lawful_basis", LAWFUL_BASES)):
        values = f.get(key)
        if isinstance(values, str):
            values = [values] if values else []
        values = [v for v in (values or []) if v in allowed]
        if values:
            where.append(f"c.{key} IN ({','.join('?' * len(values))})")
            args += values
    if f.get("tag"):
        where.append("EXISTS (SELECT 1 FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id WHERE ct.contact_id = c.id "
                     "AND t.name = ?)")
        args.append(f["tag"])
    if str(f.get("import_id", "")).isdigit():
        where.append("c.import_id = ?")
        args.append(int(f["import_id"]))
    if f.get("exclude_role"):
        where.append("c.is_role = 0")
    if f.get("exclude_sandbox"):
        where.append("c.sandbox = 0")
    return " AND ".join(where), args


def _filters_from_request():
    keys = ("q", "company", "domain", "country", "source", "industry", "tag", "verification_status", "status",
            "recipient_type", "lawful_basis", "import_id")
    return {k: request.args.get(k, "").strip() for k in keys if request.args.get(k, "").strip()}


def tags_for(contact_ids):
    if not contact_ids:
        return {}
    rows = query(f"SELECT ct.contact_id, t.name FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id "
                 f"WHERE ct.contact_id IN ({','.join('?' * len(contact_ids))}) ORDER BY t.name", contact_ids)
    out = {}
    for r in rows:
        out.setdefault(r["contact_id"], []).append(r["name"])
    return out


# ---------------------------------------------------------------- list, detail, create, edit
@bp.route("/")
@require("view_contacts")
def index():
    wid = ws_id()
    f = _filters_from_request()
    where, args = filter_sql(wid, f)
    sort = request.args.get("sort", "created")
    direction = "ASC" if request.args.get("dir") == "asc" else "DESC"
    order = SORTS.get(sort, SORTS["created"])
    page = max(1, request.args.get("page", 1, type=int))
    total = scalar(f"SELECT COUNT(*) FROM contacts c WHERE {where}", args)
    rows = query(f"SELECT c.* FROM contacts c WHERE {where} ORDER BY {order} {direction}, c.id DESC LIMIT ? OFFSET ?",
                 (*args, PAGE_SIZE, (page - 1) * PAGE_SIZE))
    groups = query("SELECT t.name, COUNT(ct.contact_id) AS n FROM tags t LEFT JOIN contact_tags ct ON ct.tag_id = t.id "
                   "WHERE t.workspace_id = ? AND t.name NOT LIKE 'compose-%' GROUP BY t.id HAVING n > 0 ORDER BY t.name", (wid,))
    files = query("SELECT i.id, i.filename, i.created_at, COUNT(c.id) AS n FROM imports i JOIN contacts c ON c.import_id = i.id "
                  "WHERE i.workspace_id = ? GROUP BY i.id ORDER BY i.id DESC LIMIT 20", (wid,))
    everyone = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ?", (wid,))
    imported = scalar("SELECT COUNT(*) FROM contacts WHERE workspace_id = ? AND import_id IS NOT NULL", (wid,))
    return render_template("contacts/list.html", contacts=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                           groups=groups, files=files, everyone=everyone, imported=imported,
                           filters=f, sort=sort, dir=direction.lower(), tags=tags_for([r["id"] for r in rows]),
                           all_tags=query("SELECT name FROM tags WHERE workspace_id = ? ORDER BY name", (wid,)),
                           any_contacts=scalar("SELECT 1 FROM contacts WHERE workspace_id = ? LIMIT 1", (wid,)))


@bp.route("/new", methods=["GET", "POST"])
@require("edit_contacts")
def new():
    form = request.form
    if request.method == "POST":
        wid = ws_id()
        ok, value = validation.syntax_check(form.get("email", ""))
        if not ok:
            flash(f"That email address is not valid: {value}", "error")
            return render_template("contacts/form.html", contact=None, form=form), 400
        if form.get("recipient_type") not in RECIPIENT_TYPES or form.get("lawful_basis") not in LAWFUL_BASES:
            abort(400)
        cid, created = create_contact(wid, form["email"], form, "manual", source_detail=form.get("source_detail", ""),
                                      source_url=form.get("source_url", ""), purpose=form.get("purpose", ""),
                                      recipient_type=form["recipient_type"], lawful_basis=form["lawful_basis"])
        if not created:
            flash("That address is already in your contacts.", "info")
            return redirect(url_for("contacts.detail", contact_id=cid))
        apply_tags(wid, cid, form.get("tags", "").split(","))
        result = validation.check(form["email"], wid, dns=form.get("dns") == "on")
        validation.store_on_contact(cid, result)
        audit("contact.created", target=value)
        flash("Contact added.", "success")
        return redirect(url_for("contacts.detail", contact_id=cid))
    return render_template("contacts/form.html", contact=None, form=form)


@bp.route("/<int:contact_id>")
@require("view_contacts")
def detail(contact_id):
    c = get_contact(contact_id)
    events = query("SELECT e.*, cp.name AS campaign_name FROM contact_events e LEFT JOIN campaigns cp ON cp.id = e.campaign_id "
                   "WHERE e.contact_id = ? ORDER BY e.id DESC LIMIT 200", (contact_id,))
    messages = query("SELECT m.*, cp.name AS campaign_name FROM messages m LEFT JOIN campaigns cp ON cp.id = m.campaign_id "
                     "WHERE m.contact_id = ? AND m.workspace_id = ? ORDER BY m.id DESC LIMIT 50", (contact_id, c["workspace_id"]))
    campaigns = query("SELECT r.*, cp.name FROM campaign_recipients r JOIN campaigns cp ON cp.id = r.campaign_id "
                      "WHERE r.contact_id = ? ORDER BY r.id DESC", (contact_id,))
    detail_json = json.loads(c["verification_detail"]) if c["verification_detail"] else None
    suppressed = query("SELECT * FROM suppressions WHERE workspace_id = ? AND email_normalized = ?",
                       (c["workspace_id"], c["email_normalized"]), one=True)
    return render_template("contacts/detail.html", c=c, events=events, messages=messages, campaigns=campaigns,
                           verification=detail_json, suppressed=suppressed, tags=tags_for([contact_id]).get(contact_id, []))


@bp.route("/<int:contact_id>/edit", methods=["GET", "POST"])
@require("edit_contacts")
def edit(contact_id):
    c = get_contact(contact_id)
    if request.method == "POST":
        form = request.form
        if form.get("recipient_type") not in RECIPIENT_TYPES or form.get("lawful_basis") not in LAWFUL_BASES:
            abort(400)
        values = {f: form.get(f, "").strip()[:500] for f in FIELDS if f != "email"}
        consent_at = c["consent_at"] or (now() if form["lawful_basis"] == "consent" else None)
        execute("UPDATE contacts SET " + ", ".join(f"{k} = ?" for k in values) + ", purpose = ?, recipient_type = ?, "
                "lawful_basis = ?, consent_at = ?, source_detail = ?, marketing_opt_out = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (*values.values(), form.get("purpose", "")[:300], form["recipient_type"], form["lawful_basis"], consent_at,
                 form.get("source_detail", "")[:300], int(form.get("marketing_opt_out") == "on"), now(), contact_id,
                 c["workspace_id"]))
        execute("DELETE FROM contact_tags WHERE contact_id = ?", (contact_id,))
        apply_tags(c["workspace_id"], contact_id, form.get("tags", "").split(","))
        if form.get("marketing_opt_out") == "on" and not c["marketing_opt_out"]:
            add_event(c["workspace_id"], contact_id, "preference", "Marketing opt-out recorded by a team member")
            from .campaigns import stop_for_contact
            stop_for_contact(contact_id, "Opted out of marketing")
        add_event(c["workspace_id"], contact_id, "updated", f"Edited by {current_user()['full_name']}")
        audit("contact.updated", target=c["email_normalized"])
        flash("Contact saved.", "success")
        return redirect(url_for("contacts.detail", contact_id=contact_id))
    return render_template("contacts/form.html", contact=c, form=c, tags=", ".join(tags_for([contact_id]).get(contact_id, [])))


@bp.route("/<int:contact_id>/verify", methods=["POST"])
@require("verify")
def verify_one(contact_id):
    c = get_contact(contact_id)
    result = validation.check(c["email"], c["workspace_id"], dns=True)
    validation.store_on_contact(contact_id, result)
    add_event(c["workspace_id"], contact_id, "verification", f"{VERIFICATION[result['status']]}: {result['summary']}")
    flash(f"Checked: {VERIFICATION[result['status']]}. {result['summary']}", "success")
    return redirect(url_for("contacts.detail", contact_id=contact_id))


@bp.route("/<int:contact_id>/suppress", methods=["POST"])
@require("edit_contacts")
def suppress(contact_id):
    c = get_contact(contact_id)
    from .suppression import add_suppression, remove_suppression
    if request.form.get("action") == "remove":
        if not can("manage_settings"):
            abort(403)
        remove_suppression(c["workspace_id"], c["email_normalized"])
        flash("Removed from the suppression list. Only do this if the person has clearly asked to hear from you again.", "info")
    else:
        add_suppression(c["workspace_id"], c["email_normalized"], "manual", f"By {current_user()['full_name']}")
        flash("Address suppressed. It will not receive marketing email.", "success")
    return redirect(url_for("contacts.detail", contact_id=contact_id))


@bp.route("/<int:contact_id>/delete", methods=["POST"])
@require("delete_contacts")
def delete(contact_id):
    c = get_contact(contact_id)
    execute("DELETE FROM contacts WHERE id = ? AND workspace_id = ?", (contact_id, c["workspace_id"]))
    audit("contact.deleted", target=c["email_normalized"])
    flash("Contact deleted. Its suppression record (if any) is kept so the address stays protected.", "success")
    return redirect(url_for("contacts.index"))


# ---------------------------------------------------------------- bulk actions and export
def _selected_ids():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if request.form.get("all_matching") == "1":
        f = json.loads(request.form.get("filters") or "{}")
        where, args = filter_sql(ws_id(), f)
        ids = [r["id"] for r in query(f"SELECT c.id FROM contacts c WHERE {where} LIMIT 20000", args)]
    return ids


@bp.route("/bulk", methods=["POST"])
@require("edit_contacts")
def bulk():
    wid = ws_id()
    ids = _selected_ids()
    action = request.form.get("action", "")
    if not ids:
        flash("Select at least one contact first.", "error")
        return redirect(request.referrer or url_for("contacts.index"))
    owned = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        owned += [r["id"] for r in query(f"SELECT id FROM contacts WHERE workspace_id = ? AND id IN ({','.join('?' * len(chunk))})",
                                         (wid, *chunk))]
    if not owned:
        abort(404)
    def rows(sql_prefix, extra=()):
        """Run an id-list query in chunks of 500 (older SQLite builds allow only 999 parameters)."""
        out = []
        for i in range(0, len(owned), 500):
            chunk = owned[i:i + 500]
            out += query(f"{sql_prefix} ({','.join('?' * len(chunk))})", (*extra, *chunk))
        return out

    if action == "export":
        if not can("export_contacts"):
            abort(403)
        return export_rows(rows("SELECT * FROM contacts WHERE id IN"), "selected-contacts.csv")
    if action == "delete":
        if not can("delete_contacts"):
            abort(403)
        _delete_ids(wid, owned)
        audit("contact.bulk_deleted", detail=f"{len(owned)} contacts")
        flash(f"Deleted {len(owned)} contacts.", "success")
        return redirect(url_for("contacts.index"))
    elif action in ("tag", "untag"):
        names = [n for n in request.form.get("tag", "").split(",") if n.strip()]
        if not names:
            flash("Enter a tag name.", "error")
            return redirect(request.referrer or url_for("contacts.index"))
        tids = tag_ids(wid, names)
        for cid in owned:
            for tid in tids:
                if action == "tag":
                    execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)", (cid, tid))
                else:
                    execute("DELETE FROM contact_tags WHERE contact_id = ? AND tag_id = ?", (cid, tid))
        flash(f"Updated tags on {len(owned)} contacts.", "success")
    elif action in ("set_business", "set_consumer"):
        for i in range(0, len(owned), 500):
            chunk = owned[i:i + 500]
            execute(f"UPDATE contacts SET recipient_type = ?, updated_at = ? WHERE id IN ({','.join('?' * len(chunk))})",
                    ("business" if action == "set_business" else "consumer", now(), *chunk))
        flash(f"Updated {len(owned)} contacts.", "success")
    elif action == "verify":
        if not can("verify"):
            abort(403)
        job = jobs.enqueue("verify_contacts", {"ids": owned}, workspace_id=wid, user_id=current_user()["id"], total=len(owned))
        flash(f"Checking {len(owned)} contacts in the background.", "success")
        return redirect(url_for("verification.job", job_id=job))
    elif action == "suppress":
        from .suppression import add_suppression
        for r in rows("SELECT email_normalized FROM contacts WHERE id IN"):
            add_suppression(wid, r["email_normalized"], "manual", f"Bulk action by {current_user()['full_name']}")
        flash(f"Suppressed {len(owned)} addresses.", "success")
    else:
        abort(400)
    return redirect(request.referrer or url_for("contacts.index"))


def _delete_ids(wid, ids):
    """Delete contacts in chunks. Do-not-email entries are kept so those addresses stay protected."""
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        execute(f"DELETE FROM contacts WHERE workspace_id = ? AND id IN ({','.join('?' * len(chunk))})", (wid, *chunk))
    execute("DELETE FROM tags WHERE workspace_id = ? AND id NOT IN (SELECT tag_id FROM contact_tags)", (wid,))
    return len(ids)


@bp.route("/groups/delete", methods=["POST"])
@require("delete_contacts")
def delete_group():
    """Delete every contact in a group (tag), or just remove the group label (keep=1)."""
    wid = ws_id()
    tag = request.form.get("tag", "")
    t = query("SELECT * FROM tags WHERE workspace_id = ? AND name = ?", (wid, tag), one=True)
    if not t:
        abort(404)
    if request.form.get("keep") == "1":
        execute("DELETE FROM tags WHERE id = ?", (t["id"],))
        audit("contacts.group_removed", target=tag)
        flash(f"Group “{tag}” removed. The contacts are still in your list.", "success")
    else:
        ids = [r["contact_id"] for r in query("SELECT contact_id FROM contact_tags WHERE tag_id = ?", (t["id"],))]
        _delete_ids(wid, ids)
        execute("DELETE FROM tags WHERE id = ?", (t["id"],))
        audit("contacts.group_deleted", target=tag, detail=f"{len(ids)} contacts")
        flash(f"Deleted the {len(ids)} contacts in “{tag}”.", "success")
    return redirect(url_for("contacts.index"))


@bp.route("/import/<int:import_id>/delete-contacts", methods=["POST"])
@require("delete_contacts")
def delete_import_contacts(import_id):
    """Undo an import: delete every contact that came from this file."""
    imp = _get_import(import_id)
    ids = [r["id"] for r in query("SELECT id FROM contacts WHERE workspace_id = ? AND import_id = ?",
                                  (imp["workspace_id"], import_id))]
    _delete_ids(imp["workspace_id"], ids)
    audit("import.contacts_deleted", target=imp["filename"], detail=f"{len(ids)} contacts")
    flash(f"Deleted the {len(ids)} contacts imported from {imp['filename']}.", "success")
    return redirect(request.referrer or url_for("contacts.index"))


@bp.route("/imports/delete-all", methods=["POST"])
@require("delete_contacts")
def delete_all_imported():
    """Delete every contact that came from an imported file; contacts added by hand or found by search stay."""
    wid = ws_id()
    ids = [r["id"] for r in query("SELECT id FROM contacts WHERE workspace_id = ? AND import_id IS NOT NULL", (wid,))]
    _delete_ids(wid, ids)
    audit("contacts.imported_deleted", detail=f"{len(ids)} contacts")
    flash(f"Deleted all {len(ids)} imported contacts. Your do-not-email list was kept.", "success")
    return redirect(url_for("contacts.index"))


@bp.route("/delete-all", methods=["POST"])
@require("delete_contacts")
def delete_all():
    """Delete every contact in the workspace (the button sends confirm_text=DELETE after a confirm dialog).
    Do-not-email entries are kept."""
    wid = ws_id()
    if request.form.get("confirm_text", "").strip().upper() != "DELETE":
        flash("Type DELETE in the box to confirm deleting all contacts.", "error")
        return redirect(url_for("contacts.index"))
    ids = [r["id"] for r in query("SELECT id FROM contacts WHERE workspace_id = ?", (wid,))]
    _delete_ids(wid, ids)
    audit("contacts.all_deleted", detail=f"{len(ids)} contacts")
    flash(f"All {len(ids)} contacts deleted. Your do-not-email list was kept so those people stay protected.", "success")
    return redirect(url_for("contacts.index"))


EXPORT_COLUMNS = ["email", "first_name", "last_name", "company", "job_title", "domain", "website", "phone", "country",
                  "industry", "timezone", "source", "source_url", "source_detail", "purpose", "recipient_type", "lawful_basis",
                  "consent_at", "status", "verification_status", "verification_checked_at", "is_role", "is_disposable",
                  "created_at", "last_contacted_at"]


def export_rows(rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS + ["tags"])
    tags = tags_for([r["id"] for r in rows])
    for r in rows:
        w.writerow([csv_safe(r[c]) for c in EXPORT_COLUMNS] + [csv_safe(", ".join(tags.get(r["id"], [])))])
    audit("contact.exported", detail=f"{len(rows)} rows")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/export")
@require("export_contacts")
def export():
    where, args = filter_sql(ws_id(), _filters_from_request())
    return export_rows(query(f"SELECT c.* FROM contacts c WHERE {where} ORDER BY c.id LIMIT 200000", args), "contacts.csv")


# ---------------------------------------------------------------- CSV import
def _decode(raw):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("The file is not readable text. Save it as CSV (UTF-8) and try again.")


def read_csv(path, limit=None):
    with open(path, "rb") as fh:
        text = _decode(fh.read())
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = []
    for i, row in enumerate(reader):
        if limit is not None and i > limit:
            break
        rows.append(row)
    return rows


def guess_mapping(headers):
    mapping = {}
    for idx, h in enumerate(headers):
        key = h.strip().lower().replace("_", " ")
        for field, names in HEADER_GUESSES.items():
            if key in names and field not in mapping.values():
                mapping[str(idx)] = field
                break
    return mapping


def save_upload(file, kind="imports"):
    """Validate and store an uploaded CSV outside the web root. Returns (stored_name, headers, row_count)."""
    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".txt")):
        raise ValueError("Upload a .csv file.")
    raw = file.read(current_app.config["MAX_UPLOAD_BYTES"] + 1)
    if len(raw) > current_app.config["MAX_UPLOAD_BYTES"]:
        raise ValueError(f"The file is larger than {current_app.config['MAX_UPLOAD_BYTES'] // (1024 * 1024)} MB.")
    if b"\x00" in raw[:8192]:
        raise ValueError("That doesn't look like a CSV file (it contains binary data).")
    text = _decode(raw)
    stored = secrets.token_hex(16) + ".csv"
    folder = current_app.config["IMPORT_DIR"]
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, stored), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    rows = read_csv(os.path.join(folder, stored))
    if not rows or not any(cell.strip() for cell in rows[0]):
        os.remove(os.path.join(folder, stored))
        raise ValueError("The file is empty or has no header row.")
    if len(rows) - 1 > current_app.config["MAX_IMPORT_ROWS"]:
        os.remove(os.path.join(folder, stored))
        raise ValueError(f"The file has more than {current_app.config['MAX_IMPORT_ROWS']:,} rows. Split it into smaller files.")
    if len(rows[0]) > 100:
        os.remove(os.path.join(folder, stored))
        raise ValueError("The file has more than 100 columns.")
    return stored, [h.strip()[:80] for h in rows[0]], len(rows) - 1


@bp.route("/import", methods=["GET", "POST"])
@require("edit_contacts")
def import_upload():
    wid = ws_id()
    if request.method == "POST":
        file = request.files.get("file")
        if not file:
            flash("Choose a CSV file to upload.", "error")
            return redirect(url_for("contacts.import_upload"))
        try:
            stored, headers, count = save_upload(file)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("contacts.import_upload"))
        imp = execute("INSERT INTO imports (workspace_id, user_id, filename, stored_name, headers, mapping, total_rows, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (wid, current_user()["id"], (file.filename or "upload.csv")[:120], stored, json.dumps(headers),
                       json.dumps(guess_mapping(headers)), count, now()))
        return redirect(url_for("contacts.import_map", import_id=imp))
    recent = query("SELECT * FROM imports WHERE workspace_id = ? ORDER BY id DESC LIMIT 20", (wid,))
    return render_template("contacts/import_upload.html", imports=recent)


def _get_import(import_id):
    imp = query("SELECT * FROM imports WHERE id = ? AND workspace_id = ?", (import_id, ws_id()), one=True)
    if not imp:
        abort(404)
    return imp


@bp.route("/import/<int:import_id>/map", methods=["GET", "POST"])
@require("edit_contacts")
def import_map(import_id):
    imp = _get_import(import_id)
    if imp["status"] != "uploaded":
        return redirect(url_for("contacts.import_status", import_id=import_id))
    headers = json.loads(imp["headers"])
    if request.method == "POST":
        form = request.form
        mapping = {str(i): form.get(f"col_{i}") for i in range(len(headers))
                   if form.get(f"col_{i}") in list(FIELD_LABELS)}
        if "email" not in mapping.values():
            flash("Choose which column holds the email address.", "error")
            return redirect(url_for("contacts.import_map", import_id=import_id))
        if list(mapping.values()).count("email") > 1:
            flash("Only one column can be the email address.", "error")
            return redirect(url_for("contacts.import_map", import_id=import_id))
        if form.get("recipient_type") not in RECIPIENT_TYPES or form.get("lawful_basis") not in LAWFUL_BASES:
            abort(400)
        # Optional: sensible defaults are recorded when left empty.
        purpose = form.get("purpose", "").strip() or "Business outreach"
        source_detail = form.get("source_detail", "").strip() or f"Imported file {imp['filename']}"
        options = {"purpose": purpose[:300], "source_detail": source_detail[:300],
                   "recipient_type": form["recipient_type"], "lawful_basis": form["lawful_basis"],
                   "tag": form.get("tag", "").strip()[:40], "dns": form.get("dns") == "on",
                   "update_existing": form.get("update_existing") == "on"}
        job = jobs.enqueue("import_contacts", {"import_id": import_id}, workspace_id=imp["workspace_id"],
                           user_id=current_user()["id"], dedupe_key=f"import:{import_id}", total=imp["total_rows"])
        execute("UPDATE imports SET mapping = ?, options = ?, status = 'queued', job_id = ? WHERE id = ?",
                (json.dumps(mapping), json.dumps(options), job, import_id))
        audit("import.started", target=imp["filename"], detail=f"{imp['total_rows']} rows")
        if not options["dns"] and imp["total_rows"] <= IMPORT_NOW_ROWS:
            jobs.run_one(job)  # a few seconds; bigger files and DNS checks go to the background worker
        return redirect(url_for("contacts.import_status", import_id=import_id))
    rows = read_csv(os.path.join(current_app.config["IMPORT_DIR"], imp["stored_name"]), limit=8)[1:]
    return render_template("contacts/import_map.html", imp=imp, headers=headers, mapping=json.loads(imp["mapping"]),
                           preview=rows)


@bp.route("/import/<int:import_id>")
@require("view_contacts")
def import_status(import_id):
    imp = _get_import(import_id)
    job = query("SELECT * FROM jobs WHERE id = ?", (imp["job_id"],), one=True) if imp["job_id"] else None
    issues = query("SELECT * FROM import_issues WHERE import_id = ? ORDER BY row_number LIMIT 200", (import_id,))
    return render_template("contacts/import_status.html", imp=imp, job=job, issues=issues,
                           options=json.loads(imp["options"] or "{}"))


@bp.route("/import/<int:import_id>/report.csv")
@require("view_contacts")
def import_report(import_id):
    imp = _get_import(import_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "email", "outcome", "detail"])
    for r in query("SELECT * FROM import_issues WHERE import_id = ? ORDER BY row_number", (import_id,)):
        w.writerow([r["row_number"], csv_safe(r["email"]), r["outcome"], csv_safe(r["detail"])])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=import-{imp['id']}-report.csv"})


@jobs.handler("import_contacts")
def _run_import(ctx):
    imp = query("SELECT * FROM imports WHERE id = ?", (ctx.params["import_id"],), one=True)
    if imp is None or imp["status"] not in ("queued", "processing"):
        return {"skipped": True}
    wid = imp["workspace_id"]
    execute("UPDATE imports SET status = 'processing' WHERE id = ?", (imp["id"],))
    execute("DELETE FROM import_issues WHERE import_id = ?", (imp["id"],))  # safe re-run after a crash
    mapping = {int(k): v for k, v in json.loads(imp["mapping"]).items()}
    opts = json.loads(imp["options"])
    rows = read_csv(os.path.join(current_app.config["IMPORT_DIR"], imp["stored_name"]))[1:]
    counts = dict(accepted=0, rejected=0, duplicates=0, invalid=0, suppressed=0, review=0)
    seen = set()

    def issue(n, email, outcome, detail=""):
        execute("INSERT INTO import_issues (import_id, row_number, email, outcome, detail) VALUES (?, ?, ?, ?, ?)",
                (imp["id"], n, email[:254], outcome, detail[:300]))

    # Rows are saved 500 at a time in one transaction: one disk write per batch instead of several per row.
    # With DNS checks each row waits on the network, so batches are small to avoid holding the write lock.
    size = 25 if opts.get("dns") else IMPORT_BATCH
    for start in range(0, len(rows), size):
        ctx.check_cancel()
        ctx.progress(start, len(rows), f"Processed {start:,} of {len(rows):,} rows")
        with transaction():
            _import_batch(imp, wid, opts, mapping, rows[start:start + size], start + 2, counts, seen, issue)
    execute("UPDATE imports SET status = 'completed', completed_at = ?, accepted = ?, rejected = ?, duplicates = ?, invalid = ?, "
            "suppressed = ?, review = ? WHERE id = ?", (now(), counts["accepted"], counts["rejected"], counts["duplicates"],
                                                       counts["invalid"], counts["suppressed"], counts["review"], imp["id"]))
    ctx.progress(len(rows), len(rows), "Import finished")
    if not opts.get("dns"):
        # The fast import only checked the format; check every new address's domain in the background.
        ids = [r["id"] for r in query("SELECT id FROM contacts WHERE workspace_id = ? AND import_id = ? AND "
                                      "verification_status = 'unverified'", (wid, imp["id"]))]
        if ids:
            jobs.enqueue("verify_contacts", {"ids": ids}, workspace_id=wid, user_id=ctx.user_id, total=len(ids),
                         dedupe_key=f"verify-import:{imp['id']}")
    return counts


# Only these stay blocked when a list is uploaded again: the person's own opt-out, a spam report, or a dead address.
KEPT_BLOCKED = {"unsubscribe": "Clicked unsubscribe", "complaint": "Reported your email as spam",
                "hard_bounce": "Address doesn't exist (bounced)"}


def _self_blocked(row):
    """True for entries you created yourself: added by hand, list cleaning, or marked "Asked to stop" in Sent."""
    return row["reason"] == "manual" or (row["reason"] == "unsubscribe" and row["source"] == "Replied asking to stop")


IMPORT_BATCH = 500
IMPORT_NOW_ROWS = 20000   # files up to this size are imported during the upload request (about 10 seconds)


def _import_batch(imp, wid, opts, mapping, batch, first_row, counts, seen, issue):
    for n, row in enumerate(batch, start=first_row):
        data = {field: (row[idx].strip() if idx < len(row) else "") for idx, field in mapping.items()}
        email = data.get("email", "")
        if not any(cell.strip() for cell in row):
            continue
        if not email:
            counts["rejected"] += 1
            issue(n, "", "rejected", "No email address in this row")
            continue
        ok, value = validation.syntax_check(email)
        if not ok:
            counts["invalid"] += 1
            issue(n, email, "invalid", value)
            continue
        normalized = value.lower()
        if normalized in seen:
            counts["duplicates"] += 1
            issue(n, email, "duplicate", "Appears earlier in this file")
            continue
        seen.add(normalized)
        blocked = query("SELECT * FROM suppressions WHERE workspace_id = ? AND email_normalized = ?", (wid, normalized), one=True)
        if blocked and _self_blocked(blocked):
            # Blocked earlier by you (by hand, list cleaning or "Asked to stop"): uploading it again is your decision.
            from .suppression import remove_suppression
            remove_suppression(wid, normalized)
            execute("UPDATE contacts SET marketing_opt_out = 0 WHERE workspace_id = ? AND email_normalized = ?", (wid, normalized))
            blocked = None
        if blocked:
            counts["suppressed"] += 1
            issue(n, email, "suppressed", KEPT_BLOCKED.get(blocked["reason"], "Blocked by your email provider") +
                  "; skipped to keep you within anti-spam law and protect your sender reputation")
            continue
        with transaction():
            cid, created = create_contact(wid, email, data, "import", source_url=data.get("source_url", ""),
                                          source_detail=f"{opts['source_detail']} (file: {imp['filename']})",
                                          purpose=opts["purpose"], recipient_type=opts["recipient_type"],
                                          lawful_basis=opts["lawful_basis"], import_id=imp["id"])
            if not created:
                counts["duplicates"] += 1
                if opts.get("update_existing"):
                    blanks = {f: v for f, v in data.items() if f in FIELDS and f != "email" and v}
                    for f, v in blanks.items():
                        execute(f"UPDATE contacts SET {f} = ? WHERE id = ? AND {f} = ''", (v[:500], cid))
                    issue(n, email, "duplicate", "Already a contact; empty fields filled in")
                else:
                    issue(n, email, "duplicate", "Already a contact; left unchanged")
                continue
            tags = [t for t in (data.get("tags", "").split(",") + [opts.get("tag", "")]) if t.strip()]
            apply_tags(wid, cid, tags)
        result = validation.check(email, wid, dns=opts.get("dns", False))
        if not opts.get("dns") and result["status"] == "unknown":
            result["status"] = "unverified"
        validation.store_on_contact(cid, result)
        reasons = []
        if result["is_disposable"]:
            reasons.append("disposable domain")
        if result["status"] in ("risky", "invalid"):
            reasons.append(result["summary"])
        if opts["recipient_type"] == "consumer" and opts["lawful_basis"] != "consent":
            reasons.append("consumer address without recorded consent")
        if reasons:
            counts["review"] += 1
            issue(n, email, "review", "; ".join(reasons))
        else:
            counts["accepted"] += 1


@bp.route("/import/<int:import_id>/cancel", methods=["POST"])
@require("edit_contacts")
def import_cancel(import_id):
    imp = _get_import(import_id)
    if imp["job_id"]:
        jobs.request_cancel(imp["job_id"], imp["workspace_id"])
    execute("UPDATE imports SET status = 'cancelled' WHERE id = ? AND status IN ('uploaded', 'queued', 'processing')", (import_id,))
    flash("Import cancelled. Rows already imported are kept.", "info")
    return redirect(url_for("contacts.import_status", import_id=import_id))
