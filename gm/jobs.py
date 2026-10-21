"""Persistent background jobs (imports, validation, discovery, campaign sending, retention).

Shared cPanel hosting usually cannot keep a worker process running, so the queue is a database table and work is
done by `flask --app gm worker --once`, run from cron every minute (it stops itself after a time budget).
On a VPS run `flask --app gm worker` continuously instead. In development the web process runs a worker thread.

Guarantees: at most one queued/running job per dedupe key; jobs are claimed atomically; a job whose worker died
(no heartbeat for STALE_MINUTES) is retried up to max_attempts; cancellation is cooperative (handlers call
ctx.check_cancel()); progress is stored for the UI to poll.
"""
import json
import os
import socket
import sqlite3
import threading
import time
import traceback

from flask import current_app

from .db import execute, now, query

HANDLERS = {}
STALE_MINUTES = 10
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class Cancelled(Exception):
    pass


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


def enqueue(kind, params=None, workspace_id=None, user_id=None, dedupe_key=None, total=0, max_attempts=3, delay_seconds=0):
    """Queue a job. Returns its id; if an identical job is already queued or running, returns that one instead."""
    if dedupe_key:
        existing = query("SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')", (dedupe_key,), one=True)
        if existing:
            return existing["id"]
    try:
        return execute("INSERT INTO jobs (workspace_id, user_id, kind, params, total, max_attempts, dedupe_key, run_after, "
                       "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', ?), ?)",
                       (workspace_id, user_id, kind, json.dumps(params or {}), total, max_attempts, dedupe_key,
                        f"+{int(delay_seconds)} seconds", now()))
    except sqlite3.IntegrityError:  # lost a race with an identical enqueue
        return query("SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')", (dedupe_key,), one=True)["id"]


def request_cancel(job_id, workspace_id):
    job = query("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, workspace_id), one=True)
    if not job:
        return False
    if job["status"] == "queued":
        execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, message = 'Cancelled before it started' "
                "WHERE id = ? AND status = 'queued'", (now(), job_id))
    elif job["status"] == "running":
        execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    return True


class Context:
    def __init__(self, job):
        self.job = job
        self.id = job["id"]
        self.params = json.loads(job["params"] or "{}")
        self.workspace_id = job["workspace_id"]
        self.user_id = job["user_id"]
        self._last_beat = 0

    def progress(self, done, total=None, message=None):
        if time.time() - self._last_beat < 0.5 and (total is None or done < total):
            return
        self._last_beat = time.time()
        sets, args = ["progress = ?", "heartbeat_at = ?"], [done, now()]
        if total is not None:
            sets.append("total = ?")
            args.append(total)
        if message is not None:
            sets.append("message = ?")
            args.append(message[:300])
        execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", (*args, self.id))

    def check_cancel(self):
        if query("SELECT cancel_requested FROM jobs WHERE id = ?", (self.id,), one=True)["cancel_requested"]:
            raise Cancelled()


def _claim():
    # Requeue jobs whose worker stopped sending heartbeats.
    execute("UPDATE jobs SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, "
            "error = CASE WHEN attempts >= max_attempts THEN 'Worker stopped responding' ELSE error END, "
            "finished_at = CASE WHEN attempts >= max_attempts THEN ? ELSE NULL END "
            "WHERE status = 'running' AND heartbeat_at < datetime('now', ?)", (now(), f"-{STALE_MINUTES} minutes"))
    job = query("SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY id LIMIT 1", (now(),), one=True)
    if not job:
        return None
    execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_by = ?, started_at = COALESCE(started_at, ?), "
            "heartbeat_at = ? WHERE id = ? AND status = 'queued'", (WORKER_ID, now(), now(), job["id"]))
    claimed = query("SELECT * FROM jobs WHERE id = ? AND status = 'running' AND locked_by = ?", (job["id"], WORKER_ID), one=True)
    return claimed


