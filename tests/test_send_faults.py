"""What a failed send means, and what happens because of it.

The incident behind this file: an account Telegram had barred from public
groups got «UserBannedInChannelError» from the typing indicator in every
chat. The app took each one for a ban in that chat, walked out of the
user's own test groups, counted every one towards the safety net and
switched the account off again after each manual switch-on.

The rules now: the indicator never decides anything; one classification
(telegram/errors.py) decides what a refusal means; a «banned» refusal is
checked against the chat before anything is concluded.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.models import Account, Target
from app.models.enums import AccountState, CampaignState, TargetResultStatus
from app.services import campaigns as campaigns_module
from app.telegram.errors import Fault, classify


# ── Telethon's errors, by the names that matter ─────────────────────────
class UserBannedInChannelError(Exception):
    pass


class ChatWriteForbiddenError(Exception):
    pass


class UsernameNotOccupiedError(Exception):
    pass


class ReactionInvalidError(Exception):
    pass


class SlowModeWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"wait {seconds}s")
        self.seconds = seconds


class FloodWaitError(SlowModeWaitError):
    pass


BANNED_FOR_GOOD = {"kicked": False, "send": True, "until": None}


# ── helpers ─────────────────────────────────────────────────────────────
def _target(storage, username, telegram=None, group=False, member=True):
    target = Target(title=username.title(), username=username)
    storage.targets.add(target)
    if telegram is not None:
        if group:
            telegram.kinds[username] = "GROUP"
        if member:
            telegram.members.add(username)
    return target


def _campaign(services, account, targets, name="blast"):
    return services.campaigns.create({
        "name": name, "account_id": account.id,
        "target_ids": [t.id for t in targets], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0,
        "schedule": {"mode": "LOOP"}})


def _run(services, *campaigns):
    async def go():
        for c in campaigns:
            c.raw_state = CampaignState.SCHEDULED
        await asyncio.gather(*(services.scheduler._run(c) for c in campaigns))
    asyncio.run(go())


def _slow_typing(monkeypatch, seconds=3.0):
    """Typing that would take a while, with the clock taken out of it."""
    from app.services import presence
    monkeypatch.setattr(presence, "TYPING_MIN_SEC", seconds)
    monkeypatch.setattr(presence, "TYPING_MAX_SEC", seconds)
    real = asyncio.sleep

    async def no_wait(_seconds):
        await real(0)

    monkeypatch.setattr(presence.asyncio, "sleep", no_wait)


def _guard(storage, percent=30, minimum=3):
    storage.settings.update({"campaign.error_stop_percent": percent,
                             "campaign.error_stop_min": minimum})


def _second_account(storage, seeded, tid=888):
    other = Account(key=f"session_{tid}", telegram_id=tid, username=f"acc{tid}",
                    api_profile_id=seeded["profile"].id,
                    raw_state=AccountState.READY)
    storage.accounts.add(other)
    return other


def _result(storage, campaign, target):
    return storage.campaigns.get(campaign.id).result_for(target.id)


# ── the classification itself ───────────────────────────────────────────
@pytest.mark.parametrize("error, fault", [
    (UserBannedInChannelError(), Fault.BANNED),
    (SlowModeWaitError(30), Fault.SLOW_MODE),
    (FloodWaitError(30), Fault.FLOOD),
    (UsernameNotOccupiedError(), Fault.NOT_FOUND),
    (ValueError('No user has "gone" as username'), Fault.NOT_FOUND),
    (ValueError("Could not find the input entity for PeerChannel(1)"), Fault.CHAT),
    (ChatWriteForbiddenError(), Fault.CHAT),
    (ConnectionError("proxy down"), Fault.ACCOUNT),
    (RuntimeError("AuthKeyUnregisteredError: dead"), Fault.DEAD),
])
def test_one_table_decides_what_an_error_means(error, fault):
    assert classify(error) == fault


# ── 1, 2: the indicator decides nothing ─────────────────────────────────
def test_a_failed_indicator_does_not_touch_a_send_that_went(
        services, storage, telegram, seeded, monkeypatch):
    _slow_typing(monkeypatch)
    _guard(storage, minimum=1)
    telegram.typing_error = UserBannedInChannelError("the indicator")
    target = seeded["target"]
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    result = _result(storage, campaign, target)
    assert len(telegram.sent) == 1 and storage.campaigns.get(campaign.id).sent_total == 1
    assert result.error is None and result.refusals == 0
    assert not result.excluded
    assert not storage.accounts.get(seeded["account"].id).disabled
    assert telegram.restriction_asks == [], "nothing to check: nothing failed"


def test_the_transport_sends_without_telethons_background_action(
        monkeypatch):
    """`client.action` ran the indicator as a task whose failure surfaced
    after the block, in place of the send's own result."""
    from app.telegram import service as service_module
    from app.telegram.service import TelegramService

    class Client:
        requests = []

        async def is_user_authorized(self):
            return True

        async def send_message(self, entity, text, reply_to=None):
            return "sent"

        def action(self, *args, **kwargs):
            raise AssertionError("client.action is not used")

        async def __call__(self, request):
            self.requests.append(type(request).__name__)

    client = Client()
    tg = TelegramService()

    async def client_for(key):
        return client

    monkeypatch.setattr(tg, "_client_for", client_for)
    monkeypatch.setattr(service_module, "describe_message", lambda m, *_: m)

    assert asyncio.run(tg.send_message("k", "chat", "hi")) == "sent"
    asyncio.run(tg.set_typing("k", "chat"))
    assert client.requests == ["SetTypingRequest"], "one request, nothing left running"


