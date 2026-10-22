"""Templates, campaign workflow and engine, limits, retries, duplicate-send prevention, follow-ups, webhooks,
unsubscribe, AI assistant fallbacks and the Resend adapter."""
import base64
import hashlib
import hmac
import json
import time

from helpers import AppCase


def live(case):
    """Real-sending mode against the fake Resend provider."""
    from gm.db import execute
    execute("UPDATE workspaces SET sandbox_mode = 0 WHERE id = ?", (case.ws_id,))
    case.use_fake_provider = True


class Rendering(AppCase):
    def test_escaping_fallbacks_footer_and_headers(self):
        from gm import render
        from gm.security import workspace
        from gm.db import query
        ws = query("SELECT * FROM workspaces WHERE id = ?", (self.ws_id,), one=True)
        contact = {"email": "x@good.test", "email_normalized": "x@good.test", "first_name": "<b>Eve</b>", "company": ""}
        subj, html, text, headers = render.build_marketing(ws, contact, "Hi {{first_name}} at {{company|your company}}",
                                                           "<p>Hi {{first_name}}</p><script>alert(1)</script>"
                                                           "<a href=\"javascript:alert(1)\" onclick=\"x()\">x</a>", "")
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("onclick", html)
        self.assertEqual(subj, "Hi <b>Eve</b> at your company")
        self.assertIn("1 Test Street", html)
        self.assertIn("Unsubscribe", text)
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(headers["List-Unsubscribe"].startswith("<https://gm.test/u/"))
        self.assertTrue(workspace is not None)

    def test_unknown_fields_rejected(self):
        self.login()
        r = self.post("/templates/new", {"name": "T", "subject": "Hi {{firstname}}", "html_body": "<p>x</p>", "category": "General"})
        self.assertEqual(r.status_code, 400)
        r = self.post("/templates/new", {"name": "T", "subject": "Hi {{first_name}}", "html_body": "<p>x</p>", "category": "General"})
        self.assertEqual(r.status_code, 302)

    def test_template_versions_and_approval_reset(self):
        self.login()
        self.post("/templates/new", {"name": "T", "subject": "One", "html_body": "<p>v1</p>", "category": "General"})
        from gm.db import query, scalar
        tid = scalar("SELECT id FROM templates")
        self.post(f"/templates/{tid}/approve")
        self.assertIsNotNone(query("SELECT approved_at FROM templates WHERE id = ?", (tid,), one=True)["approved_at"])
        self.post(f"/templates/{tid}", {"name": "T", "subject": "Two", "html_body": "<p>v2</p>", "category": "General"})
        t = query("SELECT * FROM templates WHERE id = ?", (tid,), one=True)
        self.assertEqual(t["version"], 2)
        self.assertIsNone(t["approved_at"])
        self.post(f"/templates/{tid}/restore/1")
        self.assertEqual(query("SELECT subject FROM templates WHERE id = ?", (tid,), one=True)["subject"], "One")

    def test_test_send_only_to_team(self):
        self.login()
        self.post("/templates/new", {"name": "T", "subject": "S", "html_body": "<p>x</p>", "category": "General"})
        from gm.db import scalar
        tid = scalar("SELECT id FROM templates")
        n = len(self.outbox)
        self.post(f"/templates/{tid}/test", {"to": "stranger@good.test"})
        self.assertEqual(len(self.outbox), n)
        self.post(f"/templates/{tid}/test", {"to": "owner@acme.test"})
        self.assertEqual(len(self.outbox), n + 1)
        self.assertTrue(self.outbox[-1]["subject"].startswith("[TEST]"))


