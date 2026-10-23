"""SMTP as a second sending service, and choosing the default service."""
import smtplib
from unittest import mock

from helpers import AppCase


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL. Records connections and messages; errors can be scripted."""
    instances = []
    login_error = None
    send_errors = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.tls, self.logged_in = host, port, [], False, None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.logged_in = (user, password)

    def send_message(self, msg):
        if FakeSMTP.send_errors:
            raise FakeSMTP.send_errors.pop(0)
        self.sent.append(msg)

    def quit(self):
        pass


class SmtpProviderTests(AppCase):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances, FakeSMTP.login_error, FakeSMTP.send_errors = [], None, []
        self.patches = [mock.patch("smtplib.SMTP", FakeSMTP), mock.patch("smtplib.SMTP_SSL", FakeSMTP)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def smtp(self, security="starttls"):
        from gm.mailer import SmtpProvider
        return SmtpProvider("mail.sender.test", 587, security, "olive@sender.test", "pw")

    def test_sends_text_email_and_reuses_one_connection(self):
        p = self.smtp()
        payload = {"from": "Olive <olive@sender.test>", "to": ["ann@good.test"], "subject": "Hello", "text": "Hi Ann",
                   "reply_to": "reply@sender.test"}
        with self.app.app_context():
            r1 = p.send(payload, "k1")
            r2 = p.send(dict(payload, to=["bob@good.test"]), "k2")
            p.close()
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(len(FakeSMTP.instances), 1, "one connection for the batch")
        conn = FakeSMTP.instances[0]
        self.assertTrue(conn.tls)
        self.assertEqual(conn.logged_in, ("olive@sender.test", "pw"))
        msg = conn.sent[0]
        self.assertEqual(msg["To"], "ann@good.test")
        self.assertEqual(msg["Reply-To"], "reply@sender.test")
        self.assertIn("@sender.test>", msg["Message-ID"])
        self.assertEqual(msg.get_content_type(), "text/plain", "plain emails stay plain text")
        self.assertIn(r1.provider_id, msg["Message-ID"])

    def test_errors_are_classified(self):
        p = self.smtp("ssl")
        payload = {"from": "o@sender.test", "to": ["a@good.test"], "subject": "s", "text": "t"}
        with self.app.app_context():
            FakeSMTP.send_errors = [smtplib.SMTPRecipientsRefused({"a@good.test": (550, b"No such user")})]
            r = p.send(payload, "k")
            self.assertFalse(r.ok or r.transient)
            self.assertIn("No such user", r.error)
            FakeSMTP.send_errors = [smtplib.SMTPDataError(451, b"Try later")]
            self.assertTrue(p.send(payload, "k").transient)
            FakeSMTP.send_errors = [smtplib.SMTPServerDisconnected()]
            self.assertTrue(p.send(payload, "k").ok, "reconnects once after a dropped connection")
            p.close()
            FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"Bad login")
            r = p.send(payload, "k")
            self.assertTrue(r.auth_error)
            ok, message = p.test_connection()
            self.assertFalse(ok)
            self.assertIn("username or password", message)


class ServiceChoiceTests(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def ws(self):
        from gm.db import query
        return query("SELECT * FROM workspaces WHERE id = ?", (self.ws_id,), one=True)

    def save_smtp(self, **extra):
        data = {"action": "smtp", "smtp_host": "mail.sender.test", "smtp_security": "ssl", "smtp_port": "",
                "smtp_username": "olive@sender.test", "smtp_password": "secret-pw"}
        data.update(extra)
        with mock.patch("gm.mailer.SmtpProvider.test_connection", return_value=(True, "Connected")):
            return self.post("/settings/integrations", data)

    def test_save_smtp_then_choose_it_as_default(self):
        from gm import mailer
        from gm.security import get_secret
        self.post("/settings/integrations", {"action": "default_service", "email_provider": "smtp"})
        self.assertEqual(self.ws()["email_provider"], "resend", "can't pick a service that isn't set up")
        self.save_smtp()
        ws = self.ws()
        self.assertEqual((ws["smtp_host"], ws["smtp_port"], ws["smtp_security"]), ("mail.sender.test", 465, "ssl"))
        with self.app.app_context():
            self.assertEqual(get_secret(self.ws_id, "smtp_password"), "secret-pw")
        self.post("/settings/integrations", {"action": "default_service", "email_provider": "smtp"})
        self.assertEqual(self.ws()["email_provider"], "smtp")
        with self.app.test_request_context():
            self.assertEqual(mailer.live_provider(self.ws()).name, "smtp")
        page = self.client.get("/settings/integrations").get_data(as_text=True)
        self.assertIn("Sending with SMTP", page)
        self.assertNotIn("secret-pw", page)

    def test_empty_password_keeps_saved_one_and_remove_clears(self):
        from gm.security import get_secret
        self.save_smtp()
        self.save_smtp(smtp_password="", smtp_port="2525")
        with self.app.app_context():
            self.assertEqual(get_secret(self.ws_id, "smtp_password"), "secret-pw")
        self.assertEqual(self.ws()["smtp_port"], 2525)
        self.save_smtp(smtp_host="")
        self.assertEqual(self.ws()["smtp_host"], "")
        with self.app.app_context():
            self.assertEqual(get_secret(self.ws_id, "smtp_password"), "")

    def test_falls_back_to_the_other_service(self):
        from gm import mailer
        from gm.db import execute
        self.save_smtp()
        execute("UPDATE workspaces SET email_provider = 'resend' WHERE id = ?", (self.ws_id,))
        with self.app.test_request_context():
            self.assertEqual(mailer.live_provider(self.ws()).name, "smtp", "default Resend has no key, so SMTP is used")

    def test_rejects_a_url_as_server(self):
        self.save_smtp(smtp_host="https://mail.sender.test/")
        self.assertEqual(self.ws()["smtp_host"], "")
