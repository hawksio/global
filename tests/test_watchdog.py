"""A stuck background worker must not keep the database locked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gm import watchdog  # noqa: E402


class StuckWorkers(unittest.TestCase):
    def test_recognises_only_this_apps_workers_and_setup(self):
        self.assertTrue(watchdog._is_ours(["/home/u/venv/bin/python", "/home/u/venv/bin/flask", "--app", "gm", "worker",
                                           "--once", "--budget", "50"]))
        self.assertTrue(watchdog._is_ours(["python", "setup_cpanel.py"]))
        self.assertFalse(watchdog._is_ours(["Passenger", "AppPreloader:", "/home/u/global-marketing"]))
        self.assertFalse(watchdog._is_ours(["php", "worker.php"]))
        self.assertFalse(watchdog._is_ours(["flask", "--app", "gm", "worker"]), "a VPS worker service is left alone")

    def test_kills_old_workers_but_not_new_ones_or_itself(self):
        procs = [(101, 1200.0, ["flask", "--app", "gm", "worker", "--once"]),
                 (102, 30.0, ["flask", "--app", "gm", "worker", "--once"]),
                 (103, 5000.0, ["lsphp"]),
                 (os.getpid(), 9999.0, ["flask", "--app", "gm", "worker"])]
        killed = []
        with mock.patch.object(watchdog, "_processes", return_value=procs), \
                mock.patch.object(watchdog.os.path, "isdir", return_value=True), \
                mock.patch.object(watchdog.os, "getuid", create=True, return_value=1000), \
                mock.patch.object(watchdog.signal, "SIGKILL", 9, create=True):
            stopped = watchdog.stop_stuck_workers(kill=lambda pid, sig: killed.append(pid))
        self.assertEqual(stopped, [101])
        self.assertEqual(killed, [101])