def test_when_both_fail_the_send_is_what_is_judged(services, storage,
                                                   telegram, seeded):
    telegram.typing_error = UserBannedInChannelError("the indicator")
    target = seeded["target"]
    telegram.send_errors[target.username] = ChatWriteForbiddenError("no")
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    result = _result(storage, campaign, target)
    assert result.error["code"] == "tg.ChatWriteForbiddenError"
    assert result.refusals == 1, "a refusal of the chat, counted as one"
    assert telegram.restriction_asks == [], "not taken for a ban"
    assert not storage.accounts.get(seeded["account"].id).disabled


# ── 3: banned in the chat itself ────────────────────────────────────────
def test_a_confirmed_ban_switches_the_chat_off_for_the_whole_account(
        services, storage, telegram, seeded):
    _guard(storage, minimum=1)
    account = seeded["account"]
    target = _target(storage, "banned_grp", telegram, group=True)
    telegram.send_errors["banned_grp"] = UserBannedInChannelError("banned")
    telegram.restrictions["banned_grp"] = BANNED_FOR_GOOD
    first = _campaign(services, account, [target])
    second = _campaign(services, account, [target], name="other")

    _run(services, first)

    assert _result(storage, first, target).excluded
    assert _result(storage, second, target).excluded, "the pair, not one campaign"
    assert _result(storage, first, target).excluded_reason["code"] == "excluded.banned"
    assert telegram.left == [(account.key, "banned_grp")]
    assert not storage.accounts.get(account.id).disabled
    assert services.pacer.error_rate_exceeded(account.id) is None


def test_a_banned_commenter_leaves_the_discussion_and_the_channel(
        services, storage, telegram, seeded):
    account = seeded["account"]
    channel = _target(storage, "news", telegram)
    telegram.discussion["news"] = ("news_chat", 9)
    telegram.members.add("news_chat")
    telegram.send_errors["news_chat"] = UserBannedInChannelError("banned")
    telegram.restrictions["news_chat"] = BANNED_FOR_GOOD
    campaign = _campaign(services, account, [channel])

    _run(services, campaign)

    assert set(telegram.left) == {(account.key, "news_chat"), (account.key, "news")}
    asked = telegram.restriction_asks[0][1]
    assert set(asked) == {"news_chat", "news"}, "both asked, in one request"


# ── 4: barred from public groups ────────────────────────────────────────
def test_a_ban_the_chat_does_not_know_about_is_the_account(
        services, storage, telegram, seeded):
    account = seeded["account"]
    targets = [_target(storage, f"grp{i}", telegram, group=True) for i in range(3)]
    for t in targets:
        telegram.send_errors[t.username] = UserBannedInChannelError("banned")
    campaign = _campaign(services, account, targets)
    other = _campaign(services, account, targets, name="other")
    services.campaigns.start(other)

    _run(services, campaign)

    fresh = storage.accounts.get(account.id)
    assert fresh.disabled
    assert fresh.stop_note["code"] == "note.public_groups"
    assert storage.campaigns.get(campaign.id).raw_state == CampaignState.PAUSED
    assert storage.campaigns.get(other.id).raw_state == CampaignState.PAUSED
    assert not any(r.excluded for r in storage.campaigns.get(campaign.id).results)
    assert telegram.left == []
    assert len(telegram.restriction_asks) == 1, "switched off once, then stopped"


