"""SQLite storage: one connection per request (or per worker task), rows as dict-like sqlite3.Row.

Schema changes are versioned migrations (MIGRATIONS below). On start-up pending migrations run in order,
each inside a transaction. If the database already holds data a copy is saved to instance/backups/ first,
so a failed or unwanted migration can be rolled back by restoring that copy.
Never edit a migration that has shipped; add a new one.

All timestamps are UTC text, "YYYY-MM-DD HH:MM:SS", which sorts and compares correctly as text.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import current_app, g

M1_INITIAL = """
CREATE TABLE workspaces (
    id                       INTEGER PRIMARY KEY,
    public_id                TEXT NOT NULL UNIQUE,
    name                     TEXT NOT NULL,
    company_name             TEXT NOT NULL DEFAULT '',
    company_address          TEXT NOT NULL DEFAULT '',
    website                  TEXT NOT NULL DEFAULT '',
    logo_url                 TEXT NOT NULL DEFAULT '',
    brand_color              TEXT NOT NULL DEFAULT '#2563eb',
    sender_name              TEXT NOT NULL DEFAULT '',
    sender_email             TEXT NOT NULL DEFAULT '',
    reply_to                 TEXT NOT NULL DEFAULT '',
    marketing_footer         TEXT NOT NULL DEFAULT '',
    timezone                 TEXT NOT NULL DEFAULT 'UTC',
    sandbox_mode             INTEGER NOT NULL DEFAULT 1,
    four_eyes_approval       INTEGER NOT NULL DEFAULT 0,
    daily_send_limit         INTEGER NOT NULL DEFAULT 200,
    hourly_send_limit        INTEGER NOT NULL DEFAULT 50,
    min_hours_between_emails INTEGER NOT NULL DEFAULT 72,
    ai_monthly_limit         INTEGER NOT NULL DEFAULT 200,
    discovery_daily_limit    INTEGER NOT NULL DEFAULT 200,
    bounce_pause_pct         REAL NOT NULL DEFAULT 4.0,
    complaint_pause_pct      REAL NOT NULL DEFAULT 0.1,
    retention_days           INTEGER NOT NULL DEFAULT 730,
    sending_paused_reason    TEXT NOT NULL DEFAULT '',
    email_login_codes        INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL
);

CREATE TABLE users (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    email            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash    TEXT NOT NULL,
    full_name        TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'marketer' CHECK (role IN ('owner', 'admin', 'marketer', 'analyst')),
    is_superadmin    INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    email_verified_at TEXT,
    mfa_secret_enc   TEXT NOT NULL DEFAULT '',
    mfa_enabled      INTEGER NOT NULL DEFAULT 0,
    email_codes      INTEGER NOT NULL DEFAULT 0,
    failed_logins    INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT,
    onboarding_done  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_login_at    TEXT
);
CREATE INDEX ix_users_workspace ON users(workspace_id);

CREATE TABLE user_sessions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sid_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    ip           TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_sessions_user ON user_sessions(user_id);

