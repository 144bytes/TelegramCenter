"""The dependency graph and the raw-vs-effective split."""
from __future__ import annotations

from app.models import Campaign, NetworkProfile, Operator, Schedule
from app.models.enums import (
    AccountState, CampaignState, EffectiveState, ProbeState,
)
from app.state import StateManager


def test_healthy_account_is_ready(storage, seeded):
    state = StateManager(storage)
    assert state.account_effective(seeded["account"]) == EffectiveState.READY
    assert state.account_view(seeded["account"])["issues"] == []


def test_api_profile_error_cascades_to_account(storage, seeded):
    state = StateManager(storage)
    profile = seeded["profile"]
    profile.raw_state = ProbeState.ERROR
    profile.last_error = "AUTH_KEY_UNREGISTERED"
    storage.api_profiles.upsert(profile)

    # nothing recomputed the account by hand — the value is derived on read
    assert state.account_effective(seeded["account"]) == EffectiveState.ERROR
    codes = [i["code"] for i in state.account_view(seeded["account"])["issues"]]
    assert "api.error" in codes


def test_deleting_api_profile_breaks_the_account(storage, seeded):
    state = StateManager(storage)
    storage.api_profiles.delete(seeded["profile"].id)
    view = state.account_view(seeded["account"])
    assert view["effective"] == EffectiveState.ERROR
    assert "api.missing" in [i["code"] for i in view["issues"]]


def test_disabled_api_profile_breaks_the_account(storage, seeded):
    state = StateManager(storage)
    seeded["profile"].enabled = False
    storage.api_profiles.upsert(seeded["profile"])
    assert state.account_effective(seeded["account"]) == EffectiveState.ERROR


def test_broken_proxy_breaks_the_account(storage, seeded):
    state = StateManager(storage)
    proxy = NetworkProfile(name="p", host="1.2.3.4", port=1080,
                           raw_state=ProbeState.ERROR, last_error="timeout")
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    storage.accounts.upsert(account)

    assert state.account_effective(account) == EffectiveState.ERROR
    assert "proxy.error" in [i["code"] for i in state.account_view(account)["issues"]]


def test_forbidden_combination_is_unreachable(storage, seeded):
    """proxy=ERROR + account=READY + campaign=RUNNING must be impossible.

    We write the worst case straight into storage — a stored RUNNING campaign
    on an account whose proxy is dead — and check that nothing the UI can see
    reports it as running.
    """
    state = StateManager(storage)
    proxy = NetworkProfile(name="p", host="1.2.3.4", port=1080,
                           raw_state=ProbeState.ERROR, last_error="dead")
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    account.raw_state = AccountState.READY          # stale raw value, on purpose
    storage.accounts.upsert(account)

    campaign = Campaign(name="c", account_id=account.id,
                        target_ids=[seeded["target"].id], message_text="hi",
                        raw_state=CampaignState.RUNNING)
    campaign.sync_results()
    storage.campaigns.add(campaign)

    assert state.network_profile_view(proxy)["effective"] == EffectiveState.ERROR
    assert state.account_effective(account) == EffectiveState.ERROR
    assert state.campaign_effective(campaign) == EffectiveState.ERROR
    ok, reason = state.can_run(campaign)
    assert ok is False and reason


def test_campaign_blocked_when_account_merely_offline(storage, seeded):
    """An offline (not broken) account blocks an *active* campaign but leaves
    a draft alone — a draft is not claiming to be doing anything."""
    state = StateManager(storage)
    account = seeded["account"]
    account.raw_state = AccountState.OFFLINE
    storage.accounts.upsert(account)

    active = Campaign(name="a", account_id=account.id, message_text="x",
                      target_ids=[seeded["target"].id],
                      raw_state=CampaignState.SCHEDULED)
    draft = Campaign(name="d", account_id=account.id, message_text="x",
                     target_ids=[seeded["target"].id],
                     raw_state=CampaignState.DRAFT)
    for c in (active, draft):
        c.sync_results()
        storage.campaigns.add(c)

    assert state.campaign_effective(active) == EffectiveState.BLOCKED
    assert state.campaign_effective(draft) == CampaignState.DRAFT


def test_effective_state_is_never_persisted(storage, seeded):
    """Effective status must not leak into the JSON files."""
    state = StateManager(storage)
    state.account_view(seeded["account"])
    state.snapshot()
    raw = storage.accounts.path.read_text(encoding="utf-8")
    assert "effective" not in raw
    assert "issues" not in raw


def test_disabled_account_reports_disabled_not_error(storage, seeded):
    state = StateManager(storage)
    account = seeded["account"]
    account.disabled = True
    storage.accounts.upsert(account)
    assert state.account_effective(account) == EffectiveState.DISABLED


def test_dangling_operator_is_a_warning_not_an_error(storage, seeded):
    state = StateManager(storage)
    account = seeded["account"]
    account.operator_id = "op_gone"
    storage.accounts.upsert(account)
    view = state.account_view(account)
    assert view["effective"] == EffectiveState.READY
    assert "operator.missing" in [i["code"] for i in view["issues"]]


def test_operator_without_session_is_flagged(storage):
    state = StateManager(storage)
    op = Operator(username="helper")
    storage.operators.add(op)
    view = state.operator_view(op)
    assert view["logged_in"] is False
    assert "operator.not_logged_in" in [i["code"] for i in view["issues"]]
    # a handle alone is still usable, so this must not be an error
    assert all(i["level"] == "warning" for i in view["issues"])


def test_campaign_missing_account_is_an_error(storage):
    state = StateManager(storage)
    campaign = Campaign(name="orphan", account_id="acc_gone", message_text="hi",
                        schedule=Schedule())
    storage.campaigns.add(campaign)
    view = state.campaign_view(campaign)
    assert view["effective"] == EffectiveState.ERROR
    assert "campaign.account_missing" in [i["code"] for i in view["issues"]]


def test_snapshot_covers_every_section(storage, seeded):  # noqa: ARG001
    snap = StateManager(storage).snapshot()
    for key in ("accounts", "campaigns", "operators", "targets", "templates",
                "api_profiles", "network_profiles", "auto_reply", "settings"):
        assert key in snap
