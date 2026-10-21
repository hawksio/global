"""Configuration from environment variables, optionally loaded from a .env file next to the project.

Every setting has a safe development default. In production (GM_ENV=production) start-up refuses to run
with settings that would be unsafe there; see startup_errors().
"""
import os
import secrets

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "GM_ENV": "development",                 # development | staging | production
    "GM_INSTANCE_DIR": "",                   # database, uploads, outbox, backups (default: ./instance)
    "GM_SECRET_KEY": "",                     # Flask session signing; generated into instance/ for development
    "GM_ENCRYPTION_KEY": "",                 # Fernet key protecting stored API keys; generated for development
    "GM_BASE_URL": "http://localhost:4322",  # public URL used in emails, unsubscribe links and webhooks
    "GM_ALLOW_SIGNUP": "1",                  # let anyone create a workspace from /register
    "GM_SECURE_COOKIES": "0",                # 1 behind HTTPS (required in production)
    "GM_TRUST_PROXY": "0",                   # number of reverse proxies in front (cPanel/Passenger: usually 1)
    "GM_SESSION_HOURS": "12",
    "GM_SESSION_IDLE_MINUTES": "60",
    "GM_MAX_UPLOAD_MB": "10",
    "GM_MAX_IMPORT_ROWS": "50000",
    "GM_INLINE_WORKER": "",                  # run background jobs in a thread of the web process (dev default: on)
    # Transactional email (sign-up codes, password resets, security alerts). Separate from marketing.
    "GM_RESEND_API_KEY": "",                 # system Resend key for transactional email; empty = local outbox only
    "GM_TRANSACTIONAL_FROM": "Global Marketing <no-reply@localhost>",
    "GM_TRANSACTIONAL_REPLY_TO": "",
    "GM_RESEND_WEBHOOK_SECRET": "",          # signing secret (whsec_...) for the system webhook endpoint
    # AI assistant (optional; the rule-based assistant works without it)
    "GM_AI_PROVIDER": "auto",                # auto | anthropic | rules | mock
    "GM_ANTHROPIC_API_KEY": "",
    "GM_AI_MODEL": "claude-opus-5-5",
    "GM_AI_EFFORT": "low",
    # Contact discovery crawler
    "GM_CRAWL_USER_AGENT": "GlobalMarketingBot/1.0 (+contact discovery; respects robots.txt)",
    "GM_CRAWL_MAX_PAGES": "8",
    "GM_CRAWL_DELAY_SECONDS": "1.5",
    "GM_CRAWL_TIMEOUT_SECONDS": "10",
    "GM_DNS_TIMEOUT_SECONDS": "4",
    "GM_DEV_ADMIN": "1",                     # development only: create admin@gm.test with a random password
}

_loaded = False


def load_dotenv(path=None):
    """Minimal .env reader (KEY=VALUE lines, # comments, optional quotes). Real env vars win."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = path or os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def env(name):
    return os.environ.get(name, DEFAULTS.get(name, ""))


def flag(name):
    return env(name).strip().lower() in ("1", "true", "yes", "on")


def is_production():
    return env("GM_ENV") in ("production", "staging")


def instance_dir():
    return env("GM_INSTANCE_DIR") or os.path.join(ROOT_DIR, "instance")


def _file_secret(instance, filename, factory):
    path = os.path.join(instance, filename)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    value = factory()
    os.makedirs(instance, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def secret_key(instance):
    return env("GM_SECRET_KEY") or _file_secret(instance, "secret_key", lambda: secrets.token_hex(32))


def encryption_key(instance):
    def make():
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()
    return env("GM_ENCRYPTION_KEY") or _file_secret(instance, "encryption_key", make)


def startup_errors():
    """Settings that must be right before the app may run in production."""
    if not is_production():
        return []
    problems = []
    if len(env("GM_SECRET_KEY")) < 32:
        problems.append("GM_SECRET_KEY must be set to a random value of at least 32 characters")
    if not env("GM_ENCRYPTION_KEY"):
        problems.append("GM_ENCRYPTION_KEY must be set (generate one with: python -m gm.keys)")
    if not flag("GM_SECURE_COOKIES"):
        problems.append("GM_SECURE_COOKIES must be 1 (the site must be served over HTTPS)")
    if not env("GM_BASE_URL").startswith("https://"):
        problems.append("GM_BASE_URL must be the public https:// address")
    if flag("GM_DEV_ADMIN") and os.environ.get("GM_DEV_ADMIN") is not None:
        problems.append("GM_DEV_ADMIN must not be enabled in production")
    return problems