-- One-time secrets: email-verification codes, sign-in codes and password-reset tokens. Only hashes are stored.
CREATE TABLE auth_codes (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL CHECK (purpose IN ('verify_email', 'login', 'reset')),
    secret_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_auth_codes_user ON auth_codes(user_id, purpose);

CREATE TABLE rate_limits (
    key          TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL
);

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER,
    user_id      INTEGER,
    action       TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    ip           TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX ix_audit_ws ON audit_log(workspace_id, created_at);

-- Encrypted credentials (API keys, webhook secrets). workspace_id 0 holds system-wide secrets.
CREATE TABLE secrets (
    workspace_id INTEGER NOT NULL,
    name         TEXT NOT NULL,
    value_enc    TEXT NOT NULL,
    hint         TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    updated_by   INTEGER,
    PRIMARY KEY (workspace_id, name)
);

CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE contacts (
    id                     INTEGER PRIMARY KEY,
    workspace_id           INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email                  TEXT NOT NULL,
    email_normalized       TEXT NOT NULL,
    first_name             TEXT NOT NULL DEFAULT '',
    last_name              TEXT NOT NULL DEFAULT '',
    company                TEXT NOT NULL DEFAULT '',
    job_title              TEXT NOT NULL DEFAULT '',
    domain                 TEXT NOT NULL DEFAULT '',
    website                TEXT NOT NULL DEFAULT '',
    phone                  TEXT NOT NULL DEFAULT '',
    country                TEXT NOT NULL DEFAULT '',
    industry               TEXT NOT NULL DEFAULT '',
    timezone               TEXT NOT NULL DEFAULT '',
    source                 TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'import', 'discovery', 'api', 'sandbox')),
    source_url             TEXT NOT NULL DEFAULT '',
    source_detail          TEXT NOT NULL DEFAULT '',
    purpose                TEXT NOT NULL DEFAULT '',
    recipient_type         TEXT NOT NULL DEFAULT 'unknown' CHECK (recipient_type IN ('business', 'consumer', 'unknown')),
    lawful_basis           TEXT NOT NULL DEFAULT 'unknown'
                           CHECK (lawful_basis IN ('consent', 'legitimate_interest', 'existing_customer', 'unknown')),
    consent_at             TEXT,
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'unsubscribed', 'bounced', 'complained', 'suppressed')),
    verification_status    TEXT NOT NULL DEFAULT 'unverified'
                           CHECK (verification_status IN ('unverified', 'validated', 'invalid', 'risky', 'unknown', 'suppressed')),
    verification_checked_at TEXT,
    verification_detail    TEXT NOT NULL DEFAULT '',
    is_role                INTEGER NOT NULL DEFAULT 0,
    is_disposable          INTEGER NOT NULL DEFAULT 0,
    marketing_opt_out      INTEGER NOT NULL DEFAULT 0,
    notes                  TEXT NOT NULL DEFAULT '',
    import_id              INTEGER,
    sandbox                INTEGER NOT NULL DEFAULT 0,
    last_contacted_at      TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (workspace_id, email_normalized)
);
CREATE INDEX ix_contacts_ws_domain ON contacts(workspace_id, domain);
CREATE INDEX ix_contacts_ws_status ON contacts(workspace_id, status, verification_status);
CREATE INDEX ix_contacts_ws_created ON contacts(workspace_id, created_at);

CREATE TABLE tags (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (workspace_id, name)
);
CREATE TABLE contact_tags (
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
);
CREATE INDEX ix_contact_tags_tag ON contact_tags(tag_id);

