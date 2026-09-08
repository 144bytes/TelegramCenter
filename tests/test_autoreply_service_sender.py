"""Telegram's own notices are not customers.

Signing an account in produces a private message from Telegram's service
account - "new login from ..." - and it is not flagged as a bot, so nothing
filtered it out. The log showed a cheerful sales reply being scheduled to
Telegram itself.
"""
from __future__ import annotations

import asyncio

from app.models.enums import AutoReplyKind


def _incoming(key, sender_id, **extra):
    payload = {"account_key": key, "peer_id": sender_id, "private": True,
               "sender_id": sender_id, "sender_name": "Telegram",
               "sender_username": None, "sender_bot": False,
               "message_id": 1, "text": "New login from ...",
               "media": None, "reply_to": None, "date": "", "out": False}
    payload.update(extra)
    return payload


def _armed(services, storage, seeded):
    services.autoreply.save_config("global", {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": AutoReplyKind.FIRST_MESSAGE, "enabled": True,
                   "match": "", "response": "Здравствуйте!"}]})
    return seeded["account"]


def test_a_notice_from_telegram_is_ignored(services, storage, telegram, seeded):
    account = _armed(services, storage, seeded)
    responder = services.responder
    responder._running = True

    asyncio.run(responder._handle(_incoming(account.key, 777000)))

    assert telegram.sent == [], "Telegram is not a customer"


def test_a_real_person_still_gets_an_answer(services, storage, telegram, seeded):
    account = _armed(services, storage, seeded)
    responder = services.responder
    responder._running = True

    asyncio.run(responder._handle(_incoming(account.key, 12345,
                                            sender_name="Гость")))

    assert len(telegram.sent) == 1
