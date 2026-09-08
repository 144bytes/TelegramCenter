"""The antispam check.

The classifier is the part that can quietly go wrong, so it is pinned against
the wording @SpamBot actually uses. The rule that matters most: an answer we do
not understand is never reported as "clean".
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.enums import SpamState
from app.services.spamcheck import classify, tidy

# What the bot really replies with. Kept verbatim so a change in our matching
# has to be a deliberate decision, not a silent regression.
EN_CLEAN = ("Good news, no limits are currently applied to your account. "
            "You’re free as a bird!")          # note: typographic apostrophe
RU_CLEAN_CURRENT = "Ваш аккаунт свободен от каких-либо ограничений."
EN_LIMITED = (
    "I'm very sorry that you had to contact me. Unfortunately, some phone "
    "numbers may trigger a harsh response from our anti-spam systems. "
    "Your account was limited until 14 September 2026.")
EN_LIMITED_SHORT = "Your account is now limited until 3 October 2026."
RU_CLEAN = ("Хорошие новости, никаких ограничений на ваш аккаунт сейчас "
            "не наложено. Свободны как птица!")
RU_LIMITED = ("Мне очень жаль, что вам пришлось обратиться ко мне. "
              "К сожалению, некоторые номера телефонов могут вызвать жёсткую "
              "реакцию наших антиспам-систем. Ваш аккаунт ограничен до "
              "14 сентября 2026 года.")


# ── classification ──────────────────────────────────────────────────────
@pytest.mark.parametrize("reply", [EN_CLEAN, RU_CLEAN, RU_CLEAN_CURRENT])
def test_a_clean_answer_reads_as_clean(reply):
    assert classify(reply) == SpamState.CLEAN


def test_typographic_apostrophes_do_not_break_matching():
    """Telegram writes "You’re" and "I’m", not "You're" and "I'm"."""
    assert classify("You’re free as a bird!") == SpamState.CLEAN
    assert classify("I’m afraid I can’t help you here.") == SpamState.LIMITED


@pytest.mark.parametrize("reply", [EN_LIMITED, EN_LIMITED_SHORT, RU_LIMITED])
def test_a_limited_answer_reads_as_limited(reply):
    assert classify(reply) == SpamState.LIMITED


def test_the_clean_russian_answer_is_not_read_as_a_limit():
    """It says "свободен от каких-либо ограничений" - the same word a limit
    notice uses. A marker shorter than a whole clause would flip the verdict,
    which is exactly the bug this pins down."""
    assert classify(RU_CLEAN_CURRENT) == SpamState.CLEAN
    assert classify(RU_CLEAN) == SpamState.CLEAN


@pytest.mark.parametrize("reply", ["", "   ", "Привет!", "Something new",
                                   "Please use the buttons below"])
def test_an_answer_we_do_not_understand_is_never_clean(reply):
    assert classify(reply) == SpamState.UNKNOWN


def test_classification_ignores_case_and_spacing():
    assert classify(EN_CLEAN.upper()) == SpamState.CLEAN
    assert classify("  " + EN_LIMITED + "  ") == SpamState.LIMITED


def test_the_reply_is_kept_readable_for_a_tooltip():
    assert tidy("  many\n\nspaces   here ") == "many spaces here"
    assert len(tidy("x" * 900)) <= 400


# ── one account ─────────────────────────────────────────────────────────
def test_a_clean_account_gets_no_issue(services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_reply = EN_CLEAN

    assert asyncio.run(services.spamcheck.check(account)) == SpamState.CLEAN
    assert telegram.asked == [(account.key, "@SpamBot", "/start")]
    assert services.state.account_view(account)["issues"] == []


def test_a_limited_account_is_marked_with_the_bot_wording(
        services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_reply = EN_LIMITED

    asyncio.run(services.spamcheck.check(account))

    view = services.state.account_view(account)
    issue = next(i for i in view["issues"] if i["code"] == "account.spam_limited")
    assert "14 September 2026" in issue["message"], "show Telegram's own words"
    assert issue["level"] == "warning"


def test_a_limited_account_stops_being_green(services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_reply = EN_LIMITED_SHORT
    asyncio.run(services.spamcheck.check(account))

    view = services.state.account_view(account)
    assert view["issues"], "the dot has nothing to turn yellow for"
    assert any(i["code"] == "account.spam_limited" for i in view["issues"])


def test_a_limit_switches_the_account_off(services, storage, telegram, seeded):
    """A limited account must stop sending, and the account's own on/off
    switch is the lever - so the check throws that switch rather than
    inventing a second kind of stopped."""
    from app.models.enums import EffectiveState

    account = seeded["account"]
    campaign = services.campaigns.create({
        "name": "c", "account_id": account.id,
        "target_ids": [seeded["target"].id], "message_text": "hi"})
    telegram.bot_reply = EN_LIMITED

    asyncio.run(services.spamcheck.check(account))

    assert storage.accounts.get(account.id).disabled is True
    assert services.state.account_effective(account) == EffectiveState.DISABLED
    assert services.state.can_run(campaign)[0] is False, "its campaigns stop too"


def test_turning_it_back_on_clears_the_old_verdict(services, storage, telegram,
                                                   seeded):
    """Switching it on means the user dealt with it. Keeping the old mark
    would leave the account looking broken until the next check."""
    account = seeded["account"]
    campaign = services.campaigns.create({
        "name": "c", "account_id": account.id,
        "target_ids": [seeded["target"].id], "message_text": "hi"})
    telegram.bot_reply = EN_LIMITED
    asyncio.run(services.spamcheck.check(account))

    services.accounts.set_disabled(account, False)

    assert account.spam_state == SpamState.UNKNOWN
    assert account.spam_detail == ""
    assert services.state.account_view(account)["issues"] == []
    assert services.state.can_run(campaign)[0] is True, "campaigns come back"


def test_only_a_real_limit_switches_anything_off(services, storage, telegram,
                                                 seeded):
    """Failing to ask, or not understanding the answer, is not evidence."""
    account = seeded["account"]

    telegram.bot_fails.add(account.key)
    asyncio.run(services.spamcheck.check(account))
    assert account.disabled is False

    telegram.bot_fails.clear()
    telegram.bot_reply = "Something the bot has never said"
    asyncio.run(services.spamcheck.check(account))
    assert account.disabled is False


def test_a_switched_off_account_can_still_be_checked_on_its_own(
        services, storage, telegram, seeded):
    """The bulk run leaves switched-off accounts alone on purpose, so without
    this there would be no way to find out whether the limit had lifted."""
    storage.settings.set("spamcheck.enabled", True)
    account = seeded["account"]
    telegram.bot_reply = EN_LIMITED
    asyncio.run(services.spamcheck.check(account))
    assert account.disabled is True

    telegram.bot_reply = EN_CLEAN
    telegram.asked.clear()
    services.checkup.start(account_ids=[account.id])

    assert telegram.asked, "the bot was asked even though the account is off"
    assert account.spam_state == SpamState.CLEAN
    assert account.disabled is True, "verifying does not turn it on by itself"


def test_the_bulk_run_still_leaves_switched_off_accounts_alone(
        services, storage, telegram, seeded):
    account = seeded["account"]
    account.disabled = True
    storage.accounts.upsert(account)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    assert telegram.asked == []


def test_a_failed_check_is_reported_not_swallowed(services, storage, telegram,
                                                  seeded):
    account = seeded["account"]
    telegram.bot_fails.add(account.key)

    assert asyncio.run(services.spamcheck.check(account)) == SpamState.FAILED

    codes = [i["code"] for i in services.state.account_view(account)["issues"]]
    assert "account.spam_check_failed" in codes


def test_an_unrecognised_reply_is_surfaced(services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_reply = "Something the bot has never said before"

    asyncio.run(services.spamcheck.check(account))

    codes = [i["code"] for i in services.state.account_view(account)["issues"]]
    assert "account.spam_unclear" in codes, "silence here would look like a pass"


def test_an_unchecked_account_shows_nothing(services, storage, seeded):
    account = seeded["account"]
    assert account.spam_state == SpamState.UNKNOWN
    assert account.spam_checked_at is None
    assert services.state.account_view(account)["issues"] == []


def test_the_result_survives_a_restart(services, storage, telegram, seeded):
    from app.storage import Storage

    telegram.bot_reply = EN_LIMITED
    asyncio.run(services.spamcheck.check(seeded["account"]))

    reloaded = Storage().accounts.get(seeded["account"].id)
    assert reloaded.spam_state == SpamState.LIMITED
    assert "limited until" in reloaded.spam_detail.lower()


# ── every account ───────────────────────────────────────────────────────
def _extra_account(storage, seeded, key):
    from app.models import Account
    from app.models.enums import AccountState
    account = Account(key=key, session_file=f"{key}.session",
                      username=key, api_profile_id=seeded["profile"].id,
                      raw_state=AccountState.READY)
    storage.accounts.add(account)
    return account


def _enable(storage):
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})


def test_every_account_is_checked_in_turn(services, storage, telegram, seeded):
    _extra_account(storage, seeded, "session_778")
    _extra_account(storage, seeded, "session_779")
    _enable(storage)

    services.checkup.start()

    assert [key for key, _bot, _text in telegram.asked] == [
        "session_777", "session_778", "session_779"]
    assert all(a.spam_state == SpamState.CLEAN for a in storage.accounts.all())


def test_the_pause_is_waited_out_inside_the_single_check(
        services, storage, telegram, seeded, monkeypatch):
    """The wait belongs to the one-account method, right before it speaks to
    the bot - that is what keeps a skipped account from costing the wait."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.services.checkup.asyncio.sleep", fake_sleep)

    asked = asyncio.run(services.checkup.check_one(
        seeded["account"], spam=True, pause=5))

    assert asked is True
    assert slept == [5]


