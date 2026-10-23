"""Navigation, Gmail style: a Compose button, then Sent, Contacts and Settings; everything else under "More tools".

Related pages appear as tabs inside each section.

Each tab lists the endpoints (prefix match) that belong to it, so the right section and tab are highlighted on
every page. Sections and tabs are only shown to people with the permission they need.
"""
from flask import request

from .security import can, current_user

# (section label, icon, [ (tab label, endpoint, url args, permission or None, endpoint prefixes) ])
SECTIONS = [
    ("Sent", "📤", "main", [
        ("Sent", "simple.sent", {}, "view_campaigns", ("main.overview", "simple.sent", "main.search")),
    ]),
    ("Contacts", "👥", "main", [
        ("All contacts", "contacts.index", {}, "view_contacts",
         ("contacts.index", "contacts.detail", "contacts.edit", "contacts.new")),
        ("Import a file", "contacts.import_upload", {}, "edit_contacts", ("contacts.import",)),
        ("Find on websites", "discovery.index", {}, "discover", ("discovery.",)),
        ("Check emails", "verification.index", {}, "verify", ("verification.index", "verification.job")),
        ("Clean up list", "verification.cleaning", {}, "verify", ("verification.cleaning",)),
        ("Do-not-email list", "suppression.index", {}, "view_contacts", ("suppression.",)),
    ]),
    ("Settings", "⚙", "main", [
        ("Company & sender", "settings.index", {}, "manage_settings", ("settings.index",)),
        ("Connections", "settings.integrations", {}, "manage_integrations", ("settings.integrations",)),
        ("Team", "admin.team", {}, "manage_users", ("admin.team",)),
        ("Activity log", "admin.audit_log", {}, "view_audit", ("admin.audit_log",)),
        ("Account emails", "main.transactional", {}, "transactional", ("main.transactional",)),
        ("My security", "auth.security", {}, None, ("auth.security",)),
        ("System", "admin.system", {}, "superadmin", ("admin.system",)),
    ]),
    ("Dashboard", "⌂", "more", [
        ("Dashboard", "main.dashboard", {}, None, ("main.dashboard", "main.onboarding")),
    ]),
    ("Saved emails & assistant", "✉", "more", [
        ("Saved emails", "templates.index", {}, "view_templates", ("templates.",)),
        ("Writing assistant", "ai.index", {}, "use_ai", ("ai.",)),
    ]),
    ("Campaigns (advanced)", "📣", "more", [
        ("All campaigns", "campaigns.index", {}, "view_campaigns", ("campaigns.",)),
        ("With follow-ups", "campaigns.index", {"sequences": 1}, "view_campaigns", ()),
    ]),
    ("Reports", "📊", "more", [
        ("Results", "analytics.index", {}, "view_analytics", ("analytics.",)),
        ("Email health", "deliverability.index", {}, "view_deliverability", ("deliverability.",)),
    ]),
]


def _allowed(permission, user):
    if permission is None:
        return True
    if permission == "superadmin":
        return bool(user["is_superadmin"])
    return can(permission, user)


def _active_tab(tabs):
    endpoint = request.endpoint or ""
    sequences = request.args.get("sequences") == "1"
    for i, (_, ep, args, _perm, prefixes) in enumerate(tabs):
        if args.get("sequences") and sequences and endpoint == ep:
            return i
    for i, (_, ep, args, _perm, prefixes) in enumerate(tabs):
        if not sequences or ep != "campaigns.index":
            if any(endpoint.startswith(p) for p in prefixes):
                return i
    return None


def navigation():
    """The sections this user may see, each with its visible tabs and which one is active."""
    user = current_user()
    if user is None:
        return []
    out = []
    for label, icon, group, tabs in SECTIONS:
        if not user["workspace_id"] and label != "Settings":
            continue
        visible = [t for t in tabs if _allowed(t[3], user) and (user["workspace_id"] or t[1] in ("admin.system", "auth.security"))]
        if not visible:
            continue
        active = _active_tab(visible)
        out.append({"label": label, "icon": icon, "group": group, "url": (visible[0][1], visible[0][2]),
                    "active": active is not None,
                    "tabs": [{"label": t[0], "endpoint": t[1], "args": t[2], "active": i == active}
                             for i, t in enumerate(visible)]})
    return out
