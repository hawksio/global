"""Accounts: registration and email verification, sign-in, lockout, two-step verification, password reset,
invitations, permissions, workspace isolation, account export and deletion, migrations."""
import json
import os
import re
import sqlite3

from helpers import PASSWORD, AppCase


def last_code(outbox):
    text = outbox[-1]["text"]
    return re.search(r"\b(\d{6})\b", text).group(1)


class Registration(AppCase):
    def test_register_verify_with_code_and_sign_in(self):
        r = self.client.post("/register", data={"csrf_token": self.token(), "full_name": "New Person", "company": "NewCo",
                                                "email": "new@newco.test", "password": "a-long-enough-password", "terms": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify", r.headers["Location"])
        self.assertEqual(self.outbox[-1]["to"], ["new@newco.test"])
        code = last_code(self.outbox)
        # Code is stored hashed only.
        from gm.db import query
        stored = query("SELECT secret_hash FROM auth_codes ORDER BY id DESC LIMIT 1", one=True)["secret_hash"]
        self.assertNotIn(code, stored)
        r = self.client.post("/verify", data={"csrf_token": self.token(), "code": "000000" if code != "000000" else "111111"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/verify", data={"csrf_token": self.token(), "code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        # Single use: the same code no longer works.
        from gm import transactional
        uid = query("SELECT id FROM users WHERE email = 'new@newco.test'", one=True)["id"]
        self.assertFalse(transactional.check(uid, "verify_email", code))

    def test_existing_email_gets_same_answer(self):
        r = self.client.post("/register", data={"csrf_token": self.token(), "full_name": "X", "company": "Y",
                                                "email": "owner@acme.test", "password": "a-long-enough-password", "terms": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify", r.headers["Location"])
        self.assertIn("Security notice", self.outbox[-1]["subject"])

    def test_weak_password_rejected(self):
        r = self.client.post("/register", data={"csrf_token": self.token(), "full_name": "X", "company": "Y",
                                                "email": "x@y.test", "password": "short", "terms": "on"})
        self.assertEqual(r.status_code, 400)

    def test_code_expiry_and_attempt_limit(self):
        from gm import transactional
        from gm.db import execute
        code = transactional.new_code(self.owner_id, "login")
        execute("UPDATE auth_codes SET expires_at = datetime('now', '-1 minute')")
        self.assertFalse(transactional.check(self.owner_id, "login", code))
        code = transactional.new_code(self.owner_id, "login")
        for _ in range(5):
            transactional.check(self.owner_id, "login", "999999" if code != "999999" else "888888")
        self.assertFalse(transactional.check(self.owner_id, "login", code), "locked after 5 wrong attempts")

    def test_resend_cooldown(self):
        from gm import transactional
        transactional.new_code(self.owner_id, "verify_email")
        self.assertGreater(transactional.cooldown_remaining(self.owner_id, "verify_email"), 0)


class SignIn(AppCase):
    def test_login_logout(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        r = self.post("/logout")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_wrong_password_and_lockout(self):
        for _ in range(10):
            self.login(password="wrong-password-x")
        from gm.db import query
        self.assertIsNotNone(query("SELECT locked_until FROM users WHERE id = ?", (self.owner_id,), one=True)["locked_until"])
        from gm.db import execute
        execute("DELETE FROM rate_limits")  # isolate the account lock from the per-address rate limit
        r = self.login()  # correct password while locked
        self.assertEqual(r.status_code, 401)

    def test_login_rate_limited(self):
        statuses = [self.login(email="nobody@x.test", password="nope-nope-nope").status_code for _ in range(12)]
        self.assertIn(429, statuses)

    def test_totp_two_step(self):
        from gm.db import execute
        from gm.security import encrypt, new_totp_secret, totp_now
        secret = new_totp_secret()
        execute("UPDATE users SET mfa_enabled = 1, mfa_secret_enc = ? WHERE id = ?", (encrypt(secret), self.owner_id))
        r = self.login()
        self.assertIn("/login/2fa", r.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302, "not signed in before the second step")
        r = self.client.post("/login/2fa", data={"csrf_token": self.token(), "code": totp_now(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_email_login_code(self):
        from gm.db import execute
        execute("UPDATE users SET email_codes = 1 WHERE id = ?", (self.owner_id,))
        r = self.login()
        self.assertIn("/login/code", r.headers["Location"])
        r = self.client.post("/login/code", data={"csrf_token": self.token(), "code": last_code(self.outbox)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_open_redirect_blocked(self):
        r = self.client.post("/login", data={"csrf_token": self.token(), "email": "owner@acme.test", "password": PASSWORD,
                                             "next": "//evil.example/x"})
        self.assertEqual(r.headers["Location"], "/")


class PasswordReset(AppCase):
    def test_reset_is_single_use_and_revokes_sessions(self):
        other = self.app.test_client()
        self.login(client=other)
        self.client.post("/forgot", data={"csrf_token": self.token(), "email": "owner@acme.test"})
        link = re.search(r"https://gm\.test(/reset/\S+)", self.outbox[-1]["text"]).group(1)
        self.assertEqual(self.client.get(link).status_code, 200)
        r = self.client.post(link, data={"csrf_token": self.token(), "password": "a-brand-new-password"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(link).status_code, 400, "link works once")
        self.assertEqual(other.get("/").status_code, 302, "other sessions were signed out")
        self.assertEqual(self.login(password="a-brand-new-password").status_code, 302)

    def test_unknown_email_same_answer(self):
        n = len(self.outbox)
        r = self.client.post("/forgot", data={"csrf_token": self.token(), "email": "ghost@acme.test"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.outbox), n)

    def test_expired_reset(self):
        from gm import transactional
        from gm.db import execute
        token = transactional.new_reset_token(self.owner_id)
        execute("UPDATE auth_codes SET expires_at = datetime('now', '-1 minute')")
        self.assertEqual(self.client.get(f"/reset/{self.owner_id}/{token}").status_code, 400)


class Permissions(AppCase):
    def test_roles(self):
        self.add_user("analyst@acme.test", "analyst")
        self.add_user("marketer@acme.test", "marketer")
        analyst = self.app.test_client()
        self.login("analyst@acme.test", client=analyst)
        self.assertEqual(analyst.get("/contacts/").status_code, 200)
        self.assertEqual(analyst.get("/analytics/").status_code, 200)
        self.assertEqual(analyst.get("/contacts/new").status_code, 403)
        self.assertEqual(analyst.get("/settings/").status_code, 403)
        self.assertEqual(analyst.get("/admin/team").status_code, 403)
        self.assertEqual(analyst.get("/assistant/").status_code, 403)
        marketer = self.app.test_client()
        self.login("marketer@acme.test", client=marketer)
        self.assertEqual(marketer.get("/contacts/new").status_code, 200)
        cid = self.campaign()
        from gm.db import execute
        execute("UPDATE campaigns SET status = 'pending_approval' WHERE id = ?", (cid,))
        self.assertEqual(self.post(f"/campaigns/{cid}/approve", client=marketer).status_code, 403)
        self.assertEqual(marketer.get("/settings/integrations").status_code, 403)
        self.assertEqual(marketer.get("/admin/system").status_code, 403)

    def test_workspace_isolation(self):
        from werkzeug.security import generate_password_hash
        from gm.auth import create_workspace
        other_ws, _ = create_workspace("Rival", "boss@rival.test", "Boss", generate_password_hash(PASSWORD), verified=True)
        cid = self.contact("someone@good.test")
        camp = self.campaign()
        rival = self.app.test_client()
        self.login("boss@rival.test", client=rival)
        self.assertEqual(rival.get(f"/contacts/{cid}").status_code, 404)
        self.assertEqual(rival.get(f"/campaigns/{camp}").status_code, 404)
        self.assertEqual(rival.get("/templates/1").status_code, 404)
        self.assertNotIn("someone@good.test", rival.get("/contacts/").get_data(as_text=True))
        self.assertNotIn("someone@good.test", rival.get("/contacts/export").get_data(as_text=True))
        r = self.post("/contacts/bulk", {"ids": [str(cid)], "action": "delete"}, client=rival)
        self.assertEqual(r.status_code, 404)
        from gm.db import query
        self.assertIsNotNone(query("SELECT 1 FROM contacts WHERE id = ?", (cid,), one=True))

    def test_invite_flow(self):
        self.login()
        r = self.post("/admin/team", {"action": "invite", "email": "new@acme.test", "role": "marketer", "full_name": "Newbie"})
        self.assertEqual(r.status_code, 302)
        link = re.search(r"https://gm\.test(/reset/\S+)", self.outbox[-1]["text"]).group(1)
        fresh = self.app.test_client()
        r = fresh.post(link, data={"csrf_token": self.token(fresh), "password": "welcome-aboard-please"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.login("new@acme.test", "welcome-aboard-please", client=fresh).status_code, 302)
        self.assertEqual(fresh.get("/admin/team").status_code, 403)

    def test_admin_cannot_promote_to_admin(self):
        admin_id = self.add_user("admin@acme.test", "admin")
        target = self.add_user("m@acme.test", "marketer")
        c = self.app.test_client()
        self.login("admin@acme.test", client=c)
        r = self.post("/admin/team", {"action": "role", "user_id": target, "role": "admin"}, client=c)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(admin_id)


class AccountData(AppCase):
    def test_export_and_delete(self):
        self.login()
        r = self.client.get("/account/export")
        data = json.loads(r.data)
        self.assertEqual(data["profile"]["email"], "owner@acme.test")
        self.assertNotIn("password_hash", json.dumps(data))
        self.contact("x@good.test")
        r = self.post("/account/delete", {"password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        from gm.db import query
        self.assertIsNone(query("SELECT 1 FROM workspaces WHERE id = ?", (self.ws_id,), one=True))
        self.assertIsNone(query("SELECT 1 FROM contacts WHERE workspace_id = ?", (self.ws_id,), one=True))

    def test_owner_with_team_cannot_delete(self):
        self.add_user("m@acme.test", "marketer")
        self.login()
        self.post("/account/delete", {"password": PASSWORD})
        from gm.db import query
        self.assertIsNotNone(query("SELECT 1 FROM users WHERE id = ?", (self.owner_id,), one=True))


class Migrations(AppCase):
    def test_migrations_idempotent_and_backup(self):
        from gm.db import MIGRATIONS, migrate
        db = self.app.config["DATABASE"]
        backups = os.path.join(self.app.instance_path, "backups")
        self.assertEqual(migrate(db, backups), [])
        conn = sqlite3.connect(db)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        conn.close()
        self.assertEqual(versions, [m[0] for m in MIGRATIONS])
        # A new migration on a populated database backs it up first.
        MIGRATIONS.append((999, "test column", "ALTER TABLE contacts ADD COLUMN test_col TEXT;"))
        try:
            self.assertEqual(migrate(db, backups), [999])
            self.assertTrue(any("pre-migration-999" in f for f in os.listdir(backups)))
        finally:
            MIGRATIONS.pop()