def test_a_skipped_account_never_costs_the_pause(services, storage, telegram,
                                                  seeded, monkeypatch):
    account = seeded["account"]
    account.api_profile_id = None            # a fault the probe cannot fix
    storage.accounts.upsert(account)

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.services.checkup.asyncio.sleep", fake_sleep)

    asked = asyncio.run(services.checkup.check_one(account, spam=True,
                                                       pause=5))

    assert asked is False
    assert slept == [], "nothing was said to the bot, so nothing to wait for"


def test_disabled_and_sessionless_accounts_are_skipped(services, storage,
                                                        telegram, seeded):
    from app.models import Account

    off = _extra_account(storage, seeded, "session_778")
    off.disabled = True
    storage.accounts.upsert(off)
    storage.accounts.add(Account(username="no_session",
                                 api_profile_id=seeded["profile"].id))
    _enable(storage)

    services.checkup.start()

    assert [key for key, _b, _t in telegram.asked] == ["session_777"]


def test_one_bad_account_does_not_stop_the_others(services, storage, telegram,
                                                  seeded):
    second = _extra_account(storage, seeded, "session_778")
    _enable(storage)
    telegram.bot_fails.add(second.key)

    services.checkup.start()

    assert storage.accounts.get(seeded["account"].id).spam_state == SpamState.CLEAN
    assert storage.accounts.get(second.id).spam_state == SpamState.FAILED


