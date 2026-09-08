"""Working out what a target actually is, and how to reach it.

Two thirds of the delivery failures in the field came from one line:
`isinstance(entity, types.Channel)` being used to mean "broadcast channel". In
Telethon that class covers supergroups too, so every supergroup was filed as a
channel and then asked for the discussion chat only a channel has -
PeerIdInvalidError sixty-six times, MsgIdInvalidError seven more.

The rest came from addresses that could never resolve: invitation links
reduced to their hash and stored as usernames, and a single way of addressing
a chat with no fallback when it failed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.messages import AppError
from app.models import Target
from app.models.enums import TargetType
from app.services.catalog import parse_invite, parse_ref
from app.services.delivery import DeliveryError
from app.telegram.service import describe_message, entity_kind


# ── channel or group ────────────────────────────────────────────────────
def test_a_supergroup_is_not_a_broadcast_channel():
    from telethon import types
    supergroup = types.Channel(id=1, title="Чат", photo=None, date=None,
                               megagroup=True, broadcast=False)
    channel = types.Channel(id=2, title="Канал", photo=None, date=None,
                            megagroup=False, broadcast=True)

    assert entity_kind(supergroup) == TargetType.GROUP
    assert entity_kind(channel) == TargetType.CHANNEL


def test_a_small_group_and_a_person_are_told_apart():
    from telethon import types
    chat = types.Chat(id=3, title="Группа", photo=None, participants_count=2,
                      date=None, version=1)
    user = types.User(id=4)

    assert entity_kind(chat) == TargetType.GROUP
    assert entity_kind(user) == TargetType.USER


def test_delivery_asks_telegram_rather_than_reading_the_stored_type(
        services, storage, telegram, seeded):
    """Every target in the field said CHANNEL, and sending believed it.

    The record here says the same and is just as wrong; what makes the
    difference is that Telegram is asked at the moment of sending.
    """
    target = Target(title="Чат", username="realchat", type=TargetType.CHANNEL)
    storage.targets.add(target)
    telegram.kinds["realchat"] = TargetType.GROUP

    destination = asyncio.run(
        services.delivery.resolve(seeded["account"].key, target))

    assert destination.entity == "realchat", "written to directly"
    assert destination.reply_to is None, "no discussion chat was asked for"
    assert destination.comment is False


def test_the_channel_check_records_what_telegram_said(services, storage,
                                                      telegram, seeded):
    target = Target(title="Чат", username="realchat")
    storage.targets.add(target)
    telegram.kinds["realchat"] = TargetType.GROUP

    asyncio.run(services.catalog.check_target(target))

    assert storage.targets.get(target.id).type == TargetType.GROUP


# ── invitations ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("link", [
    "https://t.me/+AbCdEfGhIjKl",
    "t.me/joinchat/AbCdEfGhIjKl",
    "https://telegram.me/+AbCdEfGhIjKl",
])
def test_an_invitation_is_recognised_as_one(link):
    assert parse_invite(link) == "AbCdEfGhIjKl"


def test_an_ordinary_link_is_not_an_invitation():
    assert parse_invite("https://t.me/durov") == ""
    assert parse_ref("https://t.me/durov") == "durov"


def test_an_invitation_is_never_stored_as_a_username(services, storage):
    """It used to be, and Telegram answered every send with "no user has that
    username" - which was true, and useless."""
    target = services.catalog.create_target("https://t.me/+AbCdEfGhIjKl")

    assert target.invite == "AbCdEfGhIjKl"
    assert target.username == ""
    assert target.type == TargetType.GROUP, "nobody invites you to a channel"
    assert target.link == "https://t.me/+AbCdEfGhIjKl"


def test_the_same_invitation_is_not_added_twice(services, storage):
    services.catalog.create_target("https://t.me/+AbCdEfGhIjKl")
    with pytest.raises(AppError, match="err.target.invite_exists"):
        services.catalog.create_target("t.me/joinchat/AbCdEfGhIjKl")


