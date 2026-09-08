"""Which records a run covers, and which of them get asked about spam.

Each page's button checks what that page is about, and the antispam pass
reaches operators only when it has been asked to. Both are one mechanism with
a parameter rather than a second copy of the walk.
"""
from __future__ import annotations

import asyncio

from app.models import Account, Operator
from app.models.enums import AccountState, SpamState

EN_LIMITED = "Your account is now limited until 3 October 2026."


def _operator(storage, seeded, key="op_100"):
    op = Operator(username="support", key=key, session_file=f"{key}.session",
                  telegram_id=100, api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.operators.add(op)
    return op


def _account(storage, seeded, key="session_778"):
    acc = Account(key=key, session_file=f"{key}.session", username=key,
                  api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.accounts.add(acc)
    return acc


def _asked(telegram):
    return [key for key, _bot, _text in telegram.asked]


# ── scope ───────────────────────────────────────────────────────────────
def test_the_accounts_page_checks_accounts_only(services, storage, telegram,
                                                 seeded):
    op = _operator(storage, seeded)

    progress = services.checkup.start(spam=False, scope="accounts")

    assert progress["total"] == 1, "the one account, not the operator"
    assert storage.operators.get(op.id).last_check_at is None


def test_the_operators_page_checks_operators_only(services, storage, telegram,
                                                   seeded):
    op = _operator(storage, seeded)
    account = seeded["account"]

    progress = services.checkup.start(spam=False, scope="operators")

    assert progress["total"] == 1
    assert storage.operators.get(op.id).last_check_at is not None
    assert storage.accounts.get(account.id).last_check_at is None


def test_without_a_scope_both_kinds_are_covered(services, storage, telegram,
                                                 seeded):
    _operator(storage, seeded)
    assert services.checkup.start(spam=False)["total"] == 2


def test_a_scoped_run_still_blocks_a_second_one(services, storage, telegram,
                                                 seeded, monkeypatch):
    _operator(storage, seeded)
    monkeypatch.setattr(type(services.checkup), "running",
                        property(lambda self: True))

    services.checkup.start(scope="operators")

    assert _asked(telegram) == [], "one run at a time, whatever its scope"


# ── operators and the antispam bot ──────────────────────────────────────
def test_operators_are_not_asked_by_default(services, storage, telegram, seeded):
    """The option is off on a fresh install, so a normal check must not send
    anything from an operator."""
    _operator(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    assert _asked(telegram) == ["session_777"], "the account only"


def test_the_option_is_off_on_a_fresh_install(storage):
    assert storage.settings.get("spamcheck.include_operators") is False


def test_operators_join_the_check_when_asked(services, storage, telegram, seeded):
    op = _operator(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True,
                             "spamcheck.include_operators": True,
                             "spamcheck.delay_sec": 0})

    services.checkup.start()

    assert sorted(_asked(telegram)) == ["op_100", "session_777"]
    assert storage.operators.get(op.id).spam_state == SpamState.CLEAN


def test_a_targeted_operator_check_obeys_the_option(services, storage, telegram,
                                                     seeded):
    """Even asked outright, an operator is not messaged while the option is
    off - otherwise the setting would only hold for the bulk run."""
    op = _operator(storage, seeded)
    storage.settings.set("spamcheck.enabled", True)

    asked = asyncio.run(services.checkup.check_one(op, spam=True))

    assert asked is False
    assert _asked(telegram) == []


def test_the_verdict_lands_on_the_operator_record(services, storage, telegram,
                                                   seeded):
    op = _operator(storage, seeded)
    telegram.bot_reply = EN_LIMITED
    storage.settings.update({"spamcheck.enabled": True,
                             "spamcheck.include_operators": True,
                             "spamcheck.delay_sec": 0})

    services.checkup.start(scope="operators")

    fresh = storage.operators.get(op.id)
    assert fresh.spam_state == SpamState.LIMITED
    assert fresh.spam_checked_at is not None
    codes = [i["code"] for i in services.state.operator_view(fresh)["issues"]]
    assert "account.spam_limited" in codes, "the dot must stop being green"


def test_a_limited_operator_is_not_switched_off(services, storage, telegram,
                                                 seeded):
    """An operator answers people; it sends no campaigns. Stopping it would
    take away the replies the limit never touched."""
    op = _operator(storage, seeded)
    telegram.bot_reply = EN_LIMITED
    storage.settings.update({"spamcheck.enabled": True,
                             "spamcheck.include_operators": True,
                             "spamcheck.delay_sec": 0})

    services.checkup.start(scope="operators")

    fresh = storage.operators.get(op.id)
    assert fresh.spam_state == SpamState.LIMITED
    assert services.state.operator_effective(fresh) == "READY", \
        "warned about, not taken out of service"


def test_a_limited_account_is_still_switched_off(services, storage, telegram,
                                                  seeded):
    """The account side must not have changed on the way."""
    telegram.bot_reply = EN_LIMITED
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start(scope="accounts")

    assert storage.accounts.get(seeded["account"].id).disabled is True


def test_one_bad_record_does_not_stop_the_rest(services, storage, telegram,
                                                seeded):
    op = _operator(storage, seeded)
    second = _operator(storage, seeded, key="op_101")
    storage.settings.update({"spamcheck.enabled": True,
                             "spamcheck.include_operators": True,
                             "spamcheck.delay_sec": 0})
    telegram.bot_fails.add(op.key)

    services.checkup.start(scope="operators")

    assert storage.operators.get(op.id).spam_state == SpamState.FAILED
    assert storage.operators.get(second.id).spam_state == SpamState.CLEAN


def test_a_queued_operator_drops_its_stale_verdict(services, storage, seeded):
    op = _operator(storage, seeded)
    op.spam_state = SpamState.LIMITED
    op.spam_detail = "old news"
    storage.operators.upsert(op)

    services.checkup._queue(op, probe=True)

    assert op.raw_state == AccountState.QUEUED
    assert op.spam_state == SpamState.UNKNOWN
    assert op.spam_detail == ""


# ── one method for both kinds ───────────────────────────────────────────
def test_both_kinds_go_through_the_same_method(services, storage, telegram,
                                               seeded, monkeypatch):
    """Operators used to have their own shorter version of the walk, which is
    why their state lagged behind."""
    _operator(storage, seeded)
    _account(storage, seeded)
    storage.settings.set("spamcheck.delay_sec", 0)

    seen = []
    original = services.checkup.check_one

    async def watched(owner, *args, **kwargs):
        seen.append(owner.key)
        return await original(owner, *args, **kwargs)

    monkeypatch.setattr(services.checkup, "check_one", watched)
    services.checkup.start(spam=False)

    assert sorted(seen) == ["op_100", "session_777", "session_778"]


# ── the two kinds report the same situation the same way ────────────────
def test_a_session_without_a_name_is_not_an_error(services, storage, seeded):
    """Found on disk, no name yet: an account calls that offline, so an
    operator must not call it broken. The probe fills the name in."""
    op = Operator(key="op_555", session_file="op_555.session",
                  api_profile_id=seeded["profile"].id)
    storage.operators.add(op)

    view = services.state.operator_view(op)
    codes = [i["code"] for i in view["issues"]]

    assert "operator.no_name" not in codes
    assert view["effective"] != "ERROR"


def test_a_discovered_operator_gets_the_default_api_profile(services, storage,
                                                             app_dir, seeded):
    """A session with no keys to use it with is not a working operator - and
    an account discovered the same way has always been given them."""
    from app.services.discovery import discover

    (app_dir / "sessions" / "operators" / "op_555.session").write_text("x")
    (app_dir / "sessions" / "campaign" / "session_901.session").write_text("x")

    report = discover(storage)

    assert report == {"accounts": 1, "operators": 1}
    op = storage.operators.find(lambda o: o.key == "op_555")
    acc = storage.accounts.find(lambda a: a.key == "session_901")
    assert op.api_profile_id == seeded["profile"].id
    assert acc.api_profile_id == op.api_profile_id, "the same default for both"
    codes = [i["code"] for i in services.state.operator_view(op)["issues"]]
    assert "api.not_assigned" not in codes


def test_a_handle_only_operator_with_no_name_is_still_an_error(services, storage):
    """Without a session and without a name there is nothing to identify it
    by, and nothing that will ever fill it in."""
    op = Operator()
    storage.operators.add(op)

    codes = [i["code"] for i in services.state.operator_view(op)["issues"]]
    assert "operator.no_name" in codes


def test_a_rescan_repairs_a_missing_api_profile(services, storage, app_dir, seeded):
    """Sessions discovered before any profile existed had no keys and no way
    back - rescanning skipped them because the record was already there."""
    from app.services.discovery import adopt_default_api, discover

    (app_dir / "sessions" / "operators" / "op_555.session").write_text("x")
    (app_dir / "sessions" / "campaign" / "session_901.session").write_text("x")
    storage.api_profiles.delete(seeded["profile"].id)
    discover(storage)                      # nothing to hand out yet
    op = storage.operators.find(lambda o: o.key == "op_555")
    assert op.api_profile_id is None

    from app.models import ApiProfile
    storage.api_profiles.add(ApiProfile(name="Later", api_id=7, api_hash="h"))

    assert adopt_default_api(storage) == 2, "both kinds are repaired"
    assert storage.operators.find(lambda o: o.key == "op_555").api_profile_id
    assert storage.accounts.find(lambda a: a.key == "session_901").api_profile_id


def test_a_rescan_leaves_a_chosen_profile_alone(services, storage, seeded):
    from app.models import ApiProfile
    from app.services.discovery import adopt_default_api

    other = ApiProfile(name="Other", api_id=9, api_hash="h")
    storage.api_profiles.add(other)
    account = seeded["account"]
    account.api_profile_id = other.id
    storage.accounts.upsert(account)

    adopt_default_api(storage)

    assert storage.accounts.get(account.id).api_profile_id == other.id
