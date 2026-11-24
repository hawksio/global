"""Keeps a stuck background process from locking the database.

On shared hosting a cron worker can hang (a network call that never returns, the host pausing it). While it is
stuck inside a database write, nobody can sign in or save anything: every write waits and then fails. Two guards:

* time_limit(): a cron worker ends itself (SIGALRM) if it runs far longer than its budget, which releases its locks;
* stop_stuck_workers(): kills older workers and setup runs of this app that are still alive after MAX_AGE seconds.

Both only work on Linux/Unix (the hosting server); on Windows they do nothing.
"""
import os
import signal

MAX_AGE = 10 * 60  # seconds; a cron worker normally finishes within a minute


def time_limit(seconds):
    """End this process after `seconds` even if it's stuck in a blocking call (the default SIGALRM action)."""
    if hasattr(signal, "alarm"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(int(seconds))


def _is_ours(cmdline):
    """A cron worker (`flask --app gm worker --once`) or a setup run of this app. A long-running worker service
    (`worker` without --once, on a VPS) is never touched."""
    text = " ".join(cmdline)
    return ("--app gm" in text and " worker" in text and "--once" in text) or "setup_cpanel.py" in text


def _processes(proc="/proc"):
    """(pid, age in seconds, cmdline list) for each process of this user."""
    try:
        with open(os.path.join(proc, "uptime")) as fh:
            uptime = float(fh.read().split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, AttributeError):
        return
    uid = os.getuid()
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        path = os.path.join(proc, name)
        try:
            if os.stat(path).st_uid != uid:
                continue
            with open(os.path.join(path, "cmdline"), "rb") as fh:
                cmdline = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0") if p]
            with open(os.path.join(path, "stat")) as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            age = uptime - int(fields[19]) / ticks  # field 22 (starttime); fields here start at field 3
        except (OSError, ValueError, IndexError):
            continue
        yield int(name), age, cmdline


def stop_stuck_workers(max_age=MAX_AGE, proc="/proc", kill=os.kill):
    """Kill this app's workers/setup runs older than max_age. Returns the pids stopped."""
    if not hasattr(os, "getuid") or not os.path.isdir(proc):
        return []
    me, stopped = os.getpid(), []
    for pid, age, cmdline in _processes(proc) or ():
        if pid != me and age > max_age and _is_ours(cmdline):
            try:
                kill(pid, signal.SIGKILL)
                stopped.append(pid)
            except OSError:
                pass
    return stopped