def run_one():
    job = _claim()
    if not job:
        return False
    ctx = Context(job)
    fn = HANDLERS.get(job["kind"])
    try:
        if fn is None:
            raise RuntimeError(f"No handler for job kind {job['kind']}")
        result = fn(ctx) or {}
        execute("UPDATE jobs SET status = 'completed', result = ?, finished_at = ?, progress = CASE WHEN total > 0 THEN total "
                "ELSE progress END WHERE id = ?", (json.dumps(result), now(), job["id"]))
    except Cancelled:
        execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, message = 'Cancelled' WHERE id = ?", (now(), job["id"]))
    except Exception as exc:
        current_app.logger.error("Job %s (%s) failed: %s", job["id"], job["kind"], traceback.format_exc(limit=5))
        retry = job["attempts"] < job["max_attempts"] and not isinstance(exc, (ValueError, PermissionError))
        execute("UPDATE jobs SET status = ?, error = ?, run_after = datetime('now', ?), finished_at = ? WHERE id = ?",
                ("queued" if retry else "failed", f"{type(exc).__name__}: {exc}"[:500],
                 f"+{30 * job['attempts']} seconds", None if retry else now(), job["id"]))
    return True


def tick():
    """Periodic duties, run once per worker cycle: queue campaign sending and daily retention clean-up."""
    from . import campaigns
    campaigns.schedule_sending()
    if not query("SELECT 1 FROM jobs WHERE kind = 'retention' AND created_at > datetime('now', '-1 day')", one=True):
        enqueue("retention", dedupe_key="retention")


def run_pending(time_budget=50, max_jobs=None):
    """Process queued jobs until none are due or the time budget is used. Returns the number processed."""
    start, n = time.time(), 0
    tick()
    while time.time() - start < time_budget and (max_jobs is None or n < max_jobs):
        if not run_one():
            break
        n += 1
    return n


def start_inline_worker(app, interval=5):
    """Development convenience: a daemon thread that processes jobs inside the web process."""
    def loop():
        while True:
            try:
                with app.app_context():
                    run_pending(time_budget=interval * 4)
            except Exception:
                app.logger.exception("Inline worker cycle failed")
            time.sleep(interval)
    thread = threading.Thread(target=loop, name="gm-inline-worker", daemon=True)
    thread.start()
    return thread


def job_dict(job):
    return {"id": job["id"], "kind": job["kind"], "status": job["status"], "progress": job["progress"], "total": job["total"],
            "message": job["message"], "error": job["error"], "result": json.loads(job["result"] or "{}"),
            "created_at": job["created_at"], "finished_at": job["finished_at"]}


@handler("retention")
def _retention(ctx):
    """Delete old logs and working files according to each workspace's retention period."""
    for ws in query("SELECT id, retention_days FROM workspaces"):
        cutoff = f"-{max(30, ws['retention_days'])} days"
        for sql in ("DELETE FROM contact_events WHERE workspace_id = ? AND created_at < datetime('now', ?)",
                    "DELETE FROM messages WHERE workspace_id = ? AND created_at < datetime('now', ?) AND campaign_id IS NULL",
                    "DELETE FROM discovery_results WHERE workspace_id = ? AND found_at < datetime('now', ?) AND status != 'added'",
                    "DELETE FROM audit_log WHERE workspace_id = ? AND created_at < datetime('now', ?)",
                    "DELETE FROM ai_usage WHERE workspace_id = ? AND created_at < datetime('now', ?)"):
            execute(sql, (ws["id"], cutoff))
        for imp in query("SELECT id, stored_name FROM imports WHERE workspace_id = ? AND status IN ('completed', 'failed', "
                         "'cancelled') AND created_at < datetime('now', '-30 days') AND stored_name != ''", (ws["id"],)):
            path = os.path.join(current_app.config["IMPORT_DIR"], imp["stored_name"])
            if os.path.exists(path):
                os.remove(path)
            execute("UPDATE imports SET stored_name = '' WHERE id = ?", (imp["id"],))
    execute("DELETE FROM jobs WHERE status IN ('completed', 'cancelled', 'failed') AND created_at < datetime('now', '-90 days') "
            "AND kind IN ('send_campaigns', 'retention')")
    execute("DELETE FROM page_cache WHERE fetched_at < datetime('now', '-2 days')")
    execute("DELETE FROM user_sessions WHERE expires_at < datetime('now', '-30 days')")
    execute("DELETE FROM auth_codes WHERE expires_at < datetime('now', '-7 days')")
    return {"note": "retention clean-up finished"}
