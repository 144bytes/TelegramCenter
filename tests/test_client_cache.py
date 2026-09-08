"""Getting the connected client for a session.

The bug this covers, seen on a live account: opening a chat asks for the
dialog list and the live subscription at the same moment. Both went looking for
the same session's client, both found it unconnected, and both called
connect() on it. The dialog list came back; the subscription waited until the
HTTP call gave up 60 seconds later, so incoming messages never arrived at all
while sending worked perfectly - which is why it went unnoticed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.telegram import service as service_module
from app.telegram.service import TelegramService


class _FakeClient:
    """Enough of a Telethon client to show the race, and slow enough to lose
    it if the connect is not serialised."""

    instances: list["_FakeClient"] = []

    def __init__(self, *_args, **_kwargs):
        self.connect_calls = 0
        self.connected = False
        self.concurrent = 0
        self.max_concurrent = 0
        _FakeClient.instances.append(self)

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connect_calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.05)      # a real connect is not instant
        self.connected = True
        self.concurrent -= 1


@pytest.fixture
def service(monkeypatch, tmp_path):
    _FakeClient.instances.clear()
    monkeypatch.setattr(service_module, "TelegramClient", _FakeClient)
    svc = TelegramService()
    svc.configure_default_api(123, "hash")
    monkeypatch.setattr(svc, "_session_path", lambda key: tmp_path / key)
    return svc


def test_two_requests_for_one_session_connect_once(service):
    """Both callers get the same connected client, and connect runs once."""
    async def race():
        return await asyncio.gather(service._client_for("op_1"),
                                    service._client_for("op_1"))

    first, second = asyncio.run(race())

    assert first is second, "one client per session, not two"
    assert first.connect_calls == 1, "connect must not run twice"
    assert first.max_concurrent <= 1, "and never twice at the same time"


def test_many_requests_still_connect_once(service):
    async def stampede():
        return await asyncio.gather(*(service._client_for("op_1")
                                      for _ in range(8)))

    clients = asyncio.run(stampede())

    assert len({id(c) for c in clients}) == 1
    assert clients[0].connect_calls == 1


def test_different_sessions_are_not_serialised_against_each_other(service):
    """The lock is per session: two accounts must still connect in parallel,
    or a slow one would hold up the whole run."""
    async def both():
        await asyncio.gather(service._client_for("op_1"),
                             service._client_for("op_2"))

    asyncio.run(both())

    assert len(_FakeClient.instances) == 2
    assert all(c.connect_calls == 1 for c in _FakeClient.instances)


def test_an_already_connected_client_is_reused(service):
    asyncio.run(service._client_for("op_1"))
    client = asyncio.run(service._client_for("op_1"))

    assert client.connect_calls == 1, "no reconnect for a live client"


def test_a_dropped_connection_is_reopened(service):
    client = asyncio.run(service._client_for("op_1"))
    client.connected = False

    again = asyncio.run(service._client_for("op_1"))

    assert again is client
    assert client.connect_calls == 2


def test_missing_keys_are_refused_before_any_client_is_made(monkeypatch,
                                                            tmp_path):
    _FakeClient.instances.clear()
    monkeypatch.setattr(service_module, "TelegramClient", _FakeClient)
    svc = TelegramService()
    monkeypatch.setattr(svc, "_session_path", lambda key: tmp_path / key)

    with pytest.raises(RuntimeError):
        asyncio.run(svc._client_for("op_1"))

    assert _FakeClient.instances == [], "nothing is created without credentials"
