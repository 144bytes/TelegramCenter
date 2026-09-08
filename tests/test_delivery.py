"""Where a campaign message goes.

A channel is commented under its latest post; a user or a group is written to
directly. The same object answers both the send and the channel check, so
"comments are switched off here" cannot be true for one and false for the
other.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import Target
from app.models.enums import TargetResultStatus, TargetType
from app.messages import msg
from app.services.delivery import DeliveryError

NO_COMMENTS = msg("delivery.no_comments")


def _channel(storage, username="chan", title="Chan"):
    t = Target(title=title, username=username, type=TargetType.CHANNEL)
    storage.targets.add(t)
    return t


def _group(storage, telegram, username="grp"):
    """A supergroup, as Telegram reports it.

    The stored type is deliberately left saying CHANNEL: that is what every
    target in the field said, and delivery must reach the right answer anyway
    by asking Telegram instead of believing the record.
    """
    t = Target(title="Group", username=username, type=TargetType.CHANNEL)
    storage.targets.add(t)
    telegram.kinds[username] = TargetType.GROUP
    return t


def _auto_join(storage, delay=0):
    """Subscribing on, and the pause out of the way unless a test is about it."""
    storage.settings.set("campaign.auto_join", True)
    storage.settings.set("campaign.auto_join_delay_sec", delay)


def _send(services, key, target, text="hi"):
    """What the scheduler does for one message: find the chat, be in it,
    write."""
    async def go():
        destination = await services.delivery.resolve(key, target)
        await services.delivery.enter(key, destination, target)
        await services.delivery.send(key, destination, text)
        return destination
    return asyncio.run(go())


# ── picking the destination ─────────────────────────────────────────────
def test_channel_message_becomes_a_comment_under_the_last_post(
        services, storage, telegram, seeded):
    channel = _channel(storage)
    telegram.discussion[channel.username] = ("chat_for_chan", 4242)

    destination = asyncio.run(
        services.delivery.resolve(seeded["account"].key, channel))

    assert destination.entity == "chat_for_chan", "sent to the discussion chat"
    assert destination.reply_to == 4242, "answering the post's anchor message"
    assert destination.comment is True


def test_a_group_is_written_to_directly(services, storage, telegram, seeded):
    group = _group(storage, telegram)
    destination = asyncio.run(
        services.delivery.resolve(seeded["account"].key, group))

    assert destination.entity == group.username
    assert destination.reply_to is None
    assert destination.comment is False


def test_a_channel_without_comments_says_so_in_russian(
        services, storage, telegram, seeded):
    channel = _channel(storage, username="silent")
    telegram.no_comments.add("silent")

    with pytest.raises(DeliveryError) as err:
        asyncio.run(services.delivery.resolve(seeded["account"].key, channel))

    assert err.value.message == NO_COMMENTS, "a reason of its own, not Telethon's"


def test_the_send_answers_the_anchor(services, storage, telegram, seeded):
    channel = _channel(storage)
    telegram.discussion[channel.username] = ("chat_for_chan", 77)

    _send(services, seeded["account"].key, channel)

    assert telegram.sent == [(seeded["account"].key, "chat_for_chan", "hi")]
    assert telegram.replied == [77]


# ── auto-join ───────────────────────────────────────────────────────────
def test_nothing_is_joined_while_the_setting_is_off(
        services, storage, telegram, seeded):
    channel = _channel(storage)
    _send(services, seeded["account"].key, channel)
    assert telegram.joined == []


def test_auto_join_subscribes_to_the_discussion_chat_not_the_channel(
        services, storage, telegram, seeded):
    _auto_join(storage, delay=0)
    channel = _channel(storage)
    telegram.discussion[channel.username] = ("chat_for_chan", 9)

    _send(services, seeded["account"].key, channel)

    assert telegram.joined == [(seeded["account"].key, "chat_for_chan")]


def test_already_being_a_member_is_not_a_failure(
        services, storage, telegram, seeded):
    _auto_join(storage, delay=0)
    channel = _channel(storage)

    class UserAlreadyParticipantError(Exception):
        pass

    telegram.join_error = UserAlreadyParticipantError("already in")
    _send(services, seeded["account"].key, channel)

    assert len(telegram.sent) == 1, "the message still goes out"


def test_a_join_request_only_sent_is_reported_as_a_failure(
        services, storage, telegram, seeded):
    """The request is in, but we are not a member yet, so the message cannot
    land. Saying so beats a send that fails a moment later for a stranger
    reason."""
    _auto_join(storage, delay=0)
    channel = _channel(storage)

    class InviteRequestSentError(Exception):
        pass

    telegram.join_error = InviteRequestSentError("pending")

    with pytest.raises(DeliveryError) as err:
        _send(services, seeded["account"].key, channel)

    assert err.value.message["code"] == "tg.InviteRequestSentError"
    assert err.value.fault == "chat", "a refusal of this chat, counted"
    assert telegram.sent == []


# ── the same answer reaches the channel check ───────────────────────────
def test_the_channel_check_notices_comments_are_off(
        services, storage, telegram, seeded):
    channel = _channel(storage, username="silent")
    telegram.no_comments.add("silent")

    asyncio.run(services.catalog.check_target(channel))

    assert channel.last_check_error == NO_COMMENTS


def test_a_commentable_channel_checks_out_as_available(
        services, storage, telegram, seeded):
    channel = _channel(storage)
    asyncio.run(services.catalog.check_target(channel))
    assert channel.last_check is not None and channel.last_check_error is None


# ── and the campaign says it in the result row ──────────────────────────
def test_a_failed_target_carries_the_readable_reason(
        services, storage, telegram, seeded):
    channel = _channel(storage, username="silent")
    telegram.no_comments.add("silent")
    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [channel.id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0,
    })

    asyncio.run(services.scheduler._run(campaign))

    result = campaign.result_for(channel.id)
    assert result.status == TargetResultStatus.FAILED
    assert result.error == NO_COMMENTS


# ── the pause between joining and writing ───────────────────────────────
# Waited by the scheduler, between two holds of the account's lock, so
# another campaign on the account is never held up by it and Stop reaches it.
def _waits(services, monkeypatch):
    """Record what the scheduler waited for instead of actually waiting."""
    waited: list[float] = []

    async def fake_wait(c, seconds):  # noqa: ARG001
        waited.append(seconds)
        return True

    monkeypatch.setattr(services.scheduler, "_wait", fake_wait)
    return waited


def _one_channel(services, seeded, channel):
    return services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [channel.id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0,
    })


def test_the_pause_is_waited_out_after_joining(services, storage, telegram,
                                               seeded, monkeypatch):
    waited = _waits(services, monkeypatch)
    _auto_join(storage, delay=45)
    channel = _channel(storage)
    campaign = _one_channel(services, seeded, channel)

    asyncio.run(services.scheduler._run(campaign))

    assert waited == [45]
    assert telegram.joined and telegram.sent, "joined first, then sent"
    assert not services.pacer.lock(seeded["account"].id).locked()


def test_a_zero_pause_waits_for_nothing(services, storage, telegram, seeded,
                                        monkeypatch):
    waited = _waits(services, monkeypatch)
    _auto_join(storage, delay=0)
    campaign = _one_channel(services, seeded, _channel(storage))
    asyncio.run(services.scheduler._run(campaign))
    assert waited == []


def test_nothing_is_waited_for_when_subscribing_is_off(services, storage,
                                                       telegram, seeded,
                                                       monkeypatch):
    waited = _waits(services, monkeypatch)
    storage.settings.set("campaign.auto_join_delay_sec", 60)
    campaign = _one_channel(services, seeded, _channel(storage))
    asyncio.run(services.scheduler._run(campaign))
    assert waited == [], "no join, nothing to settle"


def test_an_existing_member_does_not_wait(services, storage, telegram, seeded,
                                          monkeypatch):
    """Nothing was joined, so there is nothing for anti-spam to notice."""
    waited = _waits(services, monkeypatch)
    _auto_join(storage, delay=60)

    class UserAlreadyParticipantError(Exception):
        pass

    telegram.join_error = UserAlreadyParticipantError("already in")
    campaign = _one_channel(services, seeded, _channel(storage))
    asyncio.run(services.scheduler._run(campaign))

    assert waited == []
    assert len(telegram.sent) == 1


def test_the_pause_has_a_sane_default(storage, services):
    assert storage.settings.get("campaign.auto_join_delay_sec") == 10
    assert services.delivery.join_delay == 10


def test_a_broken_setting_falls_back_instead_of_stopping_a_campaign(
        storage, services):
    storage.settings.set("campaign.auto_join_delay_sec", "не число")
    assert services.delivery.join_delay == 10, "the one default, in DEFAULTS"

    storage.settings.set("campaign.auto_join_delay_sec", -5)
    assert services.delivery.join_delay == 0
