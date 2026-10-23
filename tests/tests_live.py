"""Shared helper: switch a test workspace to live sending against the fake Resend provider."""


def live_mode(case):
    from gm.db import execute
    execute("UPDATE workspaces SET sandbox_mode = 0 WHERE id = ?", (case.ws_id,))
    case.use_fake_provider = True
