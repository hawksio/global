"""Resend webhooks: delivery events, bounces, complaints, opens/clicks (approximate), provider suppressions and
inbound replies.

Endpoints
  POST /webhooks/resend/<workspace public id>   marketing events, verified with that workspace's signing secret
  POST /webhooks/resend/system                   transactional events, verified with GM_RESEND_WEBHOOK_SECRET

Every request must carry a valid Svix signature (checked by the official SDK, which also rejects stale timestamps).
Events are stored by their svix-id first, so a redelivered event is acknowledged without being applied twice.
Statuses only move forward (a late "delivered" never overwrites "bounced").
"""
import json

from flask import Blueprint, abort, current_app, jsonify, request

from .db import execute, now, query
from .security import get_secret

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")

RANK = {"queued": 0, "simulated": 1, "accepted": 1, "delivery_delayed": 2, "delivered": 3, "soft_bounced": 3, "failed": 4,
        "suppressed": 4, "bounced": 5, "complained": 6, "cancelled": 0}


def verify(raw, secret):
    import resend
    try:
        return resend.Webhooks.verify({"payload": raw, "webhook_secret": secret, "headers": {
            "id": request.headers.get("svix-id", ""), "timestamp": request.headers.get("svix-timestamp", ""),
            "signature": request.headers.get("svix-signature", "")}})
    except ValueError:
        return None


def _set_status(message, status, column=None):
    if RANK.get(status, 0) >= RANK.get(message["status"], 0) and status != message["status"]:
        execute("UPDATE messages SET status = ? WHERE id = ?", (status, message["id"]))
    if column:
        execute(f"UPDATE messages SET {column} = COALESCE({column}, ?) WHERE id = ?", (now(), message["id"]))


def apply_event(workspace_id, etype, data, simulated=False):
    """Apply one email event. Returns a short outcome string. Used by real webhooks and sandbox simulations."""
    from .contacts import add_event
    from .suppression import add_suppression
    if etype == "email.received":
        return _inbound(workspace_id, data)
    email_id = data.get("email_id") if isinstance(data, dict) else None
    if not email_id or not isinstance(email_id, str):
        return "ignored: no email_id"
    if workspace_id is None:
        message = query("SELECT * FROM messages WHERE provider_id = ? AND category = 'transactional'", (email_id,), one=True)
    else:
        message = query("SELECT * FROM messages WHERE provider_id = ? AND workspace_id = ?", (email_id, workspace_id), one=True)
    if not message:
        return "ignored: unknown message"
    if bool(message["simulated"]) != bool(simulated):
        return "ignored: simulated/real mismatch"
    wid, cid = message["workspace_id"], message["contact_id"]
    email = (message["to_email"] or "").lower()
    label = "Simulated: " if simulated else ""

    def event(kind, detail):
        if cid:
            add_event(wid, cid, kind, label + detail, campaign_id=message["campaign_id"], message_id=message["id"],
                      simulated=simulated)

    if etype == "email.sent":
        _set_status(message, "accepted", "accepted_at")
    elif etype == "email.delivered":
        _set_status(message, "delivered", "delivered_at")
        event("email_delivered", "Delivered to the recipient's mail server (not proof it reached the inbox)")
    elif etype == "email.delivery_delayed":
        _set_status(message, "delivery_delayed")
        event("email_delayed", "Delivery delayed; the provider will keep trying")
    elif etype == "email.bounced":
        bounce = data.get("bounce") or {}
        permanent = str(bounce.get("type", "")).lower() == "permanent"
        _set_status(message, "bounced" if permanent else "soft_bounced", "bounced_at")
        detail = f"{'Hard' if permanent else 'Soft'} bounce: {str(bounce.get('message', ''))[:200]}"
        event("email_bounced", detail)
        if permanent and wid and message["category"] == "marketing":
            add_suppression(wid, email, "hard_bounce", str(bounce.get("subType", ""))[:100], simulated=simulated)
    elif etype == "email.complained":
        _set_status(message, "complained", "complained_at")
        event("email_complained", "Marked as spam by the recipient")
        if wid:
            add_suppression(wid, email, "complaint", "Spam complaint reported by the mailbox provider", simulated=simulated)
    elif etype == "email.opened":
        execute("UPDATE messages SET opened_at = COALESCE(opened_at, ?) WHERE id = ?", (now(), message["id"]))
        event("email_opened", "Opened (approximate: privacy features and image proxies distort opens)")
    elif etype == "email.clicked":
        execute("UPDATE messages SET clicked_at = COALESCE(clicked_at, ?) WHERE id = ?", (now(), message["id"]))
        link = str((data.get("click") or {}).get("link", ""))[:200]
        event("email_clicked", f"Clicked {link}")
    elif etype == "email.failed":
        _set_status(message, "failed")
        reason = str((data.get("failed") or {}).get("reason", ""))[:200]
        execute("UPDATE messages SET last_error = ? WHERE id = ?", (f"Provider failure: {reason}", message["id"]))
        event("email_failed", f"Failed at the provider: {reason}")
    elif etype == "email.suppressed":
        _set_status(message, "suppressed")
        event("email_suppressed", "The provider refused to send because the address is on its suppression list")
        if wid and message["category"] == "marketing":
            add_suppression(wid, email, "provider", str((data.get("suppressed") or {}).get("type", ""))[:100],
                            simulated=simulated)
    else:
        return f"ignored: {etype}"
    return f"applied {etype}"


