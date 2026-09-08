"""A channel that keeps refusing stops being knocked on.

Twenty-seven "В этом чате запрещено писать" in the field came from a handful
of chats, on a loop, every cycle, for as long as the app ran. Nobody was
going to be persuaded by the twenty-eighth.
"""
from __future__ import annotations

import asyncio

from app.models import Target
from app.models.enums import CampaignState


class ChatWriteForbiddenError(Exception):
    pass


class UserBannedInChannelError(Exception):
    pass


def _campaign(services, storage, seeded, targets=1):
    ids = [seeded["target"].id]
    for i in range(targets - 1):
        extra = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra)
        ids.append(extra.id)
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": ids, "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0,
        "schedule": {"mode": "LOOP"}})


def _refuse_with(telegram, error):
    async def refuse(key, entity, text, file=None, reply_to=None):
        raise error

    telegram.send_message = refuse


def _cycle(services, campaign):
    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))


def _banned_in(telegram, chat):
    """The chat itself bars the account for good - what the check after a
    «banned» refusal finds."""
    telegram.restrictions[chat] = {"kicked": False, "send": True, "until": None}


# ── counting ────────────────────────────────────────────────────────────
def test_three_refusals_in_a_row_switch_the_channel_off(services, storage,
                                                        telegram, seeded):
    storage.settings.set("campaign.refusals_before_off", 3)
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))

    for _ in range(3):
        _cycle(services, campaign)

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.excluded is True
    assert result.refusals == 3
    assert result.excluded_reason == {"code": "excluded.refused",
                                      "params": {"n": 3}}


def test_two_refusals_are_not_enough(services, storage, telegram, seeded):
    storage.settings.set("campaign.refusals_before_off", 3)
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))

    for _ in range(2):
        _cycle(services, campaign)

    assert storage.campaigns.get(campaign.id).results[0].excluded is False


def test_one_success_clears_the_count(services, storage, telegram, seeded):
    """A chat that took a message is not refusing any more."""
    storage.settings.set("campaign.refusals_before_off", 3)
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))
    _cycle(services, campaign)
    _cycle(services, campaign)

    del telegram.send_message              # back to the working fake
    _cycle(services, campaign)

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.results[0].refusals == 0
    # a recurring pass puts its targets back to PENDING for the next cycle,
    # so what proves the send is the running total
    assert fresh.sent_total == 1


def test_a_comment_refused_before_joining_is_an_ordinary_refusal(
        services, storage, telegram, seeded):
    """A comment is tried without joining the discussion; when the chat
    refuses, that is a refusal of the chat like any other - counted."""
    storage.settings.set("campaign.refusals_before_off", 2)
    campaign = _campaign(services, storage, seeded)   # not in `members`
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))

    for _ in range(2):
        _cycle(services, campaign)

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.refusals == 2
    assert result.excluded is True


def test_zero_never_switches_anything_off(services, storage, telegram, seeded):
    storage.settings.set("campaign.refusals_before_off", 0)
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))

    for _ in range(6):
        _cycle(services, campaign)

    assert storage.campaigns.get(campaign.id).results[0].excluded is False


# ── a ban is an answer, not a count ─────────────────────────────────────
def test_a_ban_switches_the_channel_off_at_once(services, storage, telegram,
                                                seeded):
    telegram.members.add(seeded["target"].username)
    _banned_in(telegram, seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, UserBannedInChannelError("забанен"))

    _cycle(services, campaign)

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.excluded is True
    assert result.excluded_reason["code"] == "excluded.banned"


def test_a_ban_reaches_every_campaign_of_that_account_only(
        services, storage, telegram, seeded):
    """A ban belongs to the pair «account + chat»: the account's other
    campaigns stop knocking too; another account is not banned there."""
    from app.models import Account
    from app.models.enums import AccountState
    telegram.members.add(seeded["target"].username)
    _banned_in(telegram, seeded["target"].username)
    first = _campaign(services, storage, seeded)
    second = _campaign(services, storage, seeded)
    other = Account(key="session_888", telegram_id=888, username="other",
                    api_profile_id=seeded["profile"].id,
                    raw_state=AccountState.READY)
    storage.accounts.add(other)
    foreign = services.campaigns.create({
        "name": "theirs", "account_id": other.id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0})
    _refuse_with(telegram, UserBannedInChannelError("забанен"))

    _cycle(services, first)

    assert storage.campaigns.get(first.id).results[0].excluded is True
    assert storage.campaigns.get(second.id).results[0].excluded is True
    assert storage.campaigns.get(foreign.id).results[0].excluded is False


# ── what being switched off means ───────────────────────────────────────
def test_a_switched_off_channel_is_skipped_but_kept(services, storage,
                                                    telegram, seeded):
    campaign = _campaign(services, storage, seeded, targets=2)
    services.campaigns.set_target_excluded(campaign, seeded["target"].id, True)

    _cycle(services, campaign)

    fresh = storage.campaigns.get(campaign.id)
    assert len(fresh.target_ids) == 2, "still part of the campaign"
    assert len(telegram.sent) == 1, "only the other one was written to"


def test_ticking_it_back_on_starts_the_count_again(services, storage, seeded):
    campaign = _campaign(services, storage, seeded)
    campaign.results[0].refusals = 3
    services.campaigns.set_target_excluded(campaign, seeded["target"].id, True)

    services.campaigns.set_target_excluded(campaign, seeded["target"].id, False)

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.excluded is False
    assert result.refusals == 0
    assert result.excluded_reason is None


def test_resetting_the_campaign_clears_the_exclusions(services, storage,
                                                      seeded):
    campaign = _campaign(services, storage, seeded)
    services.campaigns.set_target_excluded(campaign, seeded["target"].id, True)

    services.campaigns.reset(campaign)

    assert storage.campaigns.get(campaign.id).results[0].excluded is False


def test_a_channel_the_campaign_does_not_have_is_refused(services, storage,
                                                         seeded):
    import pytest
    from app.services.campaigns import CampaignError
    campaign = _campaign(services, storage, seeded)

    with pytest.raises(CampaignError):
        services.campaigns.set_target_excluded(campaign, "target_nope", True)


# ── leaving ─────────────────────────────────────────────────────────────
def test_the_account_leaves_a_chat_it_is_banned_in(services, storage,
                                                   telegram, seeded):
    """Only a ban in the chat itself: whether the app joined it or not."""
    telegram.members.add(seeded["target"].username)
    _banned_in(telegram, seeded["target"].username)
    campaign = _campaign(services, storage, seeded)   # auto_join is off
    _refuse_with(telegram, UserBannedInChannelError("забанен"))

    _cycle(services, campaign)

    assert telegram.left == [(seeded["account"].key, seeded["target"].username)]


def test_no_other_refusal_ever_leaves_a_chat(services, storage, telegram,
                                            seeded):
    storage.settings.update({"campaign.auto_join": True,
                             "campaign.auto_join_delay_sec": 0,
                             "campaign.refusals_before_off": 1})
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)
    _refuse_with(telegram, ChatWriteForbiddenError("нельзя писать"))

    _cycle(services, campaign)

    assert storage.campaigns.get(campaign.id).results[0].excluded is True
    assert telegram.left == [], "switched off in the campaign, still in the chat"