class Workflow(AppCase):
    def test_full_ui_flow_simulation(self):
        self.contact("a@good.test", first_name="Ann")
        self.login()
        self.post("/templates/new", {"name": "Intro", "subject": "Hello {{first_name|there}}", "html_body": "<p>Hi</p>",
                                     "category": "General"})
        from gm.db import query, scalar
        tid = scalar("SELECT id FROM templates")
        r = self.post("/campaigns/new", {"name": "C1", "template_id": str(tid), "timezone": "UTC", "send_window_start": "0",
                                         "send_window_end": "24", "send_days": list("0123456"), "rate_per_minute": "10",
                                         "dry_run": "on", "seg_verification": "validated"})
        cid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(self.client.get(f"/campaigns/{cid}/dry-run").status_code, 200)
        self.post(f"/campaigns/{cid}/submit")
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "pending_approval")
        self.post(f"/campaigns/{cid}/approve")
        self.post(f"/campaigns/{cid}/launch")
        self.run_jobs()
        m = query("SELECT * FROM messages WHERE campaign_id = ?", (cid,), one=True)
        self.assertEqual(m["status"], "simulated")
        self.assertEqual(m["simulated"], 1)
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "completed")
        self.assertIn("SIMULATED", self.client.get(f"/campaigns/{cid}").get_data(as_text=True))

    def test_real_send_requires_test_email_and_domain(self):
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        from gm.campaigns import readiness
        from gm.db import execute, query
        execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (cid,))
        ws = query("SELECT * FROM workspaces WHERE id = ?", (self.ws_id,), one=True)
        c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
        blocking = [i["label"] for i in readiness(ws, c)[0] if i["blocking"]]
        self.assertTrue(any("Test email" in b for b in blocking))
        self.assertTrue(any("domain" in b for b in blocking))

    def test_editing_after_approval_returns_to_draft(self):
        self.contact("a@good.test")
        cid = self.campaign()
        self.login()
        from gm.db import query
        self.post(f"/campaigns/{cid}/steps", {"action": "save", "step_no": "0", "subject": "Changed", "html_body": "<p>new</p>"})
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "draft")

    def test_four_eyes(self):
        from gm.db import execute, query
        execute("UPDATE workspaces SET four_eyes_approval = 1")
        cid = self.campaign()
        execute("UPDATE campaigns SET status = 'pending_approval' WHERE id = ?", (cid,))
        self.contact("a@good.test")
        self.login()
        self.post(f"/campaigns/{cid}/approve")
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "pending_approval")