def _inbound(workspace_id, data):
    """A reply from a contact stops their follow-ups. Matching is by the sender's address, which is reliable enough
    to stop follow-ups but is not shown as proof of engagement."""
    from .campaigns import record_reply
    from .validation import normalize
    if workspace_id is None:
        return "ignored: inbound on system endpoint"
    sender = data.get("from") if isinstance(data, dict) else None
    if isinstance(sender, dict):
        sender = sender.get("email")
    if isinstance(sender, str) and "<" in sender:
        sender = sender.split("<", 1)[1].rstrip(">")
    addr = normalize(sender or "")
    if not addr:
        return "ignored: no sender"
    stopped = 0
    for r in query("SELECT r.* FROM campaign_recipients r JOIN campaigns c ON c.id = r.campaign_id JOIN contacts ct ON "
                   "ct.id = r.contact_id WHERE c.workspace_id = ? AND ct.email_normalized = ? AND r.replied_at IS NULL",
                   (workspace_id, addr)):
        record_reply(r, "Reply received by email")
        stopped += 1
    return f"reply matched {stopped} campaign recipient(s)"


def _handle(workspace_id, secret):
    if not secret:
        abort(404)  # webhooks not configured for this endpoint
    raw = request.get_data(cache=False, as_text=True)
    if len(raw) > 1_000_000:
        abort(413)
    payload = verify(raw, secret)
    if payload is None or not isinstance(payload, dict):
        current_app.logger.warning("Rejected a webhook with an invalid signature")
        abort(401)
    event_id = request.headers.get("svix-id", "")
    etype = str(payload.get("type", ""))[:60]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    execute("INSERT OR IGNORE INTO webhook_events (provider, event_id, type, workspace_id, payload, received_at) "
            "VALUES ('resend', ?, ?, ?, ?, ?)", (event_id, etype, workspace_id, raw[:20000], now()))
    row = query("SELECT * FROM webhook_events WHERE provider = 'resend' AND event_id = ?", (event_id,), one=True)
    if row["processed_at"]:
        return jsonify({"ok": True, "duplicate": True})
    # Either new, or stored earlier but never processed (e.g. a crash): apply it now.
    outcome = apply_event(workspace_id, etype, data)
    execute("UPDATE webhook_events SET processed_at = ?, outcome = ? WHERE id = ?", (now(), outcome[:200], row["id"]))
    return jsonify({"ok": True, "outcome": outcome})


@bp.route("/resend/system", methods=["POST"])
def resend_system():
    return _handle(None, current_app.config["RESEND_WEBHOOK_SECRET"] or get_secret(0, "resend_webhook_secret"))


@bp.route("/resend/<public_id>", methods=["POST"])
def resend_workspace(public_id):
    ws = query("SELECT id FROM workspaces WHERE public_id = ?", (public_id,), one=True)
    if not ws:
        abort(404)
    return _handle(ws["id"], get_secret(ws["id"], "resend_webhook_secret"))
