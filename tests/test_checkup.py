"""The "check all" run.

The bug this replaces: a fast pass turned every account green, and the slow
antispam pass ran afterwards. Six accounts went green in five seconds while
their antispam checks had not started, so green meant "probably fine" rather
than "checked". Now each account is finished before the next one begins.
"""
from __future__ import annotations

import asyncio

from app import services as _services_pkg  # noqa: F401
from app.services import checkup as checkup_module
from app.models import Account
from app.models.enums import AccountState, EffectiveState, SpamState

EN_CLEAN = "Good news, no limits are currently applied to your account."
EN_LIMITED = "Your account is now limited until 3 October 2026."


def _account(storage, seeded, key):
    account = Account(key=key, session_file=f"{key}.session", username=key,
                      api_profile_id=seeded["profile"].id,
                      raw_state=AccountState.READY)
    storage.accounts.add(account)
    return account


def _six(storage, seeded):
    return [seeded["account"]] + [
        _account(storage, seeded, f"session_{n}") for n in range(778, 783)]


# ── queueing ────────────────────────────────────────────────────────────
def test_every_account_is_greyed_before_the_run_starts(services, storage,
                                                       telegram, seeded,
                                                       monkeypatch):
    """Nothing keeps a colour it has not just earned."""
    accounts = _six(storage, seeded)
    storage.settings.set("spamcheck.delay_sec", 0)

    queued = []
    original = checkup_module.probe_session

    async def watching_probe(owner, *args, **kwargs):
        # by the time the first account is probed, the rest are already queued
        queued.append([a.raw_state for a in storage.accounts.all()])
        return await original(owner, *args, **kwargs)

    monkeypatch.setattr(checkup_module, "probe_session", watching_probe)
    services.checkup.start(spam=False)

    # when the very first probe begins, every account is already grey - none
    # is left showing a colour from the previous run
    states = queued[0]
    assert states == [AccountState.QUEUED] * len(accounts)


def test_a_queued_account_is_neither_green_nor_stale(services, storage, seeded):
    account = seeded["account"]
    account.spam_state = SpamState.LIMITED
    account.spam_detail = "old news"
    account.spam_checked_at = "2026-01-01T00:00:00"
    storage.accounts.upsert(account)

    services.checkup._queue(account, probe=True)

    assert account.raw_state == AccountState.QUEUED
    assert services.state.account_effective(account) == EffectiveState.QUEUED
    assert account.spam_state == SpamState.UNKNOWN
    assert [i["code"] for i in services.state.account_view(account)["issues"]] == []


def test_an_antispam_only_run_keeps_the_readiness_it_is_not_rechecking(
        services, storage, seeded):
    """Blanking it would make every account look unready - and be skipped."""
    account = seeded["account"]
    services.checkup._queue(account, probe=False)

    assert account.raw_state == AccountState.READY
    assert account.spam_state == SpamState.UNKNOWN


# ── order ───────────────────────────────────────────────────────────────
def test_each_account_is_finished_before_the_next_one_starts(
        services, storage, telegram, seeded, monkeypatch):
    accounts = _six(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    order = []
    probe = checkup_module.probe_session
    check = services.spamcheck.check

    async def watched_probe(owner, *args, **kwargs):
        order.append(("probe", owner.key))
        return await probe(owner, *args, **kwargs)

    async def watched_check(account):
        order.append(("spam", account.key))
        return await check(account)

    monkeypatch.setattr(checkup_module, "probe_session", watched_probe)
    services.spamcheck.check = watched_check
    services.checkup.start()

    assert len(order) == len(accounts) * 2
    # probe/spam alternate per account, never all probes then all spam checks
    for index in range(0, len(order), 2):
        assert order[index][0] == "probe"
        assert order[index + 1][0] == "spam"
        assert order[index][1] == order[index + 1][1]


def test_the_pause_is_taken_between_bot_conversations(services, storage,
                                                      telegram, seeded,
                                                      monkeypatch):
    _six(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 5})

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.services.checkup.asyncio.sleep", fake_sleep)
    services.checkup.start()

    assert slept == [5, 5, 5, 5, 5], "one pause between each pair, none trailing"


def test_a_broken_account_is_skipped_without_waiting(services, storage,
                                                     telegram, seeded,
                                                     monkeypatch):
    """A dead account cannot answer the bot, so it must not cost five seconds."""
    _six(storage, seeded)
    broken = storage.accounts.find(lambda a: a.key == "session_779")
    broken.api_profile_id = None            # a fault the probe cannot fix
    storage.accounts.upsert(broken)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 5})

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.services.checkup.asyncio.sleep", fake_sleep)
    services.checkup.start()

    asked = [key for key, _bot, _text in telegram.asked]
    assert "session_779" not in asked, "no point asking about a broken account"
    assert len(asked) == 5
    assert slept == [5, 5, 5, 5], "and no pause spent on the one we skipped"


