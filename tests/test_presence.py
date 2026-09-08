"""What an account does around a send, and in what order.

An account that only ever posts — never online, never typing, never reading
anything — is a bot by definition. These are the signals a real client sends
without thinking about them.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import Target
from app.services import presence as presence_module
from app.services.presence import typing_seconds


def _campaign(services, storage, seeded, **extra):
    payload = {"name": "blast", "account_id": seeded["account"].id,
               "target_ids": [seeded["target"].id],
               "messages": [{"text": "Здравствуйте, есть предложение."}],
               "interval_min_sec": 0, "interval_max_sec": 0}
    payload.update(extra)
    return services.campaigns.create(payload)


def _steps(telegram, key):
    return [row[1] for row in telegram.presence if row[0] == key]


# ── the scripted order ──────────────────────────────────────────────────
def test_a_campaign_message_goes_out_the_way_a_person_sends_one(
        services, storage, telegram, seeded):
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert _steps(telegram, seeded["account"].key) == \
        ["online", "peek", "read", "send", "offline"]
    assert len(telegram.sent) == 1


def test_an_auto_reply_goes_out_the_same_way(services, storage, telegram,
                                             seeded):
    """Two different clients on one account would be stranger than either."""
    from app.models.enums import AutoReplyKind
    services.autoreply.save_config("global", {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": AutoReplyKind.FIRST_MESSAGE, "enabled": True,
                   "match": "", "response": "Здравствуйте!"}]})
    responder = services.responder
    responder._running = True
    payload = {"account_key": seeded["account"].key, "peer_id": 55,
               "private": True, "sender_id": 55, "sender_name": "Гость",
               "sender_username": None, "sender_bot": False, "message_id": 1,
               "text": "привет", "media": None, "reply_to": None,
               "date": "", "out": False}

    asyncio.run(responder._handle(payload))

    assert _steps(telegram, seeded["account"].key) == \
        ["online", "peek", "read", "send", "offline"]


def test_the_account_goes_quiet_even_when_the_send_fails(services, storage,
                                                         telegram, seeded):
    """Left online for ever because an error skipped the second half is
    exactly what tying the pair to a block prevents."""
    campaign = _campaign(services, storage, seeded)

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise RuntimeError("ChatWriteForbiddenError")

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    assert _steps(telegram, seeded["account"].key)[-1] == "offline"


def test_a_presence_call_that_fails_never_stops_the_message(
        services, storage, telegram, seeded):
    """These are manners. The message is the point."""
    campaign = _campaign(services, storage, seeded)

    async def refuse(key, online=True):
        raise RuntimeError("UpdateStatus is unavailable")

    telegram.set_online = refuse
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1


# ── typing takes as long as the text ────────────────────────────────────
def test_a_longer_text_takes_longer_to_type(monkeypatch):
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 0.1)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 600.0)
    short = min(typing_seconds("Привет") for _ in range(50))
    long_ = min(typing_seconds("Привет" * 40) for _ in range(50))

    assert long_ > short


def test_typing_is_bounded_at_both_ends(monkeypatch):
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 1.0)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 25.0)

    assert typing_seconds("ок") == pytest.approx(1.0)
    assert typing_seconds("слово " * 500) == pytest.approx(25.0)


def test_nothing_to_type_takes_no_time():
    assert typing_seconds("") == 0.0


def test_the_speed_is_not_the_same_every_time(monkeypatch):
    """A constant typing speed is its own kind of metronome."""
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 0.0)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 600.0)
    draws = {round(typing_seconds("одно и то же сообщение"), 4)
             for _ in range(30)}

    assert len(draws) > 1


def _no_real_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(presence_module.asyncio, "sleep", fake_sleep)
    return slept


def test_typing_is_shown_for_as_long_as_the_text_takes(
        services, storage, telegram, seeded, monkeypatch):
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 2.0)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 9.0)
    _no_real_sleep(monkeypatch)
    key = seeded["account"].key

    asyncio.run(services.presence.type_for(key, "chat", 10.0))

    assert len(telegram.typing) >= 2, "refreshed the way a client does"
    assert all(row == (key, "chat", "typing") for row in telegram.typing)


def test_a_campaign_types_before_it_sends(services, storage, telegram, seeded,
                                          monkeypatch):
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 2.0)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 9.0)
    _no_real_sleep(monkeypatch)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    steps = _steps(telegram, seeded["account"].key)
    assert steps.index("typing") < steps.index("send")


def test_a_refused_indicator_is_not_sent_again_for_this_message(
        services, storage, telegram, seeded, monkeypatch):
    """The first refusal ends it; the message still goes."""
    monkeypatch.setattr(presence_module, "TYPING_MIN_SEC", 20.0)
    monkeypatch.setattr(presence_module, "TYPING_MAX_SEC", 20.0)
    _no_real_sleep(monkeypatch)

    class UserBannedInChannelError(Exception):
        pass

    telegram.typing_error = UserBannedInChannelError("banned")
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    steps = _steps(telegram, seeded["account"].key)
    assert steps.count("typing") == 1, "asked once, then only waited"
    assert len(telegram.sent) == 1


# ── the operator's own order ────────────────────────────────────────────
def _operator(storage, seeded):
    from app.models import Operator
    from app.models.enums import AccountState
    op = Operator(username="helper", key="op_1", session_file="op_1.session",
                  telegram_id=1, api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.operators.add(op)
    return op


def test_opening_a_chat_is_what_puts_an_operator_online(services, storage,
                                                        telegram, seeded):
    op = _operator(storage, seeded)

    asyncio.run(services.operators.enter_chat(op, 77))

    assert _steps(telegram, "op_1") == ["online", "read"]


def test_closing_it_is_what_makes_them_quiet(services, storage, telegram,
                                             seeded):
    op = _operator(storage, seeded)

    asyncio.run(services.operators.leave_chat(op))

    assert _steps(telegram, "op_1") == ["offline"]


def test_typing_follows_the_keyboard(services, storage, telegram, seeded):
    op = _operator(storage, seeded)

    asyncio.run(services.operators.typing(op, 77))

    assert _steps(telegram, "op_1") == ["typing"]


def test_an_operator_send_waits_for_nothing(services, storage, telegram,
                                            seeded):
    """A human pressed send a moment ago and is watching for it."""
    op = _operator(storage, seeded)

    asyncio.run(services.operators.send(op, 77, "сейчас посмотрю"))

    assert len(telegram.sent) == 1
    assert telegram.typing == [], "no manufactured pause"
    assert _steps(telegram, "op_1") == ["send"], "presence is not tied to the send"


# ── reactions ───────────────────────────────────────────────────────────
def _with_reactions(storage, percent=100):
    storage.settings.update({"campaign.reactions": True,
                             "campaign.reaction_percent": percent})


def test_a_reaction_lands_on_the_newest_message(services, storage, telegram,
                                                seeded):
    _with_reactions(storage)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.reacted) == 1
    _key, _entity, message_id, emoji = telegram.reacted[0]
    assert message_id == 4242, "whatever the chat's latest message is"
    assert emoji in telegram.reactions[:2], "the first or the second offered"


def test_the_reaction_comes_before_our_message(services, storage, telegram,
                                               seeded):
    """After sending, the newest message would be our own."""
    _with_reactions(storage)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    steps = _steps(telegram, seeded["account"].key)
    assert steps.index("react") < steps.index("send")


def test_a_reaction_the_chat_refuses_for_its_own_reasons_changes_nothing(
        services, storage, telegram, seeded):
    _with_reactions(storage)

    class ReactionInvalidError(Exception):
        pass

    telegram.react_error = ReactionInvalidError("no such reaction here")
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1, "the message goes as usual"


def test_nothing_reacts_while_the_switch_is_off(services, storage, telegram,
                                                seeded):
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.reacted == []


def test_a_zero_share_reacts_to_nothing(services, storage, telegram, seeded):
    _with_reactions(storage, percent=0)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.reacted == []


def test_a_chat_that_allows_none_gets_none(services, storage, telegram, seeded):
    _with_reactions(storage)
    telegram.reactions = []
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.reacted == []


def test_only_the_first_two_are_ever_used(services, storage, telegram, seeded):
    """Paid reactions are usually listed first and cost real money, so the
    transport strips them; taking anything further down the list would be
    picking an emoji nobody in that chat uses."""
    _with_reactions(storage)
    telegram.reactions = ["\U0001F44D", "❤", "\U0001F621", "\U0001F4A9"]
    target = Target(title="Ещё чат", username="more")
    storage.targets.add(target)
    campaign = _campaign(services, storage, seeded,
                         target_ids=[seeded["target"].id, target.id])

    asyncio.run(services.scheduler._run(campaign))

    used = {row[3] for row in telegram.reacted}
    assert used <= set(telegram.reactions[:2])


def test_a_failed_reaction_does_not_fail_the_message(services, storage,
                                                     telegram, seeded):
    _with_reactions(storage)
    campaign = _campaign(services, storage, seeded)

    async def refuse(key, entity, message_id, emoji):
        raise RuntimeError("ReactionInvalidError")

    telegram.react = refuse
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1
