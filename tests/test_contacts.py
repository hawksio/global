"""Contacts, CSV import, normalisation, validation stages, DNS failures, disposable domains, suppression, export,
list cleaning and public contact discovery."""
import io
import json

from helpers import AppCase


class Validation(AppCase):
    def test_syntax_and_normalisation(self):
        from gm import validation
        self.assertFalse(validation.syntax_check("not-an-email")[0])
        self.assertFalse(validation.syntax_check("a@@b.test")[0])
        self.assertEqual(validation.normalize("  Alice.Smith+news@GOOD.test "), "alice.smith+news@good.test")
        # Dots and +tags are kept: no unsafe assumptions about mailbox identity.
        self.assertNotEqual(validation.normalize("a.b@good.test"), validation.normalize("ab@good.test"))

    def test_classification(self):
        from gm import validation
        self.assertEqual(validation.check("x@good.test", self.ws_id)["status"], "validated")
        self.assertEqual(validation.check("x@nowhere.test", self.ws_id)["status"], "invalid")
        self.assertEqual(validation.check("bad syntax", self.ws_id)["status"], "invalid")
        r = validation.check("info@good.test", self.ws_id)
        self.assertTrue(r["is_role"])
        self.assertEqual(r["status"], "validated", "role addresses are flagged, not invalid")
        r = validation.check("temp@mailinator.com", self.ws_id)
        self.assertTrue(r["is_disposable"])
        self.assertIn(r["status"], ("risky",))
        stages = [c["stage"] for c in validation.check("x@good.test", self.ws_id)["checks"]]
        self.assertEqual(stages, ["A", "B", "C", "D", "E", "F"])

    def test_dns_failures_are_unknown_not_invalid(self):
        import dns.exception
        import dns.resolver
        from gm import validation
        self.dns[("slow.test", "MX")] = dns.exception.Timeout
        self.dns[("broken.test", "MX")] = dns.resolver.NoNameservers
        self.assertEqual(validation.check("a@slow.test", self.ws_id)["status"], "unknown")
        self.assertEqual(validation.check("a@broken.test", self.ws_id)["status"], "unknown")

    def test_no_mx_fallback_and_null_mx(self):
        from gm import validation
        self.dns[("amx.test", "A")] = ["192.0.2.1"]
        self.assertEqual(validation.check("a@amx.test", self.ws_id)["status"], "risky")
        self.dns[("nullmx.test", "MX")] = ["0 ."]
        self.assertEqual(validation.check("a@nullmx.test", self.ws_id)["status"], "invalid")

    def test_dns_cache(self):
        from gm import validation
        calls = []
        orig = self.app.config["DNS_RESOLVER"]
        self.app.config["DNS_RESOLVER"] = lambda d, t: calls.append(d) or orig(d, t)
        validation.dns_mail_check("good.test")
        validation.dns_mail_check("good.test")
        self.assertEqual(len(calls), 1)

    def test_history_suppression(self):
        from gm import validation
        from gm.suppression import add_suppression
        add_suppression(self.ws_id, "gone@good.test", "unsubscribe")
        self.assertEqual(validation.check("gone@good.test", self.ws_id)["status"], "suppressed")