def test_the_bot_username_comes_from_settings(services, storage, telegram,
                                              seeded):
    storage.settings.set("spamcheck.bot", "@OtherBot")
    _enable(storage)
    services.checkup.start()
    assert telegram.asked[0][1] == "@OtherBot"


def test_both_paths_go_through_the_same_method(services, storage, telegram,
                                               seeded, monkeypatch):
    """Checking one account and checking all of them must be the same code,
    or the rarely-run half is the one that rots."""
    _extra_account(storage, seeded, "session_778")
    _enable(storage)

    seen: list[str] = []
    original = services.checkup.check_one

    async def watched(account, *args, **kwargs):
        seen.append(account.key)
        return await original(account, *args, **kwargs)

    monkeypatch.setattr(services.checkup, "check_one", watched)

    services.checkup.start()
    assert seen == ["session_777", "session_778"]

    seen.clear()
    services.checkup.start(account_ids=[seeded["account"].id])
    assert seen == ["session_777"]


def test_the_feature_is_off_until_asked_for(storage):
    assert storage.settings.get("spamcheck.enabled") is False
    assert storage.settings.get("spamcheck.bot") == "@SpamBot"
    assert storage.settings.get("spamcheck.delay_sec") == 5


# ── never answer the bot back ───────────────────────────────────────────
def test_the_auto_responder_ignores_bots(services, storage, telegram, seeded):
    """Otherwise @SpamBot's reply would trigger our own auto-reply."""
    account = seeded["account"]
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FIRST_MESSAGE", "response": "Здравствуйте!"}]})
    services.responder._running = True

    asyncio.run(services.responder._handle({
        "account_key": account.key, "peer_id": 42, "sender_name": "SpamBot",
        "sender_username": "SpamBot", "sender_bot": True, "message_id": 1,
        "text": "Good news, no limits", "out": False, "date": ""}))

    assert telegram.sent == [], "a bot must never get an auto-reply"


