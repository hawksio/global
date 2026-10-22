"""One-time setup for cPanel hosting without Terminal access.

Run it from cPanel → Setup Python App → your app → "Execute python script":

    setup_cpanel.py yourdomain.com you@yourdomain.com

It installs the packages from requirements.txt first (add --skip-install to skip that).

It is safe to run again: an existing .env, database or account is never overwritten.
What it does:
  1. creates the private settings file .env (production settings and freshly generated secret keys);
  2. creates the data folder ~/gm-data (outside public_html) and the database;
  3. creates your owner account and prints a one-time link to choose your password (valid 24 hours);
  4. prints the cron command for the background sender.
"""
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) < 3 or "@" not in sys.argv[2] or "." not in sys.argv[1]:
        print("Usage: setup_cpanel.py yourdomain.com you@yourdomain.com")
        return 1
    domain = sys.argv[1].strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    email = sys.argv[2].strip().lower()
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
        if user:
            print(f"3. The account {email} already exists: nothing changed. Use 'Forgot your password?' if needed.")
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
    print("Done. Click RESTART on the Python app page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