# ── 5: muted for a while ────────────────────────────────────────────────
def test_a_temporary_mute_skips_the_chat_until_it_ends(services, storage,
                                                       telegram, seeded):
    from datetime import timezone
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    target = _target(storage, "muted_grp", telegram, group=True)
    telegram.send_errors["muted_grp"] = UserBannedInChannelError("banned")
    telegram.restrictions["muted_grp"] = {"kicked": False, "send": True,
                                          "until": until}
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    result = _result(storage, campaign, target)
    assert result.status == TargetResultStatus.SKIPPED
    assert result.error["code"] == "result.muted"
    assert not result.excluded
    assert telegram.left == []
    retry = datetime.strptime(result.retry_at, "%Y-%m-%dT%H:%M:%S")
    assert abs(retry - until.astimezone().replace(tzinfo=None)) < timedelta(seconds=2)


@pytest.mark.parametrize("row, waited", [
    ({"kicked": False, "send": True, "days": 3}, True),
    ({"kicked": True, "send": True, "days": 2}, True),
    ({"kicked": False, "send": True, "days": 10}, False),
    ({"kicked": True, "send": True, "days": 30}, False),
])
def test_a_week_is_the_longest_restriction_worth_waiting_out(
        services, storage, telegram, seeded, row, waited):
    """Muted or thrown out for up to seven days: skipped until then. Longer:
    banned - the chat is given up for this account."""
    from datetime import timezone
    until = datetime.now(timezone.utc) + timedelta(days=row["days"])
    account = seeded["account"]
    target = _target(storage, "grp", telegram, group=True)
    telegram.send_errors["grp"] = UserBannedInChannelError("banned")
    telegram.restrictions["grp"] = {"kicked": row["kicked"], "send": row["send"],
                                    "until": until}
    campaign = _campaign(services, account, [target])
    other = _campaign(services, account, [target], name="other")

    _run(services, campaign)

    result = _result(storage, campaign, target)
    if waited:
        assert result.status == TargetResultStatus.SKIPPED
        assert not result.excluded and telegram.left == []
        assert _result(storage, other, target).retry_at == result.retry_at
    else:
        assert result.excluded and result.excluded_reason["code"] == "excluded.banned"
        assert _result(storage, other, target).excluded
        assert telegram.left == [(account.key, "grp")]
    assert not storage.accounts.get(account.id).disabled


def test_a_wait_on_another_day_names_the_day():
    from app.util import until_text
    now = datetime.now().replace(hour=12, minute=0)
    assert until_text(now) == "12:00"
    assert until_text(now + timedelta(days=2)) ==         (now + timedelta(days=2)).strftime("%d.%m 12:00")


# ── 6: slow mode ────────────────────────────────────────────────────────
def test_slow_mode_skips_one_chat_and_the_pass_goes_on(services, storage,
                                                       telegram, seeded):
    _guard(storage, minimum=1)
    slow = _target(storage, "slow_grp", telegram, group=True)
    fine = _target(storage, "fine_grp", telegram, group=True)
    telegram.send_errors["slow_grp"] = SlowModeWaitError(600)
    campaign = _campaign(services, seeded["account"], [slow, fine])

    _run(services, campaign)

    assert [e for _k, e, _t in telegram.sent] == ["fine_grp"]
    result = _result(storage, campaign, slow)
    assert result.status == TargetResultStatus.SKIPPED, "«Пропущено», not «Ошибка»"
    assert result.error["code"] == "result.slow_mode"
    assert services.pacer.error_rate_exceeded(seeded["account"].id) is None


def test_another_campaign_skips_the_slow_chat_without_waiting(
        services, storage, telegram, seeded):
    account = seeded["account"]
    slow = _target(storage, "slow_grp", telegram, group=True)
    telegram.send_errors["slow_grp"] = SlowModeWaitError(600)
    first = _campaign(services, account, [slow])
    second = _campaign(services, account, [slow], name="other")
    _run(services, first)
    del telegram.send_errors["slow_grp"]

    _run(services, second)

    assert telegram.sent == [], "the second one never knocked"
    assert _result(storage, second, slow).status == TargetResultStatus.SKIPPED