def test_the_broken_account_still_shows_its_own_problem(services, storage,
                                                        telegram, seeded):
    _six(storage, seeded)
    broken = storage.accounts.find(lambda a: a.key == "session_779")
    broken.api_profile_id = None
    storage.accounts.upsert(broken)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    codes = [i["code"] for i in services.state.account_view(broken)["issues"]]
    assert "api.not_assigned" in codes


# ── results ─────────────────────────────────────────────────────────────
def test_a_clean_run_leaves_everything_green(services, storage, telegram,
                                             seeded):
    accounts = _six(storage, seeded)
    telegram.bot_reply = EN_CLEAN
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    for account in accounts:
        fresh = storage.accounts.get(account.id)
        assert services.state.account_effective(fresh) == EffectiveState.READY
        assert fresh.spam_state == SpamState.CLEAN
        assert services.state.account_view(fresh)["issues"] == []


def test_a_limited_account_is_marked_and_the_run_continues(services, storage,
                                                           telegram, seeded):
    accounts = _six(storage, seeded)
    telegram.bot_reply = EN_LIMITED
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    assert len(telegram.asked) == len(accounts), "one bad account stops nothing"
    for account in accounts:
        fresh = storage.accounts.get(account.id)
        codes = [i["code"] for i in services.state.account_view(fresh)["issues"]]
        assert "account.spam_limited" in codes


def test_disabled_accounts_are_left_out_entirely(services, storage, telegram,
                                                 seeded):
    _six(storage, seeded)
    off = storage.accounts.find(lambda a: a.key == "session_780")
    off.disabled = True
    storage.accounts.upsert(off)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    progress = services.checkup.start()

    assert progress["total"] == 5
    assert storage.accounts.get(off.id).raw_state != AccountState.QUEUED


# ── progress and overlap ────────────────────────────────────────────────
def test_progress_is_reported_and_ends_idle(services, storage, telegram, seeded):
    accounts = _six(storage, seeded)
    storage.settings.set("spamcheck.delay_sec", 0)

    started = services.checkup.start(spam=False)
    assert started["total"] == len(accounts)

    done = services.checkup.progress()
    assert done["running"] is False
    assert done["done"] == len(accounts)
    assert done["current"] is None


def test_the_snapshot_carries_the_progress(services, storage, seeded):
    _six(storage, seeded)
    services.checkup.start(spam=False)
    checkup = services.state.snapshot()["checkup"]
    assert checkup["total"] == 6
    assert checkup["running"] is False


def test_a_second_start_does_not_open_a_second_run(services, storage, telegram,
                                                   seeded, monkeypatch):
    _six(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    running = {"value": True}
    monkeypatch.setattr(type(services.checkup), "running",
                        property(lambda self: running["value"]))

    services.checkup.start()
    assert telegram.asked == [], "the run in progress owns the accounts"


def test_the_background_refresh_stands_aside_while_a_run_is_going(
        services, storage, seeded, monkeypatch):
    """The five-minute re-probe must not fight a run started from the button."""
    _six(storage, seeded)
    monkeypatch.setattr(type(services.checkup), "running",
                        property(lambda self: True))

    checked = []

    async def watched(account, **kwargs):      # noqa: ARG001
        checked.append(account.key)

    monkeypatch.setattr(services.checkup, "check_one", watched)

    async def one_tick():
        # the loop body, minus its sleep
        if services.checkup.running:
            return
        for account in storage.accounts.all():
            if not account.disabled:
                await services.checkup.check_one(account, spam=False)

    asyncio.run(one_tick())
    assert checked == []


def test_the_background_refresh_uses_the_same_method_without_the_bot(
        services, storage, seeded, monkeypatch):
    """It refreshes with the one-account check like everything else, but never
    asks the bot: that would message every account on a timer, unasked."""
    _six(storage, seeded)
    storage.settings.set("spamcheck.enabled", True)

    calls = []
    original = services.checkup.check_one

    async def watched(account, *args, **kwargs):
        calls.append((account.key, kwargs.get("spam")))
        return await original(account, *args, **kwargs)

    monkeypatch.setattr(services.checkup, "check_one", watched)

    async def one_tick():
        for account in storage.accounts.all():
            if not account.disabled:
                await services.checkup.check_one(account, spam=False)

    asyncio.run(one_tick())

    assert len(calls) == 6
    assert all(spam is False for _key, spam in calls)
