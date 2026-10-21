"""Email content rendering: personalisation fields, escaping, the marketing footer and unsubscribe headers.

Fields are written {{first_name}} or {{first_name|there}} (a fallback for empty values). Only the fields in FIELDS
are allowed; anything else is reported as an error before a template can be saved or sent. Values are HTML-escaped
in HTML bodies. Every marketing email gets the sender's identity, postal address and a visible unsubscribe link,
plus List-Unsubscribe / List-Unsubscribe-Post headers for one-click unsubscribe (RFC 8058).
"""
import html
import re

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

FIELDS = {
    "first_name": "Contact's first name", "last_name": "Contact's last name", "full_name": "First and last name",
    "company": "Contact's company", "job_title": "Job title", "email": "Contact's email address",
    "domain": "Contact's email domain", "country": "Country", "industry": "Industry",
    "sender_name": "Your sender name", "sender_company": "Your company name", "unsubscribe_url": "Unsubscribe link",
}
TOKEN = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*(?:\|\s*([^}]{0,60}?)\s*)?\}\}")
SAMPLE_CONTACT = {"first_name": "Alex", "last_name": "Example", "company": "Example Trading Ltd", "job_title": "Director",
                  "email": "alex@example.com", "domain": "example.com", "country": "United Kingdom",
                  "industry": "Retail"}


def unknown_fields(*texts):
    found = set()
    for t in texts:
        for m in TOKEN.finditer(t or ""):
            if m.group(1) not in FIELDS:
                found.add(m.group(1))
    return sorted(found)


def strip_scripts(markup):
    """Email clients ignore scripts anyway; remove them (and inline handlers) so previews are inert."""
    markup = re.sub(r"(?is)<\s*(script|iframe|object|embed|form)\b.*?>.*?<\s*/\s*\1\s*>", "", markup or "")
    markup = re.sub(r"(?is)<\s*(script|iframe|object|embed|form|meta|base)\b[^>]*>", "", markup)
    markup = re.sub(r"(?i)\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", markup)
    return re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r"\1=\2#\2", markup)


def html_to_text(markup):
    text = re.sub(r"(?is)<(style|head)\b.*?</\1>", "", markup or "")
    text = re.sub(r"(?i)<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>|</li>|</tr>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def context_for(contact, ws, unsubscribe_url=""):
    c = dict(contact) if contact else {}
    first, last = (c.get("first_name") or "").strip(), (c.get("last_name") or "").strip()
    return {
        "first_name": first, "last_name": last, "full_name": (first + " " + last).strip(),
        "company": c.get("company", ""), "job_title": c.get("job_title", ""), "email": c.get("email", ""),
        "domain": c.get("domain") or (c.get("email", "").rsplit("@", 1)[-1] if "@" in c.get("email", "") else ""),
        "country": c.get("country", ""), "industry": c.get("industry", ""),
        "sender_name": ws["sender_name"] or ws["name"], "sender_company": ws["company_name"] or ws["name"],
        "unsubscribe_url": unsubscribe_url,
    }


def fill(text, ctx, escape):
    def sub(m):
        value = str(ctx.get(m.group(1), "") or "").strip() or (m.group(2) or "")
        return html.escape(value) if escape else value
    return TOKEN.sub(sub, text or "")


# ---------------------------------------------------------------- unsubscribe tokens
def _serializer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="gm-unsubscribe")


def unsubscribe_token(workspace_id, email_normalized, message_id=None):
    return _serializer().dumps({"w": workspace_id, "e": email_normalized, "m": message_id})


def read_unsubscribe_token(token):
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    return data if isinstance(data, dict) and "w" in data and "e" in data else None


def unsubscribe_url(workspace_id, email_normalized, message_id=None):
    # Built from BASE_URL rather than url_for: the background worker has no request to build URLs from.
    return f"{current_app.config['BASE_URL']}/u/{unsubscribe_token(workspace_id, email_normalized, message_id)}"


# ---------------------------------------------------------------- full marketing message
def footer_html(ws, unsub):
    parts = [html.escape(ws["company_name"] or ws["name"])]
    if ws["company_address"]:
        parts.append(html.escape(ws["company_address"]).replace("\n", ", "))
    extra = f"<p style=\"margin:8px 0 0\">{html.escape(ws['marketing_footer'])}</p>" if ws["marketing_footer"] else ""
    return ("<div style=\"margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font:12px/1.5 Arial,sans-serif;"
            "color:#6b7280\">" f"<p style=\"margin:0\">{' · '.join(parts)}</p>{extra}"
            f"<p style=\"margin:8px 0 0\">You are receiving this because of your business relationship or interest. "
            f"<a href=\"{html.escape(unsub)}\" style=\"color:#6b7280\">Unsubscribe</a> at any time.</p></div>")


def footer_text(ws, unsub):
    lines = ["", "--", ws["company_name"] or ws["name"]]
    if ws["company_address"]:
        lines.append(ws["company_address"].replace("\n", ", "))
    if ws["marketing_footer"]:
        lines.append(ws["marketing_footer"])
    lines.append(f"Unsubscribe: {unsub}")
    return "\n".join(lines)


def wrap_html(body, preheader="", brand="#2563eb"):
    pre = (f"<div style=\"display:none;max-height:0;overflow:hidden;opacity:0\">{html.escape(preheader)}</div>"
           if preheader else "")
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            "</head><body style=\"margin:0;background:#f6f7f9\">" + pre +
            "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr><td align=\"center\" "
            "style=\"padding:24px 12px\"><table role=\"presentation\" width=\"100%\" style=\"max-width:600px;background:#fff;"
            f"border-radius:8px;border-top:4px solid {html.escape(brand)}\"><tr><td style=\"padding:28px;font:15px/1.6 Arial,"
            "sans-serif;color:#111827\">" + body + "</td></tr></table></td></tr></table></body></html>")


def build_marketing(ws, contact, subject, html_body, text_body, preheader="", message_id=None, test=False):
    """Returns (subject, html, text, headers) personalised for one contact."""
    c = dict(contact) if contact else {}
    email_norm = (c.get("email_normalized") or c.get("email") or "test@example.com").lower()
    unsub = unsubscribe_url(ws["id"], email_norm, message_id)
    ctx = context_for(c, ws, unsub)
    subj = fill(subject, ctx, escape=False).replace("\r", " ").replace("\n", " ").strip()
    body_html = fill(strip_scripts(html_body), ctx, escape=True)
    body_text = fill(text_body, ctx, escape=False) if text_body and text_body.strip() else html_to_text(body_html)
    if test:
        subj = "[TEST] " + subj
    full_html = wrap_html(body_html + footer_html(ws, unsub), fill(preheader, ctx, escape=False), ws["brand_color"])
    full_text = body_text + "\n" + footer_text(ws, unsub)
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return subj, full_html, full_text, headers


def preview(ws, subject, html_body, text_body, preheader="", contact=None):
    sample = contact or dict(SAMPLE_CONTACT, email_normalized=SAMPLE_CONTACT["email"])
    return build_marketing(ws, sample, subject, html_body, text_body, preheader)