class Engine(AppCase):
    def launch(self, cid):
        from gm.campaigns import launch_campaign
        from gm.db import now, query
        c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
        ws = query("SELECT * FROM workspaces WHERE id = ?", (self.ws_id,), one=True)
        return launch_campaign(c, ws, now())

    def test_real_send_payload_and_no_duplicates(self):
        live(self)
        self.contact("a@good.test", first_name="Ann")
        self.contact("b@good.test")
        cid = self.campaign()
        self.assertEqual(self.launch(cid), 2)
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 2)
        payload, key = self.provider.sent[0]
        self.assertEqual(payload["from"], "Acme <hello@sender.test>")
        self.assertIn("List-Unsubscribe", payload["headers"])
        self.assertIn("text", payload)
        self.assertTrue(key.startswith(f"camp-{cid}-rcp-"))
        from gm.db import query
        statuses = {m["status"] for m in query("SELECT status FROM messages WHERE campaign_id = ?", (cid,))}
        self.assertEqual(statuses, {"accepted"}, "accepted, not 'delivered', until a webhook says so")
        # Running the engine again (or relaunching) never sends the same step twice.
        self.launch(cid)
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 2)

    def test_suppression_checked_immediately_before_send(self):
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        from gm.suppression import add_suppression
        add_suppression(self.ws_id, "a@good.test", "unsubscribe")
        self.run_jobs()
        self.assertEqual(self.provider.sent, [])

    def test_ineligible_contacts_excluded(self):
        live(self)
        self.contact("ok@good.test")
        self.contact("nobasis@good.test", lawful_basis="unknown")
        self.contact("bad@nowhere.test")
        self.contact("temp@mailinator.com")
        cid = self.campaign(segment={"verification_status": ["validated", "invalid", "risky"], "status": ["active"]})
        self.assertEqual(self.launch(cid), 1)

    def test_transient_failure_retried_with_same_key(self):
        from gm.db import execute, query
        from gm.mailer import SendResult
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.provider.script = [SendResult(ok=False, error="timeout", transient=True)]
        self.run_jobs()
        m = query("SELECT * FROM messages WHERE campaign_id = ?", (cid,), one=True)
        self.assertEqual((m["status"], m["attempts"]), ("queued", 1))
        execute("UPDATE messages SET next_attempt_at = datetime('now', '-1 minute')")
        self.run_jobs()
        m = query("SELECT * FROM messages WHERE campaign_id = ?", (cid,), one=True)
        self.assertEqual((m["status"], m["attempts"]), ("accepted", 2))
        self.assertEqual(self.provider.sent[0][1], self.provider.sent[1][1], "same idempotency key on retry")
        self.assertEqual(len(self.provider.sent), 2)

    def test_retries_are_bounded(self):
        from gm.db import execute, query
        from gm.mailer import SendResult
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.provider.script = [SendResult(ok=False, error="503", transient=True) for _ in range(10)]
        for _ in range(6):
            self.run_jobs()
            execute("UPDATE messages SET next_attempt_at = datetime('now', '-1 minute') WHERE status = 'queued'")
        m = query("SELECT * FROM messages WHERE campaign_id = ?", (cid,), one=True)
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m["attempts"], 4)

    def test_permanent_failure_not_retried(self):
        from gm.db import query
        from gm.mailer import SendResult
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.provider.script = [SendResult(ok=False, error="422 invalid recipient")]
        self.run_jobs()
        self.run_jobs()
        self.assertEqual(query("SELECT status FROM messages WHERE campaign_id = ?", (cid,), one=True)["status"], "failed")
        self.assertEqual(len(self.provider.sent), 1)

    def test_auth_error_pauses_workspace(self):
        from gm.db import query
        from gm.mailer import SendResult
        live(self)
        self.contact("a@good.test")
        self.contact("b@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.provider.script = [SendResult(ok=False, error="API key is invalid", auth_error=True)]
        self.run_jobs()
        ws = query("SELECT sending_paused_reason FROM workspaces WHERE id = ?", (self.ws_id,), one=True)
        self.assertIn("API key", ws["sending_paused_reason"])
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "paused")
        self.assertEqual(len(self.provider.sent), 1)

    def test_sending_limits(self):
        from gm.db import execute
        live(self)
        for i in range(5):
            self.contact(f"p{i}@good.test")
        execute("UPDATE workspaces SET hourly_send_limit = 3, daily_send_limit = 100")
        cid = self.campaign()
        self.launch(cid)
        self.run_jobs()
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 3)

    def test_frequency_limit_defers(self):
        from gm.db import execute
        live(self)
        cid_contact = self.contact("a@good.test")
        execute("UPDATE contacts SET last_contacted_at = datetime('now', '-1 hour') WHERE id = ?", (cid_contact,))
        cid = self.campaign()
        self.launch(cid)
        self.run_jobs()
        self.assertEqual(self.provider.sent, [])

    def test_send_window(self):
        from datetime import datetime, timezone
        from gm.campaigns import in_window
        c = {"send_days": "0", "send_window_start": 9, "send_window_end": 17, "timezone": "UTC"}
        monday_10 = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
        self.assertTrue(in_window(c, "UTC", monday_10))
        self.assertFalse(in_window(c, "UTC", datetime(2026, 10, 19, 18, tzinfo=timezone.utc)))
        self.assertFalse(in_window(c, "Asia/Tokyo", monday_10), "19:00 in Tokyo is outside 9-17")
        self.assertFalse(in_window(c, "UTC", datetime(2026, 10, 20, 10, tzinfo=timezone.utc)), "Tuesday not allowed")

    def test_cancel_and_pause(self):
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.login()
        self.post(f"/campaigns/{cid}/cancel")
        self.run_jobs()
        self.assertEqual(self.provider.sent, [])
        from gm.db import query
        self.assertEqual(query("SELECT status FROM campaign_recipients WHERE campaign_id = ?", (cid,), one=True)["status"], "stopped")

    def test_follow_ups_and_reply_stop(self):
        from gm.db import execute, query
        live(self)
        self.contact("a@good.test")
        self.contact("b@good.test")
        cid = self.campaign(steps=2)
        execute("UPDATE workspaces SET min_hours_between_emails = 0")
        self.launch(cid)
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 2)
        r = query("SELECT * FROM campaign_recipients WHERE campaign_id = ? AND email = 'a@good.test'", (cid,), one=True)
        self.assertEqual(r["next_step"], 1)
        from gm.campaigns import record_reply
        record_reply(r, "test")
        execute("UPDATE campaign_recipients SET next_send_at = datetime('now', '-1 minute') WHERE status = 'active'")
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 3)
        self.assertEqual(self.provider.sent[-1][0]["to"], ["b@good.test"])
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "completed")

    def test_bounce_threshold_pauses(self):
        from gm.db import execute, now
        live(self)
        for i in range(60):
            execute("INSERT INTO messages (workspace_id, category, to_email, subject, status, idempotency_key, created_at) "
                    "VALUES (?, 'marketing', ?, 's', ?, ?, ?)", (self.ws_id, f"x{i}@good.test",
                                                                  "bounced" if i < 6 else "delivered", f"k{i}", now()))
        self.contact("a@good.test")
        cid = self.campaign()
        self.launch(cid)
        self.run_jobs()
        from gm.db import query
        self.assertIn("bounce", query("SELECT sending_paused_reason FROM workspaces", one=True)["sending_paused_reason"])
        self.assertEqual(self.provider.sent, [])


