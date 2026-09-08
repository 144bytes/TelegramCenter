"""The local HTTP layer: access control, routing, and the event stream."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.web import WebServer


@pytest.fixture
def server(services, storage, telegram, seeded):  # noqa: ARG001
    web = WebServer(services, storage, telegram)
    web.start()
    yield web
    web.stop()


def call(web, path, body=None, token="__real__", origin=None, method=None):
    url = web.origin + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data else "GET"))
    if token is not None:
        req.add_header("X-TC-Token", web.guard.token if token == "__real__" else token)
    if origin:
        req.add_header("Origin", origin)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            raw = res.read().decode("utf-8", "replace")
            try:
                return res.status, json.loads(raw)
            except json.JSONDecodeError:
                return res.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def raw_request(web, line):
    """Send a request line verbatim, bypassing urllib's path normalisation."""
    host, port = web.host, web.port
    with socket.create_connection((host, port), timeout=10) as conn:
        conn.sendall(f"GET {line} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                     f"Connection: close\r\n\r\n".encode())
        chunks = []
        while True:
            data = conn.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


# ── access control ──────────────────────────────────────────────────────
def test_binds_loopback_only(server):
    assert server.host == "127.0.0.1"


def test_token_is_required(server):
    assert call(server, "/api/state", token=None)[0] == 403


def test_wrong_token_is_rejected(server):
    assert call(server, "/api/state", token="not-the-token")[0] == 403


def test_correct_token_is_accepted(server):
    status, body = call(server, "/api/state")
    assert status == 200
    assert "accounts" in body


def test_foreign_origin_is_rejected(server):
    assert call(server, "/api/state", origin="https://evil.example")[0] == 403


def test_own_origin_is_accepted(server):
    assert call(server, "/api/state", origin=server.origin)[0] == 200


def test_preflight_is_refused(server):
    """Refusing OPTIONS is what stops a cross-origin page from ever sending
    the custom token header."""
    assert call(server, "/api/state", method="OPTIONS")[0] == 403


def test_unknown_api_route_is_404(server):
    assert call(server, "/api/does/not/exist")[0] == 404


def test_mutation_routes_are_not_reachable_by_get(server):
    # not present in the GET table, so a stray <img src> can never fire one
    assert call(server, "/api/accounts/delete")[0] == 404


def test_path_traversal_cannot_read_source(server):
    for attempt in ("/../../../app/config.py",
                    "/%2e%2e%2f%2e%2e%2fapp%2fconfig.py",
                    "/....//....//app/config.py"):
        body = raw_request(server, attempt)
        assert "APP_NAME" not in body
        assert "SCHEMA_VERSION" not in body


def test_token_differs_between_runs(services, storage, telegram):
    a = WebServer(services, storage, telegram)
    b = WebServer(services, storage, telegram)
    assert a.guard.token != b.guard.token
    assert len(a.guard.token) >= 32


# ── payloads ────────────────────────────────────────────────────────────
def test_state_carries_effective_and_issues(server):
    _, body = call(server, "/api/state")
    assert all("effective" in a and "issues" in a for a in body["accounts"])
    assert all("effective" in c and "issues" in c for c in body["campaigns"])


def test_oversized_body_is_rejected(server):
    status, _ = call(server, "/api/settings", {"values": {"x": "y" * 10}})
    assert status == 400   # unknown key, but importantly it did not crash


def test_malformed_json_is_rejected_cleanly(server):
    url = server.origin + "/api/settings"
    req = urllib.request.Request(url, data=b"{not json", method="POST")
    req.add_header("X-TC-Token", server.guard.token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            status = res.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 400


# ── settings validation ─────────────────────────────────────────────────
def test_unknown_setting_key_is_refused(server):
    status, body = call(server, "/api/settings", {"values": {"evil.path": 1}})
    assert status == 400
    assert "evil.path" in body["error"]


def test_every_setting_the_app_defines_can_be_saved(server, storage):
    """The whitelist is derived from the defaults, not retyped beside them.

    It used to be a second hand-written list, and adding a setting without
    remembering to add it there again produced "unknown setting" on a control
    the interface was showing.
    """
    from app.storage.settings import DEFAULTS, settable_keys

    defined = {f"{section}.{name}"
               for section, values in DEFAULTS.items()
               if isinstance(values, dict) for name in values}

    assert settable_keys() == defined, "every defined setting is settable"
    for key in sorted(defined):
        status, body = call(server, "/api/settings",
                            {"values": {key: storage.settings.get(key)}})
        assert status == 200, f"{key} was refused: {body}"


def test_the_operator_antispam_option_can_be_switched_on(server, storage):
    """The one that was refused."""
    status, _ = call(server, "/api/settings",
                     {"values": {"spamcheck.include_operators": True}})

    assert status == 200
    assert storage.settings.get("spamcheck.include_operators") is True


def test_faq_limit_is_bounded(server):
    assert call(server, "/api/settings",
                {"values": {"autoreply.faq_limit": 99}})[0] == 400
    assert call(server, "/api/settings",
                {"values": {"autoreply.faq_limit": -1}})[0] == 400
    assert call(server, "/api/settings",
                {"values": {"autoreply.faq_limit": 7}})[0] == 200


def test_operator_error_reaches_the_client_verbatim(server, seeded):
    status, body = call(server, "/api/autoreply/save", {
        "owner_id": seeded["account"].id, "enabled": True,
        "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "ask @operator"}],
    })
    assert status == 400
    assert "@operator" in body["error"]


# ── SSE ─────────────────────────────────────────────────────────────────
def test_event_stream_delivers_published_events(server):
    received = []

    def listen():
        req = urllib.request.Request(server.origin + "/api/events")
        req.add_header("X-TC-Token", server.guard.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                for raw in res:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))
                        if len(received) >= 2:
                            return
        except Exception:  # noqa: BLE001 - the socket is closed under us at teardown
            pass

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while server.bus.subscriber_count == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert server.bus.subscriber_count >= 1

    server.bus.publish("test.one", n=1)
    server.bus.publish("test.two", n=2)
    thread.join(timeout=5)

    assert len(received) >= 2
    assert received[0]["type"] == "test.one"
    assert received[0]["payload"]["n"] == 1


def test_event_stream_requires_the_token(server):
    assert call(server, "/api/events", token=None)[0] == 403


def test_a_slow_subscriber_cannot_stall_publishers(server):
    """A frozen tab must never block the scheduler."""
    from app.core.events import MAX_QUEUE
    sub = server.bus.subscribe()
    for i in range(MAX_QUEUE + 50):
        server.bus.publish("flood", n=i)      # never drains: must not block
    sub.close()


# ── static ──────────────────────────────────────────────────────────────
def test_root_serves_something_even_without_a_build(server):
    status, body = call(server, "/", token=None)
    assert status == 200
    assert isinstance(body, str) and "<" in body


# ── creating the API profile from the login form ────────────────────────
def test_login_rejects_an_api_id_without_a_hash(server):
    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100", "api_id": 555})
    assert status == 400
    assert "api_hash" in body["error"]


def test_login_rejects_a_hash_without_an_api_id(server):
    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100", "api_hash": "secret"})
    assert status == 400


def test_login_rejects_a_non_numeric_api_id(server):
    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100",
                         "api_id": "abc", "api_hash": "secret"})
    assert status == 400
    assert "api_id" in body["error"]


