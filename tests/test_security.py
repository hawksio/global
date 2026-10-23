"""Security: CSRF, secrets at rest and in responses/logs, headers, session cookies, private files, production config."""
import logging
import os

from helpers import AppCase


class Security(AppCase):
    def test_csrf_required(self):
        self.login()
        r = self.client.post("/contacts/new", data={"email": "x@good.test", "recipient_type": "business",
                                                    "lawful_basis": "unknown"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/logout")
        self.assertEqual(r.status_code, 400)

    def test_api_key_encrypted_and_never_returned(self):
        self.login()
        key = "re_LiveLookingKey_abcdefghij123"
        self.post("/settings/integrations", {"action": "resend_key", "api_key": key})
        from gm.db import query
        row = query("SELECT value_enc FROM secrets WHERE name = 'resend_api_key'", one=True)
        self.assertNotIn(key, row["value_enc"])
        from gm.security import get_secret
        self.assertEqual(get_secret(self.ws_id, "resend_api_key"), key)
        for page in ("/settings/integrations", "/admin/audit", "/account/export", "/analytics/?format=csv"):
            self.assertNotIn(key, self.client.get(page).get_data(as_text=True), page)
        self.post("/settings/integrations", {"action": "resend_key", "api_key": "not a key"})
        self.assertEqual(get_secret(self.ws_id, "resend_api_key"), key)

    def test_secrets_redacted_from_logs(self):
        from gm import _RedactSecrets
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with key %s", ("re_abcdefghijkl1234",), None)
        _RedactSecrets().filter(record)
        self.assertNotIn("re_abcdefghijkl1234", record.getMessage())

    def test_security_headers_and_cookie(self):
        r = self.login()
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        r = self.client.get("/")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_session_revoked_on_logout_cannot_be_replayed(self):
        self.login()
        with self.client.session_transaction() as s:
            sid = s["sid"]
        self.post("/logout")
        with self.client.session_transaction() as s:
            s["sid"] = sid
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_session_idle_expiry(self):
        self.login()
        from gm.db import execute
        execute("UPDATE user_sessions SET last_seen_at = datetime('now', '-2 hours')")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_private_files_not_served(self):
        self.login()
        import io
        r = self.post("/contacts/import", {"file": (io.BytesIO(b"email\na@good.test\n"), "x.csv")},
                      content_type="multipart/form-data")
        from gm.db import query
        stored = query("SELECT stored_name FROM imports", one=True)["stored_name"]
        self.assertTrue(os.path.exists(os.path.join(self.app.config["IMPORT_DIR"], stored)))
        for url in (f"/static/../instance/imports/{stored}", f"/imports/{stored}", "/static/../gm.sqlite3", "/instance/gm.sqlite3"):
            self.assertIn(self.client.get(url).status_code, (404, 400), url)
        anon = self.app.test_client()
        self.assertEqual(anon.get("/contacts/import/1").status_code, 302)

    def test_production_config_refuses_unsafe_settings(self):
        from gm import config
        old = {k: os.environ.get(k) for k in ("GM_ENV", "GM_SECRET_KEY", "GM_ENCRYPTION_KEY", "GM_SECURE_COOKIES", "GM_BASE_URL")}
        try:
            os.environ["GM_ENV"] = "production"
            os.environ.pop("GM_SECRET_KEY", None)
            problems = config.startup_errors()
            self.assertTrue(any("GM_SECRET_KEY" in p for p in problems))
            self.assertTrue(any("HTTPS" in p for p in problems))
            os.environ.update({"GM_SECRET_KEY": "x" * 40, "GM_ENCRYPTION_KEY": "k", "GM_SECURE_COOKIES": "1",
                               "GM_BASE_URL": "https://gm.example"})
            self.assertEqual(config.startup_errors(), [])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_dev_admin_in_tests_or_production(self):
        from gm.db import query
        self.assertIsNone(query("SELECT 1 FROM users WHERE email = 'admin@gm.test'", one=True))

    def test_healthz_reveals_no_secrets(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertNotIn(self.app.config["SECRET_KEY"], body)
        self.assertEqual(r.get_json()["checks"]["database"], "ok")

    def test_responsive_navigation_present(self):
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-menu-toggle', page)
        self.assertIn('aria-label="Main navigation"', page)
        import re
        sidebar = page[page.index('<ul class="nav">'):page.index("</ul>")]
        self.assertEqual(re.findall(r"</span>([^<]+)</a>", sidebar), ["Sent", "Contacts", "Settings"])
        self.assertIn("Compose</a>", page)
        more = page[page.index('class="nav-more"'):page.index("</details>", page.index('class="nav-more"'))]
        self.assertEqual(re.findall(r"</span>([^<]+)</a>", more),
                         ["Dashboard", "Saved emails &amp; assistant", "Campaigns (advanced)", "Reports"])
        # Every feature stays one click away as a tab inside its section.
        expected = {"/contacts/": ["All contacts", "Import a file", "Find on websites", "Check emails", "Clean up list",
                                   "Do-not-email list"],
                    "/templates/": ["Saved emails", "Writing assistant"],
                    "/campaigns/": ["All campaigns", "With follow-ups"],
                    "/analytics/": ["Results", "Email health"],
                    "/settings/": ["Company &amp; sender", "Connections", "Team", "Activity log", "Account emails",
                                   "My security"]}
        for url, tabs in expected.items():
            html = self.client.get(url).get_data(as_text=True)
            bar = html[html.index('class="tabs section-tabs"'):]
            bar = bar[:bar.index("</nav>")]
            self.assertEqual(re.findall(r">([^<]+)</a>", bar), tabs, url)
        # An analyst (read only) sees fewer tabs and no Settings section.
        self.add_user("analyst@acme.test", "analyst")
        analyst = self.app.test_client()
        self.login("analyst@acme.test", client=analyst)
        html = analyst.get("/contacts/").get_data(as_text=True)
        self.assertNotIn("Import a file", html)
        self.assertNotIn(">Connections<", analyst.get("/").get_data(as_text=True))

    def test_no_secret_files_in_repo(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
            ignore = fh.read()
        for entry in (".env", "instance/", ".venv/", "*.sqlite3"):
            self.assertIn(entry, ignore)