def test_a_person_still_gets_an_auto_reply(services, storage, telegram, seeded):
    account = seeded["account"]
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FIRST_MESSAGE", "response": "Здравствуйте!"}]})
    services.responder._running = True

    asyncio.run(services.responder._handle({
        "account_key": account.key, "peer_id": 43, "sender_name": "Client",
        "sender_username": "client", "sender_bot": False, "message_id": 1,
        "text": "привет", "out": False, "date": ""}))

    assert len(telegram.sent) == 1


# ── closing the bot's dialog ────────────────────────────────────────────
def test_the_acknowledge_button_is_offered_to_the_bot(services, storage,
                                                      telegram, seeded):
    """The reply comes with a "Cool, thanks" / "Хорошо, спасибо" button; not
    pressing it leaves the chat waiting on us between checks."""
    asyncio.run(services.spamcheck.check(seeded["account"]))
    assert telegram.acked == [("Cool, thanks", "Хорошо, спасибо")]


def test_only_the_listed_captions_are_ever_pressed():
    """The limited reply offers buttons that open an appeal. Those are the
    user's decision, so the list is exact captions, never a position."""
    from app.services.spamcheck import ACK_BUTTONS
    assert ACK_BUTTONS == ("Cool, thanks", "Хорошо, спасибо")


class _NoHistory:
    """A client whose history adds no further candidates."""

    async def get_messages(self, entity, limit=5):  # noqa: ARG002
        return []


def test_a_button_press_that_fails_does_not_fail_the_check():
    from app.telegram.service import TelegramService

    class Exploding:
        text = "Cool, thanks"

        async def click(self):
            raise RuntimeError("BUTTON_DATA_INVALID")

    class Reply:
        buttons = [[Exploding()]]

    real = TelegramService()
    # the answer is already in hand, so this must stay quiet
    pressed = asyncio.run(real._press_ack(_NoHistory(), object(), Reply(),
                                          ("Cool, thanks",), "session_777"))
    assert pressed is False


def test_an_appeal_button_is_left_alone():
    from app.telegram.service import TelegramService

    clicked = []

    class Appeal:
        text = "But I can't send messages to people"

        async def click(self):
            clicked.append(self.text)

    class Reply:
        buttons = [[Appeal()]]

    real = TelegramService()
    pressed = asyncio.run(real._press_ack(
        _NoHistory(), object(), Reply(),
        ("Cool, thanks", "Хорошо, спасибо"), "session_777"))
    assert pressed is False
    assert clicked == [], "we must never start an appeal on the user's behalf"


# ── a dialog left waiting for a button ──────────────────────────────────
STUCK_EN = "Please use buttons to communicate with me."
STUCK_RU = "Пожалуйста, используйте кнопки для общения со мной."


@pytest.mark.parametrize("reply", [STUCK_EN, STUCK_RU])
def test_the_button_complaint_is_recognised(reply):
    from app.services.spamcheck import is_stuck
    assert is_stuck(reply) is True
    # and it must never be read as a verdict either way
    assert classify(reply) == SpamState.UNKNOWN


def test_a_normal_reply_is_not_mistaken_for_the_complaint():
    from app.services.spamcheck import is_stuck
    for reply in (EN_CLEAN, RU_CLEAN_CURRENT, EN_LIMITED, RU_LIMITED):
        assert is_stuck(reply) is False


def test_a_stuck_dialog_is_retried_once_and_then_reads_the_status(
        services, storage, telegram, seeded):
    """First /start hits the pending button prompt; the press that follows
    closes it, so the second attempt gets the real answer."""
    account = seeded["account"]
    telegram.bot_replies = [STUCK_EN, EN_CLEAN]

    assert asyncio.run(services.spamcheck.check(account)) == SpamState.CLEAN
    assert len(telegram.asked) == 2, "exactly one retry"
    assert services.state.account_view(account)["issues"] == []


