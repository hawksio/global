"""Organising contacts and emails: groups, deleting an import, a group or everything; Sent folders, search, delete."""
import io

from helpers import AppCase
from tests_live import live_mode


class OrganiseContacts(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def import_file(self, rows, name="list.csv", tag=""):
        data = ("email\n" + "\n".join(rows) + "\n").encode()
        r = self.post("/contacts/import", {"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
        imp = int(r.headers["Location"].split("/")[-2])
        self.post(f"/contacts/import/{imp}/map", {"col_0": "email", "recipient_type": "business",
                                                  "lawful_basis": "legitimate_interest", "tag": tag})
        self.run_jobs()
        return imp

    def test_groups_and_files_shown_and_import_can_be_deleted(self):
        from gm.db import scalar
        imp = self.import_file(["a@good.test", "b@good.test"], "fair.csv", tag="fair")
        self.contact("keep@good.test")
        page = self.client.get("/contacts/").get_data(as_text=True)
        self.assertIn("fair.csv", page)
        self.assertIn("👥 fair", page)
        self.assertIn("Delete this import", self.client.get(f"/contacts/?import_id={imp}").get_data(as_text=True))
        self.post(f"/contacts/import/{imp}/delete-contacts")
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 1)
        self.assertEqual(scalar("SELECT email FROM contacts"), "keep@good.test")

    def test_delete_group_or_just_the_label(self):
        from gm.db import scalar
        self.import_file(["a@good.test", "b@good.test"], tag="vip")
        self.contact("other@good.test")
        self.post("/contacts/groups/delete", {"tag": "vip", "keep": "1"})
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 3)
        self.assertIsNone(scalar("SELECT 1 FROM tags WHERE name = 'vip'"))
        self.import_file(["c@good.test"], tag="temp")
        self.post("/contacts/groups/delete", {"tag": "temp"})
        self.assertIsNone(scalar("SELECT 1 FROM contacts WHERE email = 'c@good.test'"))
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 3)

    def test_delete_all_needs_typed_confirmation_and_keeps_do_not_email(self):
        from gm.db import scalar
        from gm.suppression import add_suppression
        self.contact("a@good.test")
        self.contact("b@good.test")
        add_suppression(self.ws_id, "b@good.test", "unsubscribe")
        self.post("/contacts/delete-all", {"confirm_text": "yes"})
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 2, "nothing deleted without typing DELETE")
        self.post("/contacts/delete-all", {"confirm_text": "delete"})
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 0)
        self.assertEqual(scalar("SELECT COUNT(*) FROM suppressions"), 1, "unsubscribed people stay protected")

    def test_bulk_action_on_all_matching(self):
        import json
        from gm.db import scalar
        for i in range(60):
            self.contact(f"p{i}@good.test", country="Ghana" if i % 2 else "Kenya")
        self.post("/contacts/bulk", {"action": "delete", "all_matching": "1", "filters": json.dumps({"country": "ghana"})})
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 30)
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts WHERE country = 'Ghana'"), 0)


class OrganiseSent(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def compose(self, subject, action="send", **extra):
        data = {"to_mode": "all", "subject": subject, "body": "Hi {{first_name|there}}", "from_name": "Olive",
                "action": action, "speed": "2"}
        data.update(extra)
        return self.post("/compose", data)

    def test_folders_search_and_bulk_delete(self):
        from gm.db import query, scalar
        self.contact("ann@good.test")
        self.compose("Visa question")
        self.compose("Half done", action="save")
        page = self.client.get("/sent?folder=drafts").get_data(as_text=True)
        self.assertIn("Half done", page)
        self.assertNotIn("Visa question", page)
        found = self.client.get("/sent?q=visa").get_data(as_text=True)
        self.assertIn("Visa question", found)
        self.assertNotIn("Half done", found)
        ids = [str(r["id"]) for r in query("SELECT id FROM campaigns")]
        self.post("/sent/delete", {"ids": ids})
        self.assertEqual(scalar("SELECT COUNT(*) FROM campaigns"), 0)
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 1, "contacts are not touched")

    def test_deleting_an_email_that_is_still_sending_stops_it(self):
        from gm.db import query
        live_mode(self)
        for i in range(4):
            self.contact(f"p{i}@good.test")
        self.compose("Going out")
        self.assertEqual(len(self.provider.sent), 2)
        cid = query("SELECT id FROM campaigns", one=True)["id"]
        self.post(f"/sent/{cid}/delete")
        self.run_jobs()
        self.assertEqual(len(self.provider.sent), 2, "nothing more is sent after deleting")
        self.assertIsNone(query("SELECT 1 FROM campaigns", one=True))

    def test_picture_help_in_settings(self):
        page = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn("Show your picture next to your emails", page)
        self.assertIn("accounts.google.com/signup", page)