class Contacts(AppCase):
    def test_manual_add_duplicate_and_export_is_formula_safe(self):
        self.login()
        r = self.post("/contacts/new", {"email": "Pat@Good.test", "first_name": "=HYPERLINK(\"x\")", "recipient_type": "business",
                                        "lawful_basis": "legitimate_interest", "purpose": "B2B intro", "dns": "on"})
        self.assertEqual(r.status_code, 302)
        r = self.post("/contacts/new", {"email": "pat@good.test", "recipient_type": "business", "lawful_basis": "unknown"})
        self.assertIn("/contacts/", r.headers["Location"])
        from gm.db import scalar
        self.assertEqual(scalar("SELECT COUNT(*) FROM contacts"), 1)
        csv_text = self.client.get("/contacts/export").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", csv_text)

    def test_filters_and_bulk_tag(self):
        a = self.contact("a@good.test", company="Alpha", country="UK")
        self.contact("b@other.test", company="Beta", country="France")
        self.login()
        page = self.client.get("/contacts/?country=uk").get_data(as_text=True)
        self.assertIn("a@good.test", page)
        self.assertNotIn("b@other.test", page)
        self.post("/contacts/bulk", {"ids": [str(a)], "action": "tag", "tag": "vip"})
        self.assertIn("a@good.test", self.client.get("/contacts/?tag=vip").get_data(as_text=True))

    def test_csv_import_summary(self):
        self.login()
        from gm.suppression import add_suppression
        add_suppression(self.ws_id, "blocked@good.test", "unsubscribe")
        self.contact("existing@good.test")
        csv_data = ("E-mail,First Name,Company\n"
                    "one@good.test,One,A\n"
                    "ONE@good.test,Dup,A\n"
                    "not-an-email,Bad,B\n"
                    ",Nobody,C\n"
                    "blocked@good.test,Blocked,D\n"
                    "existing@good.test,Exists,E\n"
                    "temp@mailinator.com,Temp,F\n"
                    "two@nowhere.test,Two,G\n")
        r = self.post("/contacts/import", {"file": (io.BytesIO(csv_data.encode()), "list.csv")},
                      content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        imp_id = int(r.headers["Location"].split("/")[-2])
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("one@good.test", page)  # preview
        from gm.db import query
        mapping = json.loads(query("SELECT mapping FROM imports WHERE id = ?", (imp_id,), one=True)["mapping"])
        self.assertEqual(mapping, {"0": "email", "1": "first_name", "2": "company"})
        r = self.post(f"/contacts/import/{imp_id}/map", {"col_0": "email", "col_1": "first_name", "col_2": "company",
                                                         "recipient_type": "business", "lawful_basis": "legitimate_interest",
                                                         "purpose": "Prospecting", "source_detail": "Trade fair list", "dns": "on",
                                                         "tag": "fair"})
        self.assertEqual(r.status_code, 302)
        self.run_jobs()
        imp = query("SELECT * FROM imports WHERE id = ?", (imp_id,), one=True)
        self.assertEqual(imp["status"], "completed")
        self.assertEqual((imp["accepted"], imp["duplicates"], imp["invalid"], imp["rejected"], imp["suppressed"], imp["review"]),
                         (1, 2, 1, 1, 1, 2))
        c = query("SELECT * FROM contacts WHERE email_normalized = 'one@good.test'", one=True)
        self.assertEqual((c["source"], c["purpose"], c["lawful_basis"]), ("import", "Prospecting", "legitimate_interest"))
        self.assertIn("Trade fair list", c["source_detail"])
        self.assertIsNone(query("SELECT 1 FROM contacts WHERE email_normalized = 'blocked@good.test'", one=True))
        report = self.client.get(f"/contacts/import/{imp_id}/report.csv").get_data(as_text=True)
        self.assertIn("not-an-email", report)

    def test_import_rejects_bad_files(self):
        self.login()
        r = self.post("/contacts/import", {"file": (io.BytesIO(b"\x00\x01binary"), "x.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/contacts/import", r.headers["Location"])
        r = self.post("/contacts/import", {"file": (io.BytesIO(b"a,b\n1,2"), "x.exe")}, content_type="multipart/form-data")
        from gm.db import scalar
        self.assertEqual(scalar("SELECT COUNT(*) FROM imports"), 0)
        self.app.config["MAX_UPLOAD_BYTES"] = 100
        r = self.post("/contacts/import", {"file": (io.BytesIO(b"email\n" + b"a@good.test\n" * 50), "x.csv")},
                      content_type="multipart/form-data")
        self.assertEqual(scalar("SELECT COUNT(*) FROM imports"), 0)

    def test_imported_contact_not_eligible_without_basis(self):
        from gm.campaigns import eligibility
        from gm.db import query
        cid = self.contact("nobasis@good.test", lawful_basis="unknown")
        ok, why = eligibility(query("SELECT * FROM contacts WHERE id = ?", (cid,), one=True))
        self.assertFalse(ok)
        cid = self.contact("consumer@good.test", recipient_type="consumer", lawful_basis="legitimate_interest")
        ok, why = eligibility(query("SELECT * FROM contacts WHERE id = ?", (cid,), one=True))
        self.assertFalse(ok)
        self.assertIn("consent", why)

    def test_csv_validation_job(self):
        self.login()
        data = "email,name\na@good.test,A\na@good.test,Dup\nb@nowhere.test,B\nnope,C\n"
        r = self.post("/verification/csv", {"file": (io.BytesIO(data.encode()), "check.csv"), "dns": "on"},
                      content_type="multipart/form-data")
        job_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.run_jobs()
        from gm.db import query
        job = query("SELECT * FROM jobs WHERE id = ?", (job_id,), one=True)
        self.assertEqual(json.loads(job["result"]), {"validated": 1, "invalid": 2, "risky": 0, "unknown": 0, "suppressed": 0,
                                                     "duplicate": 1, "empty": 0})
        clean = self.client.get(f"/jobs/{job_id}/report.csv?only=clean").get_data(as_text=True)
        rows = [line for line in clean.strip().splitlines()[1:]]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("a@good.test,A,validated"))
        self.assertNotIn("nowhere", clean)

    def test_list_cleaning(self):
        self.contact("x@nowhere.test")
        self.contact("y@good.test")
        self.login()
        self.post("/cleaning/apply", {"group": "invalid", "action": "suppress"})
        from gm.db import query
        self.assertIsNotNone(query("SELECT 1 FROM suppressions WHERE email_normalized = 'x@nowhere.test'", one=True))
        self.assertIsNone(query("SELECT 1 FROM suppressions WHERE email_normalized = 'y@good.test'", one=True))


class Discovery(AppCase):
    SITE = {
        "https://biz.test/robots.txt": (200, "User-agent: *\nDisallow: /private\n"),
        "https://biz.test/": (200, '<html><title>Biz Ltd | Home</title><a href="/contact">Contact us</a>'
                                   '<a href="/private/staff">Staff</a><a href="/blog/post">Blog</a>'
                                   '<script>var x="hidden@biz.test";</script><p>Email sales@biz.test</p></html>'),
        "https://biz.test/contact": (200, '<a href="mailto:Hello@Biz.test?subject=Hi">Write to us</a>'
                                          '<p>Not real: someone@example.com and logo@2x.png</p><p>owner@gmail.test</p>'),
        "https://biz.test/private/staff": (200, "<p>secret@biz.test</p>"),
    }

    def setUp(self):
        super().setUp()
        self.fetched = []

        def fetch(url):
            self.fetched.append(url)
            status, body = self.SITE.get(url, (404, ""))
            return status, url, body
        self.app.config["CRAWL_FETCH"] = fetch

    def test_crawl_respects_robots_and_extracts_published_emails(self):
        self.login()
        r = self.post("/discover/", {"targets": "Biz Ltd, https://www.biz.test/", "confirm": "on"})
        job_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.run_jobs()
        from gm.db import query
        found = {row["email"]: row for row in query("SELECT * FROM discovery_results WHERE job_id = ?", (job_id,))}
        self.assertIn("sales@biz.test", found)
        self.assertIn("hello@biz.test", found)
        self.assertNotIn("secret@biz.test", found, "robots.txt disallowed page must not be fetched")
        self.assertNotIn("https://biz.test/private/staff", self.fetched)
        self.assertNotIn("hidden@biz.test", found, "addresses inside scripts are not 'published'")
        self.assertNotIn("someone@example.com", found)
        self.assertTrue(found["sales@biz.test"]["is_role"])
        self.assertTrue(found["owner@gmail.test"]["off_domain"])
        self.assertEqual(found["hello@biz.test"]["source_url"], "https://biz.test/contact")
        self.assertEqual(found["hello@biz.test"]["company"], "Biz Ltd")
        self.assertNotIn("https://biz.test/blog/post", self.fetched, "only contact-type pages are followed")
        # Review: adding requires a recorded purpose and basis.
        ids = [str(found["hello@biz.test"]["id"])]
        self.post(f"/discover/runs/{job_id}/review", {"ids": ids, "action": "add", "purpose": "", "lawful_basis": "unknown",
                                                     "recipient_type": "business"})
        self.assertIsNone(query("SELECT 1 FROM contacts WHERE email_normalized = 'hello@biz.test'", one=True))
        self.post(f"/discover/runs/{job_id}/review", {"ids": ids, "action": "add", "purpose": "B2B intro",
                                                     "lawful_basis": "legitimate_interest", "recipient_type": "business"})
        c = query("SELECT * FROM contacts WHERE email_normalized = 'hello@biz.test'", one=True)
        self.assertEqual(c["source"], "discovery")
        self.assertEqual(c["source_url"], "https://biz.test/contact")

    def test_robots_protected_site_is_skipped(self):
        self.SITE = dict(self.SITE)
        self.SITE["https://biz.test/robots.txt"] = (403, "")
        self.login()
        r = self.post("/discover/", {"targets": "biz.test", "confirm": "on"})
        job_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.run_jobs()
        from gm.db import scalar
        self.assertEqual(scalar("SELECT COUNT(*) FROM discovery_results WHERE job_id = ?", (job_id,)), 0)

    def test_ssrf_protection(self):
        from gm.discovery import Blocked, _check_url
        self.app.config.pop("CRAWL_FETCH")
        for url in ("http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/", "ftp://biz.test/", "http://biz.test:8080/", "http://user:pw@biz.test/"):
            with self.assertRaises(Blocked, msg=url):
                _check_url(url)

    def test_requires_confirmation_and_quota(self):
        self.login()
        self.post("/discover/", {"targets": "biz.test"})
        from gm.db import execute, scalar
        self.assertEqual(scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'discover'"), 0)
        execute("UPDATE workspaces SET discovery_daily_limit = 1")
        self.post("/discover/", {"targets": "biz.test\nother.test", "confirm": "on"})
        self.assertEqual(scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'discover'"), 0)

    def test_cancel_job(self):
        from gm import jobs
        from gm.db import query
        jid = jobs.enqueue("discover", {"targets": [{"host": "biz.test"}]}, workspace_id=self.ws_id, total=1)
        jobs.request_cancel(jid, self.ws_id)
        self.assertEqual(query("SELECT status FROM jobs WHERE id = ?", (jid,), one=True)["status"], "cancelled")