CREATE TABLE contact_events (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    campaign_id  INTEGER,
    message_id   INTEGER,
    simulated    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX ix_contact_events_contact ON contact_events(contact_id, created_at);

CREATE TABLE suppressions (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email_normalized TEXT NOT NULL,
    reason           TEXT NOT NULL CHECK (reason IN ('unsubscribe', 'hard_bounce', 'complaint', 'manual', 'provider')),
    source           TEXT NOT NULL DEFAULT '',
    simulated        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    UNIQUE (workspace_id, email_normalized)
);

CREATE TABLE imports (
    id             INTEGER PRIMARY KEY,
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id        INTEGER,
    filename       TEXT NOT NULL,
    stored_name    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'uploaded'
                   CHECK (status IN ('uploaded', 'queued', 'processing', 'completed', 'failed', 'cancelled')),
    headers        TEXT NOT NULL DEFAULT '[]',
    mapping        TEXT NOT NULL DEFAULT '{}',
    options        TEXT NOT NULL DEFAULT '{}',
    total_rows     INTEGER NOT NULL DEFAULT 0,
    accepted       INTEGER NOT NULL DEFAULT 0,
    rejected       INTEGER NOT NULL DEFAULT 0,
    duplicates     INTEGER NOT NULL DEFAULT 0,
    invalid        INTEGER NOT NULL DEFAULT 0,
    suppressed     INTEGER NOT NULL DEFAULT 0,
    review         INTEGER NOT NULL DEFAULT 0,
    job_id         INTEGER,
    created_at     TEXT NOT NULL,
    completed_at   TEXT
);
CREATE TABLE import_issues (
    id         INTEGER PRIMARY KEY,
    import_id  INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    email      TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_import_issues ON import_issues(import_id);

-- Domain-level DNS results, shared by all workspaces (public information).
CREATE TABLE dns_cache (
    domain     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL,
    records    TEXT NOT NULL DEFAULT '[]',
    error      TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (domain, kind)
);

CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER,
    user_id          INTEGER,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    params           TEXT NOT NULL DEFAULT '{}',
    progress         INTEGER NOT NULL DEFAULT 0,
    total            INTEGER NOT NULL DEFAULT 0,
    message          TEXT NOT NULL DEFAULT '',
    result           TEXT NOT NULL DEFAULT '{}',
    error            TEXT NOT NULL DEFAULT '',
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    dedupe_key       TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    locked_by        TEXT NOT NULL DEFAULT '',
    heartbeat_at     TEXT,
    run_after        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);
CREATE INDEX ix_jobs_status ON jobs(status, run_after);
CREATE INDEX ix_jobs_ws ON jobs(workspace_id, created_at);
-- At most one queued or running job per dedupe key.
CREATE UNIQUE INDEX ux_jobs_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');

CREATE TABLE discovery_pages (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    domain       TEXT NOT NULL,
    url          TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    http_status  INTEGER,
    emails_found INTEGER NOT NULL DEFAULT 0,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX ix_discovery_pages_job ON discovery_pages(job_id);
CREATE TABLE discovery_results (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    domain           TEXT NOT NULL,
    company          TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL,
    email_normalized TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    is_role          INTEGER NOT NULL DEFAULT 0,
    off_domain       INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'added', 'dismissed', 'existing')),
    contact_id       INTEGER,
    found_at         TEXT NOT NULL,
    UNIQUE (job_id, email_normalized)
);
CREATE INDEX ix_discovery_results_ws ON discovery_results(workspace_id, status);
CREATE TABLE page_cache (
    url        TEXT PRIMARY KEY,
    status     INTEGER NOT NULL,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE templates (
    id              INTEGER PRIMARY KEY,
    workspace_id    INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'General',
    subject         TEXT NOT NULL DEFAULT '',
    preheader       TEXT NOT NULL DEFAULT '',
    html_body       TEXT NOT NULL DEFAULT '',
    text_body       TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    ai_generated    INTEGER NOT NULL DEFAULT 0,
    approved_at     TEXT,
    approved_by     INTEGER,
    archived        INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX ix_templates_ws ON templates(workspace_id, archived);
CREATE TABLE template_versions (
    id          INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    subject     TEXT NOT NULL,
    preheader   TEXT NOT NULL DEFAULT '',
    html_body   TEXT NOT NULL,
    text_body   TEXT NOT NULL,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    UNIQUE (template_id, version)
);

CREATE TABLE campaigns (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft', 'pending_approval', 'approved', 'scheduled', 'sending', 'paused',
                                       'completed', 'cancelled')),
    template_id      INTEGER,
    segment          TEXT NOT NULL DEFAULT '{}',
    from_name        TEXT NOT NULL DEFAULT '',
    from_email       TEXT NOT NULL DEFAULT '',
    reply_to         TEXT NOT NULL DEFAULT '',
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    send_window_start INTEGER NOT NULL DEFAULT 8,
    send_window_end   INTEGER NOT NULL DEFAULT 18,
    send_days        TEXT NOT NULL DEFAULT '0,1,2,3,4',
    rate_per_minute  INTEGER NOT NULL DEFAULT 10,
    scheduled_at     TEXT,
    dry_run          INTEGER NOT NULL DEFAULT 1,
    ai_generated     INTEGER NOT NULL DEFAULT 0,
    content_reviewed INTEGER NOT NULL DEFAULT 0,
    test_sent_at     TEXT,
    pause_reason     TEXT NOT NULL DEFAULT '',
    created_by       INTEGER,
    approved_by      INTEGER,
    approved_at      TEXT,
    launched_at      TEXT,
    completed_at     TEXT,
    cancelled_at     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX ix_campaigns_ws ON campaigns(workspace_id, status);

-- Step 0 is the first email; later steps are follow-ups sent delay_days after the previous step.
CREATE TABLE campaign_steps (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    step_no     INTEGER NOT NULL,
    delay_days  INTEGER NOT NULL DEFAULT 0,
    subject     TEXT NOT NULL,
    preheader   TEXT NOT NULL DEFAULT '',
    html_body   TEXT NOT NULL,
    text_body   TEXT NOT NULL DEFAULT '',
    UNIQUE (campaign_id, step_no)
);

CREATE TABLE campaign_recipients (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'stopped')),
    stop_reason  TEXT NOT NULL DEFAULT '',
    next_step    INTEGER NOT NULL DEFAULT 0,
    next_send_at TEXT,
    replied_at   TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (campaign_id, contact_id)
);
CREATE INDEX ix_recipients_due ON campaign_recipients(campaign_id, status, next_send_at);

