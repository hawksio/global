"""Shared test fixtures: a fresh app and database per test, fake email provider, DNS and web."""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery-staple"


class FakeProvider:
    """Stands in for Resend. Records payloads; results can be scripted per call."""
    name = "resend"

    def __init__(self):
        self.sent = []
        self.script = []   # list of SendResult to return in order; then success

    def send(self, payload, key):
        from gm.mailer import SendResult
        self.sent.append((payload, key))
        if self.script:
            return self.script.pop(0)
        return SendResult(ok=True, provider_id=f"re-{len(self.sent)}")

    def test_connection(self):
        return True, "ok"

    def domains(self):
        return [{"name": "sender.test", "status": "verified"}]


def fake_dns(records):
    """records: {(domain, rdtype): list | Exception class}."""
    import dns.resolver

    def resolve(domain, rdtype):
        value = records.get((domain, rdtype))
        if value is None:
            raise dns.resolver.NoAnswer()
        if isinstance(value, type) and issubclass(value, Exception):
            raise value()
        return value
    return resolve


DEFAULT_DNS = {("good.test", "MX"): ["10 mx.good.test."], ("sender.test", "MX"): ["10 mx.sender.test."],
               ("biz.test", "MX"): ["10 mx.biz.test."], ("other.test", "MX"): ["10 mx.other.test."],
               ("mailinator.com", "MX"): ["10 mail.mailinator.com."]}


class AppCase(unittest.TestCase):
    def setUp(self):
        import dns.resolver
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["GM_INSTANCE_DIR"] = self.tmp.name
        os.environ["GM_DEV_ADMIN"] = "0"
        from gm import create_app
        self.provider = FakeProvider()
        self.outbox = []
        self.dns = dict(DEFAULT_DNS)
        self.dns[("nowhere.test", "MX")] = dns.resolver.NXDOMAIN
        self.dns[("nowhere.test", "A")] = dns.resolver.NXDOMAIN
        self.app = create_app({"TESTING": True, "MAIL_CAPTURE": self.outbox, "AI_PROVIDER": "rules",
                               "DNS_RESOLVER": lambda d, t: fake_dns(self.dns)(d, t),
                               "MAIL_PROVIDER_FACTORY": self._factory, "BASE_URL": "https://gm.test"})
        self.use_fake_provider = False
        self.ctx = self.app.app_context()
        self.ctx.push()
        from werkzeug.security import generate_password_hash
        from gm.auth import create_workspace
        from gm.db import execute
        self.ws_id, self.owner_id = create_workspace("Acme", "owner@acme.test", "Olive Owner", generate_password_hash(PASSWORD),
                                                     verified=True)
        execute("UPDATE workspaces SET company_address = '1 Test Street, Testville', sender_email = 'hello@sender.test', "
                "sender_name = 'Acme' WHERE id = ?", (self.ws_id,))
        self.client = self.app.test_client()

    def tearDown(self):
        self.ctx.pop()
        os.environ.pop("GM_INSTANCE_DIR", None)
        self.tmp.cleanup()

    def _factory(self, category, ws):
        from gm.mailer import SandboxProvider
        if self.use_fake_provider and category == "marketing":
            return self.provider
        return SandboxProvider()

    # ---------------------------------------------------------------- helpers
    def add_user(self, email, role, workspace_id=None):
        from werkzeug.security import generate_password_hash
        from gm.db import execute, now
        return execute("INSERT INTO users (workspace_id, email, password_hash, full_name, role, email_verified_at, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (workspace_id or self.ws_id, email, generate_password_hash(PASSWORD),
                                                        email.split("@")[0], role, now(), now()))

    def token(self, client=None):
        client = client or self.client
        r = client.get("/login")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True))
        return m.group(1)

    def login(self, email="owner@acme.test", password=PASSWORD, client=None):
        client = client or self.client
        r = client.post("/login", data={"csrf_token": self.token(client), "email": email, "password": password})
        return r

    def post(self, url, data=None, client=None, **kw):
        client = client or self.client
        page = client.get("/account/security")
        m = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        tok = m.group(1) if m else self.token(client)
        payload = dict(data or {})
        payload["csrf_token"] = tok
        return client.post(url, data=payload, **kw)

    def contact(self, email, **fields):
        from gm.contacts import create_contact
        from gm import validation
        meta = {"recipient_type": fields.pop("recipient_type", "business"),
                "lawful_basis": fields.pop("lawful_basis", "legitimate_interest"), "purpose": "test"}
        cid, _ = create_contact(self.ws_id, email, fields, "manual", **meta)
        validation.store_on_contact(cid, validation.check(email, self.ws_id, dns=True))
        return cid

    def run_jobs(self, n=20):
        from gm.jobs import run_pending
        return run_pending(time_budget=60, max_jobs=n)

    def campaign(self, name="Test campaign", dry_run=0, steps=1, segment=None):
        import json
        from gm.db import execute, now
        seg = json.dumps(segment or {"verification_status": ["validated"], "status": ["active"]})
        cid = execute("INSERT INTO campaigns (workspace_id, name, status, segment, from_name, from_email, timezone, send_window_start, "
                      "send_window_end, send_days, rate_per_minute, dry_run, created_by, created_at, updated_at) "
                      "VALUES (?, ?, 'approved', ?, 'Acme', 'hello@sender.test', 'UTC', 0, 24, '0,1,2,3,4,5,6', 60, ?, ?, ?, ?)",
                      (self.ws_id, name, seg, dry_run, self.owner_id, now(), now()))
        for n in range(steps):
            execute("INSERT INTO campaign_steps (campaign_id, step_no, delay_days, subject, html_body) VALUES (?, ?, ?, ?, ?)",
                    (cid, n, 0 if n == 0 else 2, f"Hello {{{{first_name|there}}}} step {n}", f"<p>Hi {{{{first_name}}}}, step {n}</p>"))
        return cid