def test_a_chat_we_are_already_in_is_reached_by_its_invitation(
        services, storage, telegram, seeded):
    target = services.catalog.create_target("https://t.me/+AbCdEfGhIjKl")
    telegram.invites["AbCdEfGhIjKl"] = "the_private_chat"

    destination = asyncio.run(
        services.delivery.resolve(seeded["account"].key, target))

    assert destination.entity == "the_private_chat"


def test_an_invitation_we_have_not_accepted_says_so_plainly(
        services, storage, telegram, seeded):
    target = services.catalog.create_target("https://t.me/+AbCdEfGhIjKl")

    with pytest.raises(DeliveryError) as err:
        asyncio.run(services.delivery.resolve(seeded["account"].key, target))

    assert err.value.message["code"] == "excluded.not_member"
    assert err.value.fault == "not_member", "no request to write is made"


# ── addressing ──────────────────────────────────────────────────────────
def test_the_id_is_tried_when_the_username_does_not_resolve(
        services, storage, telegram, seeded):
    target = Target(title="Чат", username="gone", telegram_id=-100500)
    storage.targets.add(target)
    telegram.unresolvable.add("gone")
    telegram.kinds[-100500] = TargetType.GROUP

    destination = asyncio.run(
        services.delivery.resolve(seeded["account"].key, target))

    assert destination.entity == -100500


def test_a_target_nothing_can_reach_says_so_plainly(
        services, storage, telegram, seeded):
    target = Target(title="Чат", username="gone", telegram_id=-100500)
    storage.targets.add(target)
    telegram.unresolvable.update({"gone", -100500})

    with pytest.raises(DeliveryError) as err:
        asyncio.run(services.delivery.resolve(seeded["account"].key, target))

    assert err.value.message["code"] == "tg.not_found",         "not a raw Telethon class name"


def test_a_target_with_no_address_at_all_is_refused(services, storage, seeded):
    target = Target(title="Пусто")
    storage.targets.add(target)

    with pytest.raises(DeliveryError):
        asyncio.run(services.delivery.resolve(seeded["account"].key, target))


# ── the channel check uses the sending account ──────────────────────────
def test_the_check_prefers_an_account_that_will_actually_send(
        services, storage, seeded):
    """"Доступен" said by some other account is a promise nobody made."""
    from app.models import Account
    from app.models.enums import AccountState

    other = Account(key="session_first", telegram_id=1, username="first",
                    api_profile_id=seeded["profile"].id,
                    raw_state=AccountState.READY)
    storage.accounts.add(other)
    # `other` comes first in the list; the campaign's account is the seeded one
    services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}]})

    assert services.catalog._probe_key(seeded["target"]) == seeded["account"].key


def test_without_a_campaign_any_ready_account_will_do(services, storage, seeded):
    assert services.catalog._probe_key(seeded["target"]) == seeded["account"].key


# ── Telethon handing back nothing ───────────────────────────────────────
def test_a_send_that_describes_nothing_is_not_a_crash():
    """Telethon sometimes returns None although the message went through.

    Raising here was worse than useless: the campaign recorded a failure for
    a message that had very likely arrived, and sent it again next cycle.
    Duplicates are exactly what gets an account reported.
    """
    described = describe_message(None)

    assert described["id"] == 0
    assert described["text"] == ""
    assert described["out"] is True


def test_a_send_returning_nothing_still_counts_as_sent(
        services, storage, telegram, seeded):
    from app.models.enums import TargetResultStatus

    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0,
        "schedule": {"mode": "ONCE", "at": "2020-01-01T00:00:00"}})

    async def send_nothing(key, entity, text, file=None, reply_to=None):
        telegram.sent.append((key, entity, text))
        return describe_message(None)

    telegram.send_message = send_nothing
    asyncio.run(services.scheduler._run(campaign))

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.status == TargetResultStatus.SENT
    assert result.attempts == 1, "and not tried a second time"