def test_a_dialog_that_stays_stuck_is_reported_not_guessed(
        services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_replies = [STUCK_RU, STUCK_EN]

    assert asyncio.run(services.spamcheck.check(account)) == SpamState.FAILED
    assert len(telegram.asked) == 2, "one retry, then give up"

    issue = next(i for i in services.state.account_view(account)["issues"]
                 if i["code"] == "account.spam_check_failed")
    assert "кнопк" in issue["message"].lower(), "say what actually happened"
    assert "@SpamBot" in issue["message"], "and where to fix it"


def test_the_retry_does_not_fire_for_an_ordinary_answer(
        services, storage, telegram, seeded):
    telegram.bot_replies = [EN_LIMITED]
    asyncio.run(services.spamcheck.check(seeded["account"]))
    assert len(telegram.asked) == 1


def test_the_keyboard_is_looked_for_in_recent_history_too(seeded):
    """The complaint message carries no keyboard of its own - the buttons sit
    on the bot's earlier message."""
    from app.telegram.service import TelegramService

    clicked = []

    class Button:
        text = "Cool, thanks"

        async def click(self):
            clicked.append(self.text)

    class Bare:
        buttons = None

    class WithKeyboard:
        buttons = [[Button()]]

    class Client:
        async def get_messages(self, entity, limit=5):  # noqa: ARG002
            return [Bare(), WithKeyboard()]

    real = TelegramService()
    pressed = asyncio.run(real._press_ack(Client(), object(), Bare(),
                                          ("Cool, thanks",), "session_777"))
    assert pressed is True
    assert clicked == ["Cool, thanks"]


# ── one language to read ────────────────────────────────────────────────
def test_clients_report_english_to_telegram(app_dir, monkeypatch):  # noqa: ARG001
    """Bots answer in the language the client reports. Pinning it means the
    antispam reply has one expected wording instead of forty."""
    from app.telegram import service as svc

    captured = {}

    class FakeClient:
        def __init__(self, session, api_id, api_hash, **kwargs):  # noqa: ARG002
            captured.update(kwargs)

        def is_connected(self):
            return True

    monkeypatch.setattr(svc, "TelegramClient", FakeClient)
    telegram = svc.TelegramService()
    telegram.configure_default_api(1, "hash")

    asyncio.run(telegram._client_for("session_777"))

    assert captured["lang_code"] == "en"
    assert captured["system_lang_code"] == "en"


# Words specific enough to stand alone: the bot never uses them to say an
# account is fine. Everything else must be a clause, because the clean and the
# limited answers share ordinary vocabulary.
SAFE_SINGLE_WORDS = {"anti-spam", "antispam", "антиспам"}


def test_every_limited_phrase_is_a_clause_or_a_reviewed_word():
    """Bare words are what broke this before: the clean Russian answer and a
    limit notice both talk about "ограничения", and only the words around them
    tell the two apart."""
    from app.services.spamcheck import LIMITED_MARKERS
    for phrase in LIMITED_MARKERS:
        if phrase in SAFE_SINGLE_WORDS:
            continue
        assert " " in phrase, f"{phrase!r} is a bare word - make it a clause"


def test_no_limited_phrase_appears_in_a_clean_answer():
    """A direct guard against the whole class of mistake."""
    from app.services.spamcheck import LIMITED_MARKERS, _normalise
    for clean in (EN_CLEAN, RU_CLEAN, RU_CLEAN_CURRENT):
        flat = _normalise(clean)
        hits = [m for m in LIMITED_MARKERS if m in flat]
        assert hits == [], f"{hits} matched a clean answer"



def test_the_targeted_check_follows_the_spam_setting(services, storage,
                                                     telegram, seeded):
    """The button does what "check all" does, for one account: it asks the bot
    only when the antispam check is switched on in settings."""
    account = seeded["account"]
    storage.settings.set("spamcheck.enabled", False)

    services.checkup.start(account_ids=[account.id])
    assert telegram.asked == []

    storage.settings.set("spamcheck.enabled", True)
    services.checkup.start(account_ids=[account.id])
    assert len(telegram.asked) == 1


def test_the_targeted_check_skips_a_genuinely_broken_account(services, storage,
                                                             telegram, seeded):
    """Switched off is what we are here to undo; a missing API profile is not."""
    account = seeded["account"]
    account.api_profile_id = None
    storage.accounts.upsert(account)
    storage.settings.set("spamcheck.enabled", True)

    services.checkup.start(account_ids=[account.id])

    assert telegram.asked == []