CREATE TABLE messages (
    id               INTEGER PRIMARY KEY,
    workspace_id     INTEGER,
    campaign_id      INTEGER,
    recipient_id     INTEGER,
    contact_id       INTEGER,
    step_no          INTEGER,
    category         TEXT NOT NULL CHECK (category IN ('marketing', 'transactional', 'test')),
    kind             TEXT NOT NULL DEFAULT '',
    to_email         TEXT NOT NULL,
    subject          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'accepted', 'delivered', 'delivery_delayed', 'bounced', 'soft_bounced',
                                       'complained', 'failed', 'cancelled', 'suppressed', 'simulated')),
    provider         TEXT NOT NULL DEFAULT '',
    provider_id      TEXT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT NOT NULL DEFAULT '',
    simulated        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    accepted_at      TEXT,
    delivered_at     TEXT,
    bounced_at       TEXT,
    complained_at    TEXT,
    opened_at        TEXT,
    clicked_at       TEXT,
    payload          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_messages_ws ON messages(workspace_id, created_at);
CREATE INDEX ix_messages_campaign ON messages(campaign_id, status);
CREATE INDEX ix_messages_provider ON messages(provider_id);
CREATE INDEX ix_messages_retry ON messages(status, next_attempt_at);

CREATE TABLE webhook_events (
    id           INTEGER PRIMARY KEY,
    provider     TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    workspace_id INTEGER,
    message_id   INTEGER,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    processed_at TEXT,
    outcome      TEXT NOT NULL DEFAULT '',
    UNIQUE (provider, event_id)
);

CREATE TABLE dns_checks (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    domain       TEXT NOT NULL,
    results      TEXT NOT NULL,
    score        INTEGER NOT NULL,
    checked_at   TEXT NOT NULL
);

CREATE TABLE ai_usage (
    id            INTEGER PRIMARY KEY,
    workspace_id  INTEGER,
    user_id       INTEGER,
    task          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE INDEX ix_ai_usage_ws ON ai_usage(workspace_id, created_at);
"""

MIGRATIONS = [
    (1, "initial schema", M1_INITIAL),
]

_migrate_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def later(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(value):
    if not value:
        return None
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # some network filesystems refuse WAL; the default journal still works
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def applied_versions(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def backup_database(src_path, dest_dir, label):
    """Consistent online copy via the SQLite backup API. Returns the backup path."""
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"gm-{label}-{stamp}.sqlite3")
    src = sqlite3.connect(src_path)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    return dest


def migrate(db_path, backup_dir):
    with _migrate_lock:
        conn = connect(db_path)
        try:
            done = applied_versions(conn)
            pending = [m for m in MIGRATIONS if m[0] not in done]
            if not pending:
                return []
            has_data = bool(done)
            if has_data:
                backup_database(db_path, backup_dir, f"pre-migration-{pending[0][0]}")
            for version, name, sql in pending:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split(sql):
                        conn.execute(statement)
                    conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, now()))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            return [m[0] for m in pending]
        finally:
            conn.close()


def _split(sql):
    """Split a migration script into statements (no semicolons appear inside our string literals)."""
    parts, buf = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def init_db(app):
    migrate(app.config["DATABASE"], os.path.join(app.instance_path, "backups"))
    app.teardown_appcontext(close_db)


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def scalar(sql, args=()):
    row = get_db().execute(sql, args).fetchone()
    return row[0] if row else None


def execute(sql, args=()):
    cur = get_db().execute(sql, args)
    last = cur.lastrowid
    cur.close()
    return last


@contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on error. Nested use joins the outer transaction."""
    db = get_db()
    if db.in_transaction:
        yield db
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