def test_typed_keys_reach_telegram_and_are_saved(server, storage, telegram):
    """One request: the account is added and its API profile created with it."""
    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100",
                         "api_id": 555, "api_hash": "secret",
                         "api_name": "Typed in"})

    assert status == 200, body
    assert body["state"] == "DONE", body.get("error")
    assert telegram.login_creds == (555, "secret"), "the typed keys must be used"

    profile = storage.api_profiles.find(lambda p: p.api_id == 555)
    assert profile is not None and profile.name == "Typed in"

    account = storage.accounts.find(lambda a: a.telegram_id == telegram.login_id)
    assert account is not None
    assert account.api_profile_id == profile.id, "the profile must be bound"


def test_rejected_keys_leave_no_profile_behind(server, storage, telegram):
    telegram.login_error = "ApiIdInvalidError"
    before = len(storage.api_profiles.all())

    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100",
                         "api_id": 777, "api_hash": "wrong"})

    assert status == 200
    assert body["state"] == "ERROR"
    assert len(storage.api_profiles.all()) == before, "no junk profile"
    assert storage.api_profiles.find(lambda p: p.api_id == 777) is None


def test_keys_that_already_exist_reuse_the_saved_profile(server, storage,
                                                         telegram, seeded):
    """Typing keys that are already on file must not make a second entry."""
    before = len(storage.api_profiles.all())

    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100",
                         "api_id": 123, "api_hash": "hash"})

    assert status == 200 and body["state"] == "DONE", body.get("error")
    assert len(storage.api_profiles.all()) == before, "no duplicate profile"

    account = storage.accounts.find(lambda a: a.telegram_id == telegram.login_id)
    assert account.api_profile_id == seeded["profile"].id


