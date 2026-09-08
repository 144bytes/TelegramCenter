"""The binary upload route.

Attachments do not travel as JSON: base64 would inflate them by a third and
hold the whole file in memory twice. The raw body is streamed to a temp file,
handed to Telethon, and deleted either way.
"""
from __future__ import annotations

import json
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.models import Operator
from app.models.enums import AccountState
from app.web.server import WebServer


@pytest.fixture
def server(services, storage, telegram, seeded):  # noqa: ARG001
    web = WebServer(services, storage, telegram)
    web.start()
    yield web
    web.stop()


@pytest.fixture
def operator(storage, seeded):
    op = Operator(username="support", key="op_100", session_file="op_100.session",
                  telegram_id=100, api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.operators.add(op)
    return op


def _temp_files() -> set[Path]:
    root = Path(tempfile.gettempdir())
    return set(root.glob("tc-upload-*"))


def _wait_until_clean(before: set[Path], seconds: float = 3.0) -> set[Path]:
    """The response is written before the server's cleanup runs, so a check
    fired the instant it arrives can look a moment too early."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        left = _temp_files() - before
        if not left:
            return set()
        time.sleep(0.05)
    return _temp_files() - before


def upload(web, operator_id, name, payload=b"1234", caption="", peer=55,
           reply_to=None):
    req = urllib.request.Request(
        web.origin + "/api/operators/send_file", data=payload, method="POST")
    req.add_header("X-TC-Token", web.guard.token)
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("X-TC-Operator", operator_id)
    req.add_header("X-TC-Peer", str(peer))
    req.add_header("X-TC-Name", urllib.parse.quote(name))
    req.add_header("X-TC-Caption", urllib.parse.quote(caption))
    if reply_to is not None:
        req.add_header("X-TC-Reply-To", str(reply_to))
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# ── the happy path ──────────────────────────────────────────────────────
def test_a_file_reaches_telethon_under_its_own_name(server, telegram, operator):
    status, _body = upload(server, operator.id, "отчёт.pdf", b"%PDF-1.4",
                           caption="смотри")

    assert status == 200
    key, peer, path, caption = telegram.files[-1]
    assert key == "op_100"
    assert peer == 55
    assert Path(path).name == "отчёт.pdf", "Telegram names the file after the path"
    assert caption == "смотри"


def test_the_temp_file_is_gone_afterwards(server, telegram, operator):
    before = _temp_files()
    _status, _body = upload(server, operator.id, "pic.png")

    assert _wait_until_clean(before) == set(), "nothing left behind on success"


def test_the_temp_file_is_gone_after_a_failure(server, telegram, operator):
    """A send that throws must not leave the upload on disk - that is how a
    disk fills up one attachment at a time."""
    telegram.fail_on.add(55)
    before = _temp_files()

    status, _body = upload(server, operator.id, "pic.png")

    assert status >= 400
    assert _wait_until_clean(before) == set()


# ── what the browser is not allowed to decide ───────────────────────────
def test_a_path_in_the_name_is_stripped(server, telegram, operator):
    """The browser sends a name, never a location."""
    upload(server, operator.id, r"..\..\windows\evil.exe")

    assert Path(telegram.files[-1][2]).name == "evil.exe"


def test_a_name_that_is_only_dots_is_replaced(server, telegram, operator):
    """"..' survives being reduced to a base name and would point outside the
    folder, so it is not used at all."""
    status, _body = upload(server, operator.id, "..")

    assert status == 200
    assert Path(telegram.files[-1][2]).name == "upload"


def test_an_empty_body_is_refused(server, telegram, operator):
    status, body = upload(server, operator.id, "pic.png", payload=b"")
    assert status == 400
    assert telegram.files == []


def test_an_unknown_operator_is_refused(server, telegram):
    status, _body = upload(server, "op_nope", "pic.png")
    assert status == 404


def test_the_route_still_needs_the_token(server, operator):
    req = urllib.request.Request(
        server.origin + "/api/operators/send_file", data=b"1234", method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("X-TC-Operator", operator.id)
    req.add_header("X-TC-Peer", "55")
    try:
        urllib.request.urlopen(req, timeout=20)
        raise AssertionError("an untokened upload must be refused")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403


# ── the JSON routes are untouched ───────────────────────────────────────
def test_json_endpoints_still_work(server, telegram, operator):
    req = urllib.request.Request(
        server.origin + "/api/operators/send",
        data=json.dumps({"id": operator.id, "peer_id": 55, "text": "hi"}).encode(),
        method="POST")
    req.add_header("X-TC-Token", server.guard.token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as res:
        assert res.status == 200
    assert telegram.sent[-1][2] == "hi"


# ── an attachment can answer a message ──────────────────────────────────
def test_an_upload_can_answer_a_message(server, telegram, operator):
    status, body = upload(server, operator.id, "pic.png", reply_to=42)

    assert status == 200
    assert telegram.replied[-1] == 42
    assert body["reply_to"] == 42


def test_an_upload_without_a_reply_header_answers_nothing(server, telegram,
                                                           operator):
    upload(server, operator.id, "pic.png")
    assert telegram.replied[-1] is None
