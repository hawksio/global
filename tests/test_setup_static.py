"""setup_cpanel.py copies the CSS/JS/icons into the website folder so the web server sends them without waking the app."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import setup_cpanel  # noqa: E402

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PublishStatic(unittest.TestCase):
    def test_copies_static_files_into_the_website_folder(self):
        with tempfile.TemporaryDirectory() as home:
            site = os.path.join(home, "buryconsultantservice.com")
            os.makedirs(site)
            with open(os.path.join(site, ".htaccess"), "w") as fh:
                fh.write(f'# cPanel\nPassengerAppRoot "{APP_ROOT}"\nPassengerBaseURI "/"\n')
            os.makedirs(os.path.join(home, "public_html"))
            dest = setup_cpanel.publish_static(APP_ROOT, home)
            self.assertEqual(dest, os.path.join(site, "static"))
            self.assertTrue(os.path.exists(os.path.join(dest, "app.css")))
            self.assertIn("max-age=31536000", open(os.path.join(dest, ".htaccess")).read())

    def test_nothing_happens_when_no_website_folder_points_at_the_app(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "public_html"))
            self.assertIsNone(setup_cpanel.publish_static(APP_ROOT, home))
            self.assertFalse(os.path.exists(os.path.join(home, "public_html", "static")))

    def test_keeps_someone_elses_htaccess(self):
        with tempfile.TemporaryDirectory() as home:
            site = os.path.join(home, "public_html")
            os.makedirs(os.path.join(site, "static"))
            with open(os.path.join(site, ".htaccess"), "w") as fh:
                fh.write(f"PassengerAppRoot {APP_ROOT}\n")
            with open(os.path.join(site, "static", ".htaccess"), "w") as fh:
                fh.write("# mine\n")
            setup_cpanel.publish_static(APP_ROOT, home)
            self.assertEqual(open(os.path.join(site, "static", ".htaccess")).read(), "# mine\n")