def test_after_slow_mode_runs_out_the_next_pass_writes(services, storage,
                                                       telegram, seeded):
    slow = _target(storage, "slow_grp", telegram, group=True)
    telegram.send_errors["slow_grp"] = SlowModeWaitError(600)
    campaign = _campaign(services, seeded["account"], [slow])
    _run(services, campaign)
    del telegram.send_errors["slow_grp"]
    result = _result(storage, campaign, slow)
    result.retry_at = (datetime.now() - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S")

    _run(services, campaign)

    assert [e for _k, e, _t in telegram.sent] == ["slow_grp"]
    result = _result(storage, campaign, slow)
    assert result.retry_at is None and result.error is None, "written, wait over"


# ── 7: flood wait ───────────────────────────────────────────────────────
def test_a_flood_wait_holds_the_account_and_then_carries_on(
        services, storage, telegram, seeded, monkeypatch):
    _guard(storage, minimum=1)
    account = seeded["account"]
    target = _target(storage, "grp", telegram, group=True)
    telegram.send_errors["grp"] = FloodWaitError(1800)
    waited = []

    async def fake_wait(c, seconds):
        waited.append(seconds)
        telegram.send_errors.pop("grp", None)
        account.flood_until = None          # the wait ran out
        return True

    monkeypatch.setattr(services.scheduler, "_wait", fake_wait)
    campaign = _campaign(services, account, [target])

    _run(services, campaign)

    assert waited and waited[0] > 1700, "waited out what Telegram named"
    assert [e for _k, e, _t in telegram.sent] == ["grp"], "and then sent"
    assert storage.campaigns.get(campaign.id).sent_total == 1
    assert services.pacer.error_rate_exceeded(account.id) is None


def test_a_flood_wait_is_on_the_account_and_survives_a_restart(
        services, storage, telegram, seeded):
    from app.storage import Storage
    account = seeded["account"]
    from app.services.probing import hold_for_flood
    hold_for_flood(account, storage, None, 1800)

    reloaded = Storage().accounts.get(account.id)

    assert reloaded.flood_left() > 1700
    view = services.state.account_view(reloaded)
    assert view["effective"] == "WAITING"
    assert any(i["code"] == "account.flood_wait" for i in view["issues"])


def test_a_waiting_account_defers_its_campaigns_instead_of_stopping_them(
        services, storage, telegram, seeded):
    account = seeded["account"]
    account.flood_until = (datetime.now() + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    campaign = _campaign(services, account, [seeded["target"]])

    _run(services, campaign)

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.raw_state == CampaignState.SCHEDULED, "armed, not paused"
    assert telegram.sent == []


def test_stop_ends_a_flood_wait_at_once(services, storage, telegram, seeded,
                                        monkeypatch):
    monkeypatch.setattr(campaigns_module, "TICK_SEC", 0.02)
    account = seeded["account"]
    target = _target(storage, "grp", telegram, group=True)
    telegram.send_errors["grp"] = FloodWaitError(3600)
    campaign = _campaign(services, account, [target])

    async def go():
        campaign.raw_state = CampaignState.SCHEDULED
        run = asyncio.ensure_future(services.scheduler._run(campaign))
        await asyncio.sleep(0.1)
        assert not services.pacer.lock(account.id).locked(), \
            "nobody holds the account while it waits"
        services.campaigns.pause(campaign)
        await asyncio.wait_for(run, timeout=1.0)

    asyncio.run(go())

    assert telegram.sent == []
    assert storage.campaigns.get(campaign.id).raw_state == CampaignState.PAUSED


def test_an_auto_reply_waits_for_the_flood_to_end(services, storage, telegram,
                                                  seeded, monkeypatch):
    from app.services import autoreply as autoreply_module
    account = seeded["account"]
    account.flood_until = (datetime.now() + timedelta(minutes=20)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if seconds > 1000:
            account.flood_until = None      # the wait ran out

    monkeypatch.setattr(autoreply_module.asyncio, "sleep", fake_sleep)
    services.autoreply.save_config("global", {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FIRST_MESSAGE", "enabled": True, "match": "",
                   "response": "Здравствуйте!"}]})
    services.responder._running = True

    asyncio.run(services.responder._handle(_incoming(account)))

    assert any(s > 1000 for s in slept), "held until the wait was over"
    assert len(telegram.sent) == 1


# ── 8: the address leads nowhere ────────────────────────────────────────
def test_a_chat_that_does_not_exist_is_switched_off_everywhere(
        services, storage, telegram, seeded):
    _guard(storage, minimum=1)
    gone = _target(storage, "gone_chat")
    telegram.unresolvable.add("gone_chat")
    other = _second_account(storage, seeded)
    mine = _campaign(services, seeded["account"], [gone])
    theirs = _campaign(services, other, [gone], name="theirs")

    _run(services, mine)

    assert _result(storage, mine, gone).excluded
    assert _result(storage, theirs, gone).excluded, "every account's campaigns"
    assert storage.targets.get(gone.id).last_check_error["code"] == "delivery.not_found"
    assert services.pacer.error_rate_exceeded(seeded["account"].id) is None


# ── 9, 10: membership ───────────────────────────────────────────────────
def test_not_in_the_group_with_joining_off_sends_nothing(
        services, storage, telegram, seeded):
    target = _target(storage, "strangers", telegram, group=True, member=False)
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    assert telegram.sent == []
    result = _result(storage, campaign, target)
    assert result.excluded
    assert result.excluded_reason["code"] == "excluded.not_member"


def test_after_joining_the_account_counts_as_a_member(services, storage,
                                                     telegram, seeded):
    storage.settings.update({"campaign.auto_join": True,
                             "campaign.auto_join_delay_sec": 0})
    target = _target(storage, "fresh", telegram, group=True, member=False)
    telegram.send_errors["fresh"] = ChatWriteForbiddenError("no")
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    assert telegram.joined == [(seeded["account"].key, "fresh")]
    assert _result(storage, campaign, target).refusals == 1, \
        "counted: the account is in the chat now"


def test_a_comment_needing_the_discussion_joined_is_marked_on_the_channel(
        services, storage, telegram, seeded):
    class ChatGuestSendForbiddenError(Exception):
        pass

    channel = _target(storage, "news", telegram, member=False)
    telegram.discussion["news"] = ("news_chat", 9)
    telegram.send_errors["news_chat"] = ChatGuestSendForbiddenError("join first")
    other = _second_account(storage, seeded)
    mine = _campaign(services, seeded["account"], [channel])
    theirs = _campaign(services, other, [channel], name="theirs")

    _run(services, mine)

    assert _result(storage, mine, channel).excluded
    assert _result(storage, theirs, channel).excluded
    assert storage.targets.get(channel.id).last_check_error["code"] == \
        "delivery.join_to_comment"


# ── 11, 12: the safety net ──────────────────────────────────────────────
def test_switching_on_by_hand_starts_the_count_afresh(services, storage,
                                                     telegram, seeded):
    _guard(storage, percent=30, minimum=3)
    account = seeded["account"]
    targets = [_target(storage, f"g{i}", telegram, group=True) for i in range(4)]
    for t in targets:
        telegram.send_errors[t.username] = ConnectionError("proxy down")
    _run(services, _campaign(services, account, targets[:3]))
    assert storage.accounts.get(account.id).disabled

    services.accounts.set_disabled(account, False)
    _run(services, _campaign(services, account, targets[3:], name="again"))

    assert not storage.accounts.get(account.id).disabled, \
        "one new failure after switching on is not a pattern"


def test_only_the_account_or_the_network_trips_the_guard(services, storage,
                                                         telegram, seeded):
    _guard(storage, percent=30, minimum=2)
    account = seeded["account"]
    refusing = [_target(storage, f"r{i}", telegram, group=True) for i in range(4)]
    for t in refusing:
        telegram.send_errors[t.username] = ChatWriteForbiddenError("no")
    _run(services, _campaign(services, account, refusing))
    assert not storage.accounts.get(account.id).disabled

    broken = [_target(storage, f"n{i}", telegram, group=True) for i in range(2)]
    for t in broken:
        telegram.send_errors[t.username] = ConnectionError("proxy down")
    _run(services, _campaign(services, account, broken, name="net"))
    assert storage.accounts.get(account.id).disabled


def test_one_chat_failing_in_two_campaigns_counts_once(services, storage,
                                                       telegram, seeded):
    _guard(storage, percent=30, minimum=2)
    account = seeded["account"]
    target = _target(storage, "shared", telegram, group=True)
    telegram.send_errors["shared"] = ConnectionError("proxy down")

    _run(services, _campaign(services, account, [target]),
         _campaign(services, account, [target], name="twin"))

    assert services.pacer.error_rate_exceeded(account.id) is None, \
        "one failure, below the minimum of two"
    assert not storage.accounts.get(account.id).disabled


# ── 13: switched off on the channels page ───────────────────────────────
def test_a_channel_switched_off_on_its_page_gets_nothing(services, storage,
                                                         telegram, seeded):
    target = _target(storage, "off_grp", telegram, group=True)
    target.active = False
    storage.targets.upsert(target)
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    assert telegram.sent == []


# ── 14: several campaigns on one account ────────────────────────────────
def test_a_chat_switched_off_by_one_campaign_is_skipped_by_another_mid_run(
        services, storage, telegram, seeded):
    account = seeded["account"]
    banned = _target(storage, "banned_grp", telegram, group=True)
    telegram.send_errors["banned_grp"] = UserBannedInChannelError("banned")
    telegram.restrictions["banned_grp"] = BANNED_FOR_GOOD
    first = _campaign(services, account, [banned])
    second = _campaign(services, account, [banned], name="second")

    _run(services, first, second)

    sends = [e for _k, e, _t in telegram.sent]
    assert sends == []
    asks = [a for a in telegram.restriction_asks]
    assert len(asks) == 1, "the second found it switched off and never knocked"


def test_an_account_switched_off_mid_run_stops_its_other_campaign(
        services, storage, telegram, seeded):
    account = seeded["account"]
    barred = [_target(storage, f"pub{i}", telegram, group=True) for i in range(2)]
    for t in barred:
        telegram.send_errors[t.username] = UserBannedInChannelError("banned")
    first = _campaign(services, account, barred)
    second = _campaign(services, account, list(reversed(barred)), name="second")

    _run(services, first, second)

    assert storage.accounts.get(account.id).disabled
    for c in (first, second):
        assert storage.campaigns.get(c.id).raw_state == CampaignState.PAUSED
    assert len(telegram.restriction_asks) == 1


def test_the_lock_is_never_held_across_the_pause_after_joining(
        services, storage, telegram, seeded, monkeypatch):
    storage.settings.update({"campaign.auto_join": True,
                             "campaign.auto_join_delay_sec": 30})
    account = seeded["account"]
    target = _target(storage, "fresh", telegram, group=True, member=False)
    held = []

    async def fake_wait(c, seconds):
        held.append(services.pacer.lock(account.id).locked())
        return True

    monkeypatch.setattr(services.scheduler, "_wait", fake_wait)
    _run(services, _campaign(services, account, [target]))

    assert held == [False]
    assert len(telegram.sent) == 1


# ── the reaction goes first, and can answer for the message ─────────────
def _with_reactions(storage):
    storage.settings.update({"campaign.reactions": True,
                             "campaign.reaction_percent": 100})


def test_a_reaction_refused_with_a_ban_is_checked_before_writing(
        services, storage, telegram, seeded):
    _with_reactions(storage)
    target = _target(storage, "grp", telegram, group=True)
    telegram.react_error = UserBannedInChannelError("banned")
    campaign = _campaign(services, seeded["account"], [target])

    _run(services, campaign)

    assert telegram.sent == [], "the message was never sent"
    fresh = storage.accounts.get(seeded["account"].id)
    assert fresh.disabled and fresh.stop_note["code"] == "note.public_groups"


def test_a_reaction_the_chat_just_does_not_take_is_ignored(
        services, storage, telegram, seeded):
    _with_reactions(storage)
    telegram.react_error = ReactionInvalidError("not here")
    campaign = _campaign(services, seeded["account"], [seeded["target"]])

    _run(services, campaign)

    assert len(telegram.sent) == 1


# ── 15: an auto-reply is not sent twice over the indicator ──────────────
def _incoming(account, peer=55):
    return {"account_key": account.key, "peer_id": peer, "private": True,
            "sender_id": peer, "sender_name": "Гость", "sender_username": None,
            "sender_bot": False, "message_id": 1, "text": "привет",
            "media": None, "reply_to": None, "date": "", "out": False}


def test_a_failed_indicator_does_not_make_an_auto_reply_go_twice(
        services, storage, telegram, seeded, monkeypatch):
    _slow_typing(monkeypatch)
    telegram.typing_error = UserBannedInChannelError("the indicator")
    services.autoreply.save_config("global", {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FIRST_MESSAGE", "enabled": True, "match": "",
                   "response": "Здравствуйте!"}]})
    services.responder._running = True
    account = seeded["account"]

    asyncio.run(services.responder._handle(_incoming(account)))
    asyncio.run(services.responder._handle(_incoming(account)))

    assert len(telegram.sent) == 1, "answered once, remembered as answered"
    assert storage.conversation(account.id, 55).first_replied_at
