"""Compose and Sent: the Gmail-like way of sending."""
from helpers import AppCase


def live(case):
    from gm.db import execute
    execute("UPDATE workspaces SET sandbox_mode = 0 WHERE id = ?", (case.ws_id,))
    case.use_fake_provider = True


class Compose(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def send(self, **extra):
        data = {"to_mode": "all", "from_name": "Olive", "subject": "Quick question", "action": "send",
                "body": "Hi {{first_name|there}},\n\nAre you the right person to talk to about visas?\n\nThanks,\nOlive"}
        data.update(extra)
        return self.post("/compose", data)

    def test_compose_page_and_home_is_sent(self):
        self.assertIn("New email", self.client.get("/compose").get_data(as_text=True))
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("Nothing sent yet", home)

    def test_send_to_pasted_addresses_practice_mode(self):
        from gm.db import query
        r = self.send(to_mode="paste", pasted="ann@good.test, bob@good.test", permission="on")
        self.assertEqual(r.status_code, 302)
        self.run_jobs()
        msgs = query("SELECT * FROM messages WHERE category = 'marketing'")
        self.assertEqual(sorted(m["to_email"] for m in msgs), ["ann@good.test", "bob@good.test"])
        self.assertTrue(all(m["status"] == "simulated" for m in msgs), "practice mode never really sends")
        c = query("SELECT * FROM contacts WHERE email_normalized = 'ann@good.test'", one=True)
        self.assertEqual((c["lawful_basis"], c["recipient_type"]), ("legitimate_interest", "business"))
        page = self.client.get("/sent").get_data(as_text=True)
        self.assertIn("Quick question", page)

    def test_pasted_addresses_need_permission_tick(self):
        from gm.db import scalar
        r = self.send(to_mode="paste", pasted="ann@good.test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("genuine reason", r.get_data(as_text=True))
        self.assertEqual(scalar("SELECT COUNT(*) FROM campaigns"), 0)

    def test_real_send_is_plain_personal_style(self):
        live(self)
        self.contact("ann@good.test", first_name="Ann")
        r = self.send()
        self.assertEqual(r.status_code, 302)
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 1)
        payload, key = self.provider.sent[0]
        self.assertEqual(payload["from"], "Olive <hello@sender.test>")
        self.assertIn("Hi Ann,", payload["text"])
        self.assertNotIn("<table", payload["html"], "plain style: no newsletter layout")
        self.assertNotIn("<img", payload["html"])
        self.assertIn("Unsubscribe", payload["html"])
        self.assertIn("1 Test Street", payload["text"])
        self.assertEqual(payload["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_problems_are_explained_and_nothing_is_created(self):
        from gm.db import execute, scalar
        live(self)
        self.contact("ann@good.test")
        execute("UPDATE workspaces SET company_address = ''")
        r = self.send()
        self.assertEqual(r.status_code, 400)
        self.assertIn("postal address", r.get_data(as_text=True))
        self.assertEqual(scalar("SELECT COUNT(*) FROM campaigns"), 0)
        self.assertEqual(self.provider.sent, [])

    def test_suppressed_and_unsubscribed_are_skipped(self):
        from gm.suppression import add_suppression
        live(self)
        self.contact("ann@good.test")
        self.contact("gone@good.test")
        add_suppression(self.ws_id, "gone@good.test", "unsubscribe")
        self.send()
        self.run_jobs()
        self.assertEqual([p["to"] for p, _ in self.provider.sent], [["ann@good.test"]])

    def test_follow_up_and_test_email(self):
        from gm.db import query
        self.contact("ann@good.test")
        n = len(self.outbox)
        self.post("/compose", {"to_mode": "all", "subject": "Hello", "body": "Hi there", "action": "test", "from_name": "Olive"})
        self.assertEqual(len(self.outbox), n + 1)
        self.assertTrue(self.outbox[-1]["subject"].startswith("[TEST]"))
        self.assertEqual(self.outbox[-1]["to"], ["owner@acme.test"])
        self.send(follow_up="on", follow_days="4", follow_body="Hi {{first_name|there}}, just checking in.")
        steps = query("SELECT * FROM campaign_steps ORDER BY step_no")
        self.assertEqual([(s["step_no"], s["delay_days"]) for s in steps], [(0, 0), (1, 4)])

    def test_marketer_sends_for_approval(self):
        from gm.db import query
        self.add_user("m@acme.test", "marketer")
        m = self.app.test_client()
        self.login("m@acme.test", client=m)
        self.contact("ann@good.test")
        self.post("/compose", {"to_mode": "all", "subject": "Hello", "body": "Hi", "action": "send", "from_name": "M"}, client=m)
        self.assertEqual(query("SELECT status FROM campaigns", one=True)["status"], "pending_approval")
        cid = query("SELECT id FROM campaigns", one=True)["id"]
        self.post(f"/sent/{cid}/approve")
        self.assertEqual(query("SELECT status FROM campaigns", one=True)["status"], "scheduled")

    def test_stop_from_sent_page(self):
        from gm.db import query
        live(self)
        self.contact("ann@good.test")
        self.send()
        cid = query("SELECT id FROM campaigns", one=True)["id"]
        self.assertIn("Stop", self.client.get(f"/sent/{cid}").get_data(as_text=True))
        r = self.post(f"/sent/{cid}/cancel")
        self.assertEqual(r.headers["Location"], f"/sent/{cid}")
        self.run_jobs()
        self.assertEqual(self.provider.sent, [])
