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
        # Personal email, like one typed in Gmail: text only, no HTML, links or bulk-mail headers.
        self.assertNotIn("html", payload)
        self.assertNotIn("headers", payload)
        self.assertNotIn("http", payload["text"])
        # Still identifies the sender and offers an opt-out (by reply).
        self.assertIn("1 Test Street", payload["text"])
        self.assertIn("just reply and say so", payload["text"].lower())

    def test_asked_to_stop_and_promotions_hints(self):
        from gm.db import query
        live(self)
        self.contact("ann@good.test")
        self.send()
        cid = query("SELECT id FROM campaigns", one=True)["id"]
        rid = query("SELECT id FROM campaign_recipients", one=True)["id"]
        self.assertIn("Asked to stop", self.client.get(f"/sent/{cid}").get_data(as_text=True))
        self.post(f"/sent/{cid}/people/{rid}/stop")
        self.assertEqual(query("SELECT reason FROM suppressions", one=True)["reason"], "unsubscribe")
        self.assertEqual(query("SELECT status FROM contacts", one=True)["status"], "unsubscribed")
        page = self.post("/compose", {"to_mode": "all", "subject": "BIG SALE!!", "body": "Get 20% off, buy now",
                                      "from_name": "Olive", "action": "test"}).get_data(as_text=True)
        self.assertIn("Inbox check for this email", page)
        self.assertIn("Salesy words", page)

    def test_problems_are_explained_and_nothing_is_created(self):
        from gm.db import execute, scalar
        live(self)
        self.contact("ann@good.test")
        execute("UPDATE workspaces SET company_address = ''")
        r = self.send()
        self.assertEqual(r.status_code, 400)
        self.assertIn("business address", r.get_data(as_text=True))
        self.assertEqual(scalar("SELECT status FROM campaigns"), "draft", "your email is kept as a draft, not lost")
        self.assertEqual(self.provider.sent, [])
        # Filling the address in on the Compose page fixes it and sends the same draft.
        draft_id = scalar("SELECT id FROM campaigns")
        r = self.send(draft_id=str(draft_id), company_address="5 Real Road, Lagos")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(scalar("SELECT company_address FROM workspaces"), "5 Real Road, Lagos")
        self.assertEqual(scalar("SELECT COUNT(*) FROM campaigns"), 1)
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 1)

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

    def test_first_emails_go_immediately_at_the_chosen_speed(self):
        from gm.db import query
        live(self)
        for i in range(5):
            self.contact(f"p{i}@good.test")
        r = self.send(speed="2")
        self.assertEqual(len(self.provider.sent), 2, "the first minute's emails go out the moment you press Send")
        self.assertEqual(query("SELECT rate_per_minute FROM campaigns", one=True)["rate_per_minute"], 2)
        self.assertIn("2 sent straight away", self.client.get(r.headers["Location"]).get_data(as_text=True))
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 4, "then about 2 a minute; never the same person twice")
        self.assertEqual(len({p["to"][0] for p, _ in self.provider.sent}), 4)

    def test_stop_from_sent_page(self):
        from gm.db import query
        live(self)
        for i in range(4):
            self.contact(f"p{i}@good.test")
        self.send(speed="2")
        self.assertEqual(len(self.provider.sent), 2)
        cid = query("SELECT id FROM campaigns", one=True)["id"]
        self.assertIn("Stop", self.client.get(f"/sent/{cid}").get_data(as_text=True))
        r = self.post(f"/sent/{cid}/cancel")
        self.assertEqual(r.headers["Location"], f"/sent/{cid}")
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 2, "nothing more after Stop")


class EasierBasics(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_save_draft_and_continue(self):
        from gm.db import query
        r = self.post("/compose", {"to_mode": "paste", "pasted": "x@good.test", "subject": "Half written",
                                   "body": "Hi", "from_name": "Olive", "action": "save"})
        self.assertEqual(r.status_code, 302)
        d = query("SELECT * FROM campaigns", one=True)
        self.assertEqual((d["status"], d["style"]), ("draft", "plain"))
        self.assertIn("Half written", self.client.get("/sent").get_data(as_text=True))
        page = self.client.get(f"/compose?draft={d['id']}").get_data(as_text=True)
        self.assertIn('value="Half written"', page)
        self.assertIn("x@good.test", page)
        self.post(f"/compose/drafts/{d['id']}/delete")
        self.assertIsNone(query("SELECT 1 FROM campaigns", one=True))

    def test_new_contact_and_import_are_emailable_by_default(self):
        import io
        from gm.campaigns import eligibility
        from gm.db import query
        self.post("/contacts/new", {"email": "new@good.test", "recipient_type": "business",
                                    "lawful_basis": "legitimate_interest", "dns": "on"})
        self.assertIn('value="legitimate_interest" selected', self.client.get("/contacts/new").get_data(as_text=True))
        c = query("SELECT * FROM contacts WHERE email = 'new@good.test'", one=True)
        self.assertTrue(eligibility(c)[0])
        r = self.post("/contacts/import", {"file": (io.BytesIO(b"email\nimp@good.test\n"), "list.csv")},
                      content_type="multipart/form-data")
        imp = int(r.headers["Location"].split("/")[-2])
        page = self.client.get(f"/contacts/import/{imp}/map").get_data(as_text=True)
        self.assertIn('value="legitimate_interest" selected', page)
        self.assertNotIn('name="purpose" required', page)
        self.post(f"/contacts/import/{imp}/map", {"col_0": "email", "recipient_type": "business",
                                                  "lawful_basis": "legitimate_interest", "dns": "on"})
        self.run_jobs()
        c = query("SELECT * FROM contacts WHERE email = 'imp@good.test'", one=True)
        self.assertTrue(eligibility(c)[0])
        self.assertEqual(c["purpose"], "Business outreach")

    def test_one_click_fix_for_contacts_without_permission(self):
        from gm.db import query
        self.contact("old@good.test", lawful_basis="unknown")
        self.assertIn("can't be emailed yet", self.client.get("/compose").get_data(as_text=True))
        self.post("/compose/mark-business")
        self.assertEqual(query("SELECT lawful_basis FROM contacts", one=True)["lawful_basis"], "legitimate_interest")

    def test_settings_one_form_and_connections_one_form(self):
        from gm.db import query
        self.post("/settings/", {"section": "details", "sender_name": "Olive", "sender_email": "olive@sender.test",
                                 "reply_to": "", "company_name": "Acme", "company_address": "9 New St"})
        ws = query("SELECT * FROM workspaces", one=True)
        self.assertEqual((ws["sender_email"], ws["reply_to"], ws["company_address"]),
                         ("olive@sender.test", "olive@sender.test", "9 New St"))
        self.use_fake_provider = True
        self.post("/settings/integrations", {"action": "email_service", "api_key": "re_abcdefghijklmnop"})
        ws = query("SELECT * FROM workspaces", one=True)
        self.assertEqual(ws["sandbox_mode"], 0, "unticked practice mode + a key = live")
        self.post("/settings/integrations", {"action": "email_service", "api_key": "", "sandbox_mode": "on"})
        self.assertEqual(query("SELECT sandbox_mode FROM workspaces", one=True)["sandbox_mode"], 1)
        from gm.security import get_secret
        self.assertEqual(get_secret(self.ws_id, "resend_api_key"), "re_abcdefghijklmnop", "empty field keeps the key")