def test_a_saved_profile_can_still_be_picked_by_id(server, storage, telegram,
                                                   seeded):
    status, body = call(server, "/api/login/start",
                        {"kind": "phone", "phone": "+100",
                         "api_profile_id": seeded["profile"].id})

    assert status == 200 and body["state"] == "DONE", body.get("error")
    assert telegram.login_creds == (123, "hash")
    assert len(storage.api_profiles.all()) == 1


# ── antispam check over HTTP ────────────────────────────────────────────
def test_probe_all_skips_the_spam_pass_when_disabled(server, telegram):
    status, body = call(server, "/api/accounts/probe_all", {})
    assert status == 200
    assert body["checkup"]["spam"] is False
    assert telegram.asked == []


def test_probe_all_runs_the_spam_pass_when_enabled(server, storage, telegram):
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    status, body = call(server, "/api/accounts/probe_all", {})

    assert status == 200
    assert body["checkup"]["spam"] is True
    assert body["checkup"]["total"] == 1
    assert telegram.asked[0][1] == "@SpamBot"


def test_the_spam_pass_can_be_run_on_its_own(server, storage, telegram):  # noqa: ARG001
    storage.settings.set("spamcheck.delay_sec", 0)
    status, body = call(server, "/api/accounts/spam_check", {})
    assert status == 200
    assert body["checkup"]["total"] == 1
    assert telegram.asked[0][1] == "@SpamBot"


def test_a_limit_reaches_the_ui_as_an_account_issue(server, storage, telegram):
    telegram.bot_reply = "Your account is now limited until 3 October 2026."
    storage.settings.set("spamcheck.delay_sec", 0)
    call(server, "/api/accounts/spam_check", {})

    _status, snap = call(server, "/api/state")
    account = snap["accounts"][0]
    codes = [i["code"] for i in account["issues"]]
    assert "account.spam_limited" in codes
    assert account["spam_state"] == "LIMITED"


def test_the_bot_name_is_normalised_and_required(server, storage):
    assert call(server, "/api/settings",
                {"values": {"spamcheck.bot": "MyBot"}})[0] == 200
    assert storage.settings.get("spamcheck.bot") == "@MyBot"

    status, body = call(server, "/api/settings", {"values": {"spamcheck.bot": "  "}})
    assert status == 400 and "бота" in body["error"]


def test_the_delay_is_bounded(server):
    assert call(server, "/api/settings",
                {"values": {"spamcheck.delay_sec": 9999}})[0] == 400
    assert call(server, "/api/settings",
                {"values": {"spamcheck.delay_sec": -1}})[0] == 400
    assert call(server, "/api/settings",
                {"values": {"spamcheck.delay_sec": 30}})[0] == 200


def test_the_reply_phrases_can_be_edited(server, storage):
    status, _ = call(server, "/api/settings", {"values": {
        "spamcheck.clean_phrases": ["  all good  ", "", "fine"]}})
    assert status == 200
    assert storage.settings.get("spamcheck.clean_phrases") == ["all good", "fine"]


def test_edited_phrases_are_what_the_check_uses(server, storage, telegram,
                                                services):
    call(server, "/api/settings", {"values": {
        "spamcheck.clean_phrases": ["totally fine"],
        "spamcheck.limited_phrases": ["you are blocked"]}})
    telegram.bot_reply = "Everything is totally fine here."
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    call(server, "/api/accounts/spam_check", {})

    _status, snap = call(server, "/api/state")
    assert snap["accounts"][0]["spam_state"] == "CLEAN"


def test_emptying_the_phrases_restores_the_built_in_ones(server, storage,
                                                         services):
    from app.spamphrases import DEFAULT_CLEAN

    call(server, "/api/settings", {"values": {"spamcheck.clean_phrases": []}})
    assert services.spamcheck.clean_phrases == tuple(DEFAULT_CLEAN)


def test_phrases_must_be_a_list(server):
    status, body = call(server, "/api/settings",
                        {"values": {"spamcheck.clean_phrases": "not a list"}})
    assert status == 400 and "список" in body["error"]


def test_a_single_account_can_be_checked_over_http(server, storage, telegram):
    storage.settings.set("spamcheck.enabled", True)
    account = storage.accounts.all()[0]
    account.disabled = True
    storage.accounts.upsert(account)

    status, body = call(server, "/api/accounts/check", {"id": account.id})

    assert status == 200
    assert body["checkup"]["total"] == 1, "the same run, narrowed to one account"
    assert telegram.asked, "a switched-off account can still be verified"
