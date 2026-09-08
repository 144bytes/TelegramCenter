"""Only signed-in sessions are kept on disk.

A login writes its session under a temporary name and renames it once
Telegram has accepted the account. A wrong password, a closed dialog or a code
that never arrived left that temporary file behind: nothing would ever use it,
discovery skips it on purpose, and it sat in the folder for good.
"""
from __future__ import annotations

import asyncio

from app import config
from app.services.discovery import discover, purge_pending_sessions
from app.services.login import LoginState


def _pending(name="pending_abc123"):
    path = config.CAMPAIGN_SESSIONS_DIR / f"{name}.session"
    path.write_text("half a login")
    return path


def test_a_leftover_login_file_is_swept(app_dir):  # noqa: ARG001
    path = _pending()
    journal = path.with_suffix(".session-journal")
    journal.write_text("x")

    removed = purge_pending_sessions()

    assert removed == 2, "the journal beside it goes too"
    assert not path.exists()


def test_an_operator_login_leaves_the_same_way(app_dir):  # noqa: ARG001
    path = config.OPERATOR_SESSIONS_DIR / "op_pending_xyz.session"
    path.write_text("half a login")

    purge_pending_sessions()

    assert not path.exists()


def test_a_real_session_is_never_touched(app_dir):  # noqa: ARG001
    kept = config.CAMPAIGN_SESSIONS_DIR / "session_777.session"
    kept.write_text("a signed-in account")

    purge_pending_sessions()

    assert kept.exists(), "no record ever points at a pending_* name"


def test_a_login_in_progress_keeps_its_file(app_dir):  # noqa: ARG001
    """A rescan while somebody is signing in must not pull the file out from
    under them."""
    path = _pending("pending_live")

    purge_pending_sessions(keep={"pending_live"})

    assert path.exists()


def test_discovery_still_ignores_what_is_left(app_dir, storage):  # noqa: ARG001
    _pending()
    report = discover(storage)
    assert report["accounts"] == 0


# ── the login's own clean-up ────────────────────────────────────────────
def test_a_refused_login_takes_its_file_with_it(services, storage, telegram):
    """`remove_session` only disconnects; the file needs deleting as well,
    and that half was missing."""
    telegram.login_error = "PhoneCodeInvalidError"
    session = services.login.start(kind="qr", api_id=1, api_hash="h")
    path = config.CAMPAIGN_SESSIONS_DIR / f"{session.key}.session"
    path.write_text("half a login")

    asyncio.run(services.login._discard(session))

    assert not path.exists()
    assert session.key in telegram.dropped


def test_a_failed_login_is_reported_and_cleaned(services, storage, telegram):
    telegram.login_error = "PhoneNumberBannedError"
    session = services.login.start(kind="qr", api_id=1, api_hash="h")
    path = config.CAMPAIGN_SESSIONS_DIR / f"{session.key}.session"
    path.write_text("half a login")

    asyncio.run(services.login._run(session, {"api_id": 1, "api_hash": "h",
                                              "profile_id": None,
                                              "name": "", "explicit": True}, ""))

    assert session.state == LoginState.ERROR
    assert not path.exists(), "nothing half-done is kept"


def test_a_login_under_way_is_named_so_the_sweep_can_spare_it(services):
    session = services.login.start(kind="qr", api_id=1, api_hash="h")
    session.state = LoginState.QR_WAIT

    assert session.key in services.login.pending_keys()

    session.state = LoginState.DONE
    assert session.key not in services.login.pending_keys()
