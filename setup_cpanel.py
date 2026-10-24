"""One-time setup for cPanel hosting without Terminal access.

Run it from cPanel → Setup Python App → your app → "Execute python script":

    setup_cpanel.py yourdomain.com you@yourdomain.com

It installs the packages from requirements.txt first (add --skip-install to skip that).
If your control panel can't pass arguments, put the domain and the email on two lines in setup_cpanel.txt
and run just: setup_cpanel.py
Everything it prints is also saved in setup_cpanel.log (open it in File Manager).

It is safe to run again: an existing .env, database or account is never overwritten.
What it does:
  1. creates the private settings file .env (production settings and freshly generated secret keys);
  2. creates the data folder ~/gm-data (outside public_html) and the database;
  3. creates your owner account and prints a one-time link to choose your password (valid 24 hours);
  4. prints the cron command for the background sender.
"""
import os
import time
import secrets
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


LOG = os.path.join(ROOT, "setup_cpanel.log")
SETTINGS = os.path.join(ROOT, "setup_cpanel.txt")


class _Tee:
    """Print to the screen and to setup_cpanel.log (cPanel's script runner doesn't always show output)."""
    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "w", encoding="utf-8")

    def write(self, text):
        self.file.write(text)
        self.file.flush()
        try:
            self.stream.write(text)
        except Exception:
            pass

    def flush(self):
        self.file.flush()


def _arguments():
    """Domain and email from the command line, or from setup_cpanel.txt (two lines) next to this script."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2 and os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as fh:
            args = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    if len(args) < 2 or "@" not in args[1] or "." not in args[0]:
        return None, None
    return args[0].lower().removeprefix("https://").removeprefix("http://").strip("/"), args[1].lower()


def main():
    domain, email = _arguments()
    if not domain:
        print("Usage: setup_cpanel.py yourdomain.com you@yourdomain.com")
        print("   (or put the domain and the email on two lines in setup_cpanel.txt next to this script)")
        return 1
    print(f"Setting up Global Marketing for {domain} (owner: {email})")
    if "--skip-install" not in sys.argv:
        import subprocess
        print("0. Installing the required Python packages (this can take a few minutes)...")
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                               os.path.join(ROOT, "requirements.txt")], capture_output=True, text=True)
        if proc.returncode != 0:
            print("   Installation FAILED. The last lines from pip were:")
            for line in (proc.stdout + proc.stderr).strip().splitlines()[-25:]:
                print("   | " + line)
            return 1
        print("   Packages installed.")
    home = os.path.expanduser("~")
    data_dir = os.path.join(home, "gm-data")
    env_path = os.path.join(ROOT, ".env")

    os.makedirs(data_dir, exist_ok=True)
    if os.path.exists(env_path):
        print("1. .env already exists: kept as it is.")
    else:
        from cryptography.fernet import Fernet
        settings = {
            "GM_ENV": "production",
            "GM_BASE_URL": f"https://{domain}",
            "GM_INSTANCE_DIR": data_dir,
            "GM_SECURE_COOKIES": "1",
            "GM_TRUST_PROXY": "1",
            "GM_DEV_ADMIN": "0",
            "GM_ALLOW_SIGNUP": "0",
            "GM_INLINE_WORKER": "0",
            "GM_TRANSACTIONAL_FROM": f"{domain} <{email}>",
            "GM_SECRET_KEY": secrets.token_hex(32),
            "GM_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("# Created by setup_cpanel.py. Keep this file private and back it up safely.\n")
            fh.writelines(f"{k}={v}\n" for k, v in settings.items())
        os.chmod(env_path, 0o600)
        print(f"1. Created .env with production settings and new secret keys (private: permissions 600).")

    sys.path.insert(0, ROOT)
    from gm import create_app
    app = create_app()  # applies database migrations
    with app.app_context():
        from gm.db import MIGRATIONS, connect, applied_versions, query
        conn = connect(app.config["DATABASE"])
        version = max(applied_versions(conn) or {0})
        conn.close()
        print(f"2. Database ready in {data_dir} (schema version {version} of {MIGRATIONS[-1][0]}).")

        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user and not user["last_login_at"]:
            # Never signed in, so the earlier link may have been missed: issue a fresh one (the old one stops working).
            from gm.transactional import new_reset_token
            token = new_reset_token(user["id"], minutes=24 * 60)
            print(f"3. The account {email} exists but has never signed in. New link to choose your password (24 hours):")
            print(f"   https://{domain}/reset/{user['id']}/{token}?invite=1")
        elif user:
            print(f"3. The account {email} already exists and has been used: nothing changed.")
        else:
            from werkzeug.security import generate_password_hash
            from gm.auth import create_workspace
            from gm.transactional import new_reset_token
            _, uid = create_workspace(domain, email, "Owner", generate_password_hash(secrets.token_urlsafe(32)),
                                      verified=True, superadmin=True)
            token = new_reset_token(uid, minutes=24 * 60)
            print(f"3. Created the owner account {email}. Open this link within 24 hours to choose your password:")
            print(f"   https://{domain}/reset/{uid}/{token}?invite=1")

    venv = os.path.dirname(os.path.dirname(sys.executable))
    print("4. Add this in cPanel > Cron Jobs (Once Per Minute) so campaigns are sent:")
    print(f"   cd {ROOT} && {os.path.join(venv, 'bin', 'flask')} --app gm worker --once --budget 50 >> {data_dir}/worker.log 2>&1")
    # Passenger restarts the app when tmp/restart.txt changes, so the new files are live without clicking Restart.
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    with open(os.path.join(ROOT, "tmp", "restart.txt"), "w") as fh:
        fh.write(str(time.time()))
    try:
        version = open(os.path.join(ROOT, "gm", "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        version = "unknown"
    print(f"5. App restarted with version {version}. Check https://{domain}/healthz shows the same version.")
    print("Done. (If the site still shows the old version, click RESTART on the Python app page.)")
    return 0


if __name__ == "__main__":
    sys.stdout = sys.stderr = _Tee(sys.__stdout__, LOG)
    try:
        code = main()
    except Exception:
        import traceback
        print("Setup stopped with an error:")
        traceback.print_exc(file=sys.stdout)
        code = 1
    print(f"(This output is also saved in {LOG})")
    sys.exit(code)
