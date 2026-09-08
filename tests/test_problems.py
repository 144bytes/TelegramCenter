"""The overview list: one problem, one row.

The case this was written for: an account's session is killed by Telegram.
Walking the entities naively produced a row for the account, one for each of
its campaigns ("Аккаунт: ERROR"), one for its API profile, and another for the
operator made from it - four lines, one cause.
"""
from __future__ import annotations

from app.models import (
    Account, ApiProfile, Campaign, CampaignMessage, NetworkProfile, Operator,
    Target,
)
from app.models.enums import AccountState, ProbeState
from app.state import StateManager

DEAD = ("AuthKeyDuplicatedError: The authorization key (session file) was "
        "used under two different IP addresses simultaneously")


def _profile(storage, **fields):
    p = ApiProfile(name="Main", api_id=1, api_hash="h")
    for k, v in fields.items():
        setattr(p, k, v)
    storage.api_profiles.add(p)
    return p


def _account(storage, profile, **fields):
    a = Account(key="session_7", telegram_id=7, username="kelly",
                api_profile_id=profile.id, raw_state=AccountState.READY)
    for k, v in fields.items():
        setattr(a, k, v)
    storage.accounts.add(a)
    return a


def _campaign(storage, account, name="c"):
    target = Target(title="T", username="t")
    storage.targets.add(target)
    c = Campaign(name=name, account_id=account.id,
                 messages=[CampaignMessage(text="hi")],
                 target_ids=[target.id])
    c.sync_results()
    storage.campaigns.add(c)
    return c


def _codes(row):
    return [i["code"] for i in row["issues"]]


# ── the dead-session case, end to end ───────────────────────────────────
def test_one_dead_session_is_exactly_one_row(storage):
    profile = _profile(storage)
    account = _account(storage, profile, raw_state=AccountState.AUTH_DEAD,
                       last_error=DEAD)
    _campaign(storage, account, "Кампания 1")
    _campaign(storage, account, "Кампания 2")
    # the operator made from it has the account's problem, not a second one
    storage.operators.add(Operator(account_id=account.id))

    problems = StateManager(storage).problems()

    assert len(problems) == 1, [p["title"] for p in problems]
    row = problems[0]
    assert row["id"] == account.id
    assert row["route"] == "accounts"
    assert row["roles"] == ["account"]
    assert row["blocked_campaigns"] == 2


def test_the_row_points_at_the_record_to_open(storage):
    profile = _profile(storage)
    account = _account(storage, profile, raw_state=AccountState.AUTH_DEAD,
                       last_error=DEAD)

    row = StateManager(storage).problems()[0]

    assert row["id"] == account.id
    assert row["route"] == "accounts"
    assert row["key"] == f"accounts:{account.id}"


# ── each rule on its own ────────────────────────────────────────────────
def test_a_broken_profile_is_reported_once_not_once_per_dependant(storage):
    profile = _profile(storage)
    _account(storage, profile, raw_state=AccountState.ERROR,
             last_error="ApiIdInvalidError: The api_id/api_hash combination is invalid")
    second = Account(key="session_8", telegram_id=8, username="other",
                     api_profile_id=profile.id, raw_state=AccountState.READY)
    storage.accounts.add(second)

    problems = StateManager(storage).problems()

    assert [p["route"] for p in problems] == ["settings"]
    assert _codes(problems[0]) == ["api.rejected"]


def test_an_account_still_reports_a_fault_that_is_its_own(storage):
    profile = _profile(storage)
    _account(storage, profile, raw_state=AccountState.RESTRICTED)
    storage.accounts.add(Account(key="session_9", telegram_id=9,
                                 api_profile_id=profile.id,
                                 raw_state=AccountState.ERROR,
                                 last_error="ApiIdInvalidError: The api_id/api_hash combination is invalid"))

    problems = StateManager(storage).problems()
    routes = {p["route"] for p in problems}

    assert routes == {"settings", "accounts"}
    account_row = next(p for p in problems if p["route"] == "accounts")
    assert _codes(account_row) == ["account.restricted"], \
        "the profile's fault belongs to the profile's row"


def test_a_campaign_blocked_only_by_its_account_gets_no_row(storage):
    profile = _profile(storage)
    account = _account(storage, profile, raw_state=AccountState.OFFLINE)
    _campaign(storage, account)

    problems = StateManager(storage).problems()

    assert [p["route"] for p in problems] == ["accounts"]
    assert problems[0]["blocked_campaigns"] == 1


def test_a_campaign_with_a_fault_of_its_own_keeps_its_row(storage):
    profile = _profile(storage)
    account = _account(storage, profile, raw_state=AccountState.OFFLINE)
    campaign = _campaign(storage, account)
    campaign.target_ids = []
    campaign.sync_results()
    storage.campaigns.upsert(campaign)

    problems = StateManager(storage).problems()
    campaign_row = next(p for p in problems if p["route"] == "campaigns")

    assert _codes(campaign_row) == ["campaign.no_targets"]
    assert "campaign.account_not_ready" not in _codes(campaign_row)


def test_an_orphan_campaign_speaks_for_itself(storage):
    """Nothing upstream is saying it, because the account is gone."""
    campaign = Campaign(name="orphan", account_id="acc_gone",
                        messages=[CampaignMessage(text="hi")])
    storage.campaigns.add(campaign)

    problems = StateManager(storage).problems()

    assert len(problems) == 1
    assert "campaign.account_missing" in _codes(problems[0])


def test_an_operator_without_a_twin_keeps_its_own_row(storage):
    profile = _profile(storage)
    _account(storage, profile)
    storage.operators.add(Operator(username="lonely", key="op_99",
                                   telegram_id=99,
                                   raw_state=AccountState.AUTH_DEAD,
                                   last_error=DEAD))

    problems = StateManager(storage).problems()

    assert [p["route"] for p in problems] == ["operators"]


def test_the_same_fault_from_both_roles_is_listed_once(storage):
    profile = _profile(storage)
    account = _account(storage, profile, raw_state=AccountState.AUTH_DEAD,
                       last_error=DEAD)
    storage.operators.add(Operator(username="kelly", key=account.key,
                                   telegram_id=7,
                                   api_profile_id=profile.id,
                                   raw_state=AccountState.AUTH_DEAD,
                                   last_error=DEAD))

    row = StateManager(storage).problems()[0]

    assert _codes(row).count("account.auth_dead") == 1


def test_a_healthy_setup_has_nothing_to_report(storage):
    profile = _profile(storage)
    account = _account(storage, profile)
    _campaign(storage, account)

    assert StateManager(storage).problems() == []


def test_a_broken_proxy_is_its_own_row(storage):
    profile = _profile(storage)
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080,
                           raw_state=ProbeState.ERROR, last_error="refused")
    storage.network_profiles.add(proxy)
    _account(storage, profile, network_profile_id=proxy.id)

    problems = StateManager(storage).problems()

    assert len(problems) == 1
    assert problems[0]["id"] == proxy.id
    assert problems[0]["route"] == "settings"


def test_the_snapshot_carries_the_list(storage):
    profile = _profile(storage)
    _account(storage, profile, raw_state=AccountState.AUTH_DEAD,
             last_error=DEAD)

    snap = StateManager(storage).snapshot()

    assert "problems" in snap
    assert len(snap["problems"]) == 1