def svix_headers(secret, body, msg_id="msg_1", ts=None):
    ts = str(int(ts or time.time()))
    key = base64.b64decode(secret[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.{body}".encode(), hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "Content-Type": "application/json"}


class Webhooks(AppCase):
    SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

    def setUp(self):
        super().setUp()
        from gm.security import set_secret
        from gm.db import query
        set_secret(self.ws_id, "resend_webhook_secret", self.SECRET)
        self.url = "/webhooks/resend/" + query("SELECT public_id FROM workspaces WHERE id = ?", (self.ws_id,), one=True)["public_id"]
        live(self)
        self.cid = self.contact("a@good.test")
        self.camp = self.campaign(steps=2)
        Engine.launch(self, self.camp)
        self.run_jobs()
        self.provider_id = self.provider.sent[0][0] and "re-1"

    def send_event(self, etype, data, msg_id="msg_1", secret=None, ts=None):
        body = json.dumps({"type": etype, "created_at": "2026-10-21T00:00:00Z", "data": data})
        return self.client.post(self.url, data=body, headers=svix_headers(secret or self.SECRET, body, msg_id, ts))

    def test_signature_required(self):
        body = json.dumps({"type": "email.delivered", "data": {"email_id": "re-1"}})
        self.assertEqual(self.client.post(self.url, data=body, headers={"Content-Type": "application/json"}).status_code, 401)
        wrong = "whsec_" + base64.b64encode(b"x" * 32).decode()
        self.assertEqual(self.send_event("email.delivered", {"email_id": "re-1"}, secret=wrong).status_code, 401)
        self.assertEqual(self.send_event("email.delivered", {"email_id": "re-1"}, ts=time.time() - 3600).status_code, 401,
                         "stale timestamps are rejected (replay protection)")

    def test_delivered_then_duplicate_event(self):
        from gm.db import query, scalar
        r = self.send_event("email.delivered", {"email_id": "re-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(query("SELECT status FROM messages WHERE provider_id = 're-1'", one=True)["status"], "delivered")
        r = self.send_event("email.delivered", {"email_id": "re-1"})
        self.assertTrue(r.get_json()["duplicate"])
        self.assertEqual(scalar("SELECT COUNT(*) FROM contact_events WHERE kind = 'email_delivered'"), 1)

    def test_hard_bounce_suppresses_and_stops_follow_ups(self):
        from gm.db import query
        self.send_event("email.bounced", {"email_id": "re-1", "bounce": {"type": "Permanent", "subType": "General",
                                                                          "message": "No such user"}})
        self.assertEqual(query("SELECT reason FROM suppressions", one=True)["reason"], "hard_bounce")
        self.assertEqual(query("SELECT status FROM contacts WHERE id = ?", (self.cid,), one=True)["status"], "bounced")
        self.assertEqual(query("SELECT status FROM campaign_recipients", one=True)["status"], "stopped")
        # A late "delivered" event can't overwrite the bounce.
        self.send_event("email.delivered", {"email_id": "re-1"}, msg_id="msg_2")
        self.assertEqual(query("SELECT status FROM messages WHERE provider_id = 're-1'", one=True)["status"], "bounced")

    def test_soft_bounce_does_not_suppress(self):
        from gm.db import scalar
        self.send_event("email.bounced", {"email_id": "re-1", "bounce": {"type": "Temporary", "subType": "MailboxFull",
                                                                          "message": "full"}})
        self.assertEqual(scalar("SELECT COUNT(*) FROM suppressions"), 0)

    def test_complaint_suppresses(self):
        from gm.db import query
        self.send_event("email.complained", {"email_id": "re-1"})
        self.assertEqual(query("SELECT reason FROM suppressions", one=True)["reason"], "complaint")

    def test_inbound_reply_stops_sequence(self):
        from gm.db import query
        self.send_event("email.received", {"from": "Ann <A@good.test>", "to": ["hello@sender.test"], "subject": "Re: hi"})
        r = query("SELECT * FROM campaign_recipients", one=True)
        self.assertIsNotNone(r["replied_at"])
        self.assertEqual(r["status"], "stopped")

    def test_unknown_message_ignored(self):
        r = self.send_event("email.delivered", {"email_id": "nope"})
        self.assertIn("ignored", r.get_json()["outcome"])


class Unsubscribe(AppCase):
    def test_get_confirms_post_unsubscribes_and_one_click(self):
        from gm.render import unsubscribe_token
        from gm.db import query
        cid = self.contact("a@good.test")
        camp = self.campaign()
        Engine.launch(self, camp)
        token = unsubscribe_token(self.ws_id, "a@good.test")
        r = self.client.get(f"/u/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(query("SELECT 1 FROM suppressions", one=True), "GET must not unsubscribe (link scanners)")
        r = self.client.post(f"/u/{token}", data={"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(query("SELECT reason FROM suppressions", one=True)["reason"], "unsubscribe")
        self.assertEqual(query("SELECT status FROM contacts WHERE id = ?", (cid,), one=True)["status"], "unsubscribed")
        self.assertEqual(query("SELECT status FROM campaign_recipients", one=True)["status"], "stopped")

    def test_tampered_token(self):
        from gm.render import unsubscribe_token
        token = unsubscribe_token(self.ws_id, "a@good.test")
        self.assertEqual(self.client.post(f"/u/{token[:-2]}xx").status_code, 400)


class Assistant(AppCase):
    def test_rule_based_draft_and_save(self):
        self.login()
        r = self.post("/assistant/", {"task": "draft", "offer": "courier service", "audience": "shops", "benefit": "",
                                      "goal": "a call", "tone": "professional"})
        page = r.get_data(as_text=True)
        self.assertIn("rule-based", page)
        self.assertIn("We help shops with courier service.", page)
        draft = page[page.index('id="d-body"'):page.index("</textarea>", page.index('id="d-body"'))]
        self.assertNotIn("[", draft, "drafts are complete: no [placeholders] to fill in")
        self.post("/assistant/save", {"subject": "Hi {{company}}", "body": "Hi {{first_name|there}},\n\nText.", "name": "AI1"})
        from gm.db import query
        t = query("SELECT * FROM templates WHERE name = 'AI1'", one=True)
        self.assertEqual((t["ai_generated"], t["approved_at"]), (1, None))

    def test_provider_failure_falls_back(self):
        from gm.ai import AIError
        class Broken:
            name = "anthropic"
            model = "claude-opus-5-5"
            def complete(self, prompt, max_tokens=0):
                raise AIError("The AI provider rejected the API key.")
        self.app.config["AI_PROVIDER_OVERRIDE"] = Broken()
        self.login()
        r = self.post("/assistant/", {"task": "subjects", "offer": "packaging"})
        page = r.get_data(as_text=True)
        self.assertIn("rejected the API key", page)
        self.assertIn("rule-based", page)
        from gm.db import query
        self.assertEqual(query("SELECT ok FROM ai_usage WHERE provider = 'anthropic'", one=True)["ok"], 0)

    def test_usage_limit(self):
        from gm.ai import AIResult
        from gm.db import execute
        class Counting:
            name = "anthropic"
            model = "claude-opus-5-5"
            calls = 0
            def complete(self, prompt, max_tokens=0):
                Counting.calls += 1
                return AIResult(text="Line one\nLine two", provider="anthropic", model="claude-opus-5-5", input_tokens=10,
                                output_tokens=5)
        self.app.config["AI_PROVIDER_OVERRIDE"] = Counting()
        execute("UPDATE workspaces SET ai_monthly_limit = 1")
        self.login()
        self.post("/assistant/", {"task": "subjects", "offer": "x"})
        r = self.post("/assistant/", {"task": "subjects", "offer": "x"})
        self.assertEqual(Counting.calls, 1)
        self.assertIn("Monthly AI limit", r.get_data(as_text=True))

    def test_content_check(self):
        from gm.ai import content_check
        found = {f["title"] for f in content_check("RE: URGENT ACT NOW!!!", '<p>Guaranteed results [benefit]</p>'
                                                   '<a href="http://bit.ly/x">www.mybank.com</a>')}
        self.assertTrue(any("Misleading subject" in t for t in found))
        self.assertTrue(any("square brackets" in t for t in found))
        brackets = [f for f in content_check("Hi", "<p>Hello [note]</p>") if "square brackets" in f["title"]]
        self.assertEqual(brackets[0]["level"], "warn", "bracketed text is a hint, never a blocking error")
        self.assertTrue(any("Link shortener" in t for t in found))
        self.assertTrue(any("doesn't match" in t for t in found))
        self.assertTrue(any("evidence" in t for t in found))

    def test_proofread(self):
        from gm.ai import proofread_text
        text, changes = proofread_text("i recieve  the the email . thanks")
        self.assertEqual(text, "I receive the email. Thanks")
        self.assertTrue(changes)


class ResendAdapter(AppCase):
    def test_error_mapping_and_key_never_echoed(self):
        import resend
        from resend import exceptions as rex
        from gm.mailer import ResendProvider
        p = ResendProvider("re_supersecretkey_123456")
        orig = resend.Emails.send
        try:
            def raise_(exc):
                def f(*a, **k):
                    raise exc
                return f
            resend.Emails.send = raise_(rex.InvalidApiKeyError("API key re_supersecretkey_123456 is invalid", "invalid_api_key", 403))
            r = p.send({"to": ["a@b.test"]}, "k")
            self.assertTrue(r.auth_error and not r.ok)
            self.assertNotIn("supersecret", r.error)
            resend.Emails.send = raise_(rex.RateLimitError("slow down", "rate_limit_exceeded", 429))
            self.assertTrue(p.send({}, "k").transient)
            resend.Emails.send = raise_(rex.ValidationError("bad to", "validation_error", 422))
            r = p.send({}, "k")
            self.assertFalse(r.transient or r.ok)
            import requests
            resend.Emails.send = raise_(requests.Timeout())
            self.assertTrue(p.send({}, "k").transient)
            resend.Emails.send = lambda *a, **k: {"unexpected": True}
            r = p.send({}, "k")
            self.assertFalse(r.ok)
            seen = {}
            def ok(params, options=None):
                seen["options"] = options
                return {"id": "abc"}
            resend.Emails.send = ok
            r = p.send({}, "idem-1")
            self.assertEqual((r.ok, r.provider_id), (True, "abc"))
            self.assertEqual(seen["options"], {"idempotency_key": "idem-1"})
            self.assertIsNone(resend.api_key, "the module-level key is cleared after each call")
        finally:
            resend.Emails.send = orig


class SendNow(AppCase):
    def test_send_now_practice_run(self):
        from gm.db import execute, query
        self.contact("a@good.test")
        cid = self.campaign(dry_run=1)
        execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (cid,))
        self.login()
        r = self.post(f"/campaigns/{cid}/send-now")
        self.assertEqual(r.status_code, 302)
        c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
        self.assertEqual(c["status"], "scheduled")
        self.assertEqual(c["approved_by"], self.owner_id, "clicking Send now is recorded as the approval")
        self.run_jobs()
        self.assertEqual(query("SELECT status FROM messages WHERE campaign_id = ?", (cid,), one=True)["status"], "simulated")

    def test_send_now_real_still_requires_safety_checks(self):
        from gm.db import execute, now, query
        live(self)
        self.contact("a@good.test")
        cid = self.campaign()
        execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (cid,))
        self.login()
        self.post(f"/campaigns/{cid}/send-now")
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "draft",
                         "blocked: no test email and no domain check yet")
        execute("UPDATE campaigns SET test_sent_at = ? WHERE id = ?", (now(), cid))
        execute("INSERT INTO dns_checks (workspace_id, domain, results, score, checked_at) VALUES (?, 'sender.test', ?, 90, ?)",
                (self.ws_id, '{"dkim": {"status": "pass"}}', now()))
        self.post(f"/campaigns/{cid}/send-now")
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 1)
        self.assertEqual(query("SELECT status FROM messages WHERE campaign_id = ?", (cid,), one=True)["status"], "accepted")

    def test_marketer_cannot_send_now_and_four_eyes_respected(self):
        from gm.db import execute, query
        self.contact("a@good.test")
        cid = self.campaign(dry_run=1)
        execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (cid,))
        self.add_user("m@acme.test", "marketer")
        m = self.app.test_client()
        self.login("m@acme.test", client=m)
        self.assertEqual(self.post(f"/campaigns/{cid}/send-now", client=m).status_code, 403)
        execute("UPDATE workspaces SET four_eyes_approval = 1")
        self.login()
        self.post(f"/campaigns/{cid}/send-now")
        self.assertEqual(query("SELECT status FROM campaigns WHERE id = ?", (cid,), one=True)["status"], "draft")

    def test_send_later(self):
        from gm.db import execute, query
        self.contact("a@good.test")
        cid = self.campaign(dry_run=1)
        execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (cid,))
        self.login()
        self.post(f"/campaigns/{cid}/send-later", {"scheduled_at": "2099-01-01T09:00"})
        c = query("SELECT * FROM campaigns WHERE id = ?", (cid,), one=True)
        self.assertEqual((c["status"], c["scheduled_at"]), ("scheduled", "2099-01-01 09:00:00"))
        self.run_jobs()
        self.assertIsNone(query("SELECT 1 FROM messages WHERE campaign_id = ?", (cid,), one=True), "nothing before the start time")


class Samples(AppCase):
    def test_remove_samples_keeps_own_data(self):
        from gm.db import query, scalar
        from gm.sandbox import seed
        seed(self.ws_id, self.owner_id)
        mine = self.contact("real@good.test")
        own_campaign = self.campaign(name="My campaign")
        self.login()
        self.assertIn("Remove practice samples", self.client.get("/settings/").get_data(as_text=True))
        self.post("/settings/", {"section": "remove_samples"})
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts WHERE sandbox = 1"), 0)
        self.assertEqual(scalar("SELECT COUNT(*) FROM templates WHERE name LIKE '[Sample]%'"), 0)
        self.assertEqual(scalar("SELECT COUNT(*) FROM campaigns WHERE name LIKE '[Sample]%'"), 0)
        self.assertIsNotNone(query("SELECT 1 FROM contacts WHERE id = ?", (mine,), one=True))
        self.assertIsNotNone(query("SELECT 1 FROM campaigns WHERE id = ?", (own_campaign,), one=True))
        self.assertNotIn("Remove practice samples", self.client.get("/settings/").get_data(as_text=True))
