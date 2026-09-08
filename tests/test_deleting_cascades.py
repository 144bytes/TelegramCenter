"""Deleting reaches everything that named the deleted record.

The rule the user set: whatever can be deleted is deleted completely, with no
leftovers. A campaign holding the id of a channel that no longer exists is a
leftover, and so is an account pointing at an API profile that has gone - both
showed a permanent "не найден" that could not be cleared from that side.

What each deletion means is not the same, though, and that is what these tests
pin down:

  * a channel is taken out of every campaign. A campaign that has just lost
    its last one stops and asks for another, in red;
  * an API profile is the keys an account signs in with. Without them the
    account cannot connect at all, so it is switched off and its campaigns
    stop with a line saying what would bring them back;
  * a proxy is only a route. The account keeps working and goes direct, which
    is what "без прокси" has always meant here - nothing turns red over it.

Stopping is final in every case: the campaign says what it is missing and
waits for the user to press Start. Nothing here starts anything again on its
own.
"""
from __future__ import annotations

from app.models import NetworkProfile, Operator
from app.models.enums import CampaignState, EffectiveState, ProbeState


def _campaign(services, seeded, targets=None, name="blast"):
    campaign = services.campaigns.create({
        "name": name, "account_id": seeded["account"].id,
        "target_ids": (targets if targets is not None
                       else [seeded["target"].id]),
        "messages": [{"text": "привет"}],
        "interval_min_sec": 0, "interval_max_sec": 0})
    return services.campaigns.start(campaign)


def _fresh(storage, campaign):
    return storage.campaigns.get(campaign.id)


# ── a channel ───────────────────────────────────────────────────────────
def test_deleting_a_channel_leaves_the_other_channels_running(
        services, storage, seeded):
    from app.models import Target
    second = Target(title="Channel B", username="channel_b")
    storage.targets.add(second)
    campaign = _campaign(services, seeded,
                         [seeded["target"].id, second.id])

    services.catalog.delete_target(second.id)

    fresh = _fresh(storage, campaign)
    assert fresh.target_ids == [seeded["target"].id]
    assert fresh.raw_state == CampaignState.SCHEDULED, "it still has work"


def test_losing_the_last_channel_stops_the_campaign(services, storage, seeded):
    campaign = _campaign(services, seeded)

    services.catalog.delete_target(seeded["target"].id)

    fresh = _fresh(storage, campaign)
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.next_run_at is None
    assert fresh.last_error["code"] == "stop.no_targets"
    assert services.state.campaign_effective(fresh) == EffectiveState.ERROR


def test_adding_a_channel_back_does_not_start_it(services, storage, seeded):
    """The user picks the chat and then decides whether to send at all."""
    campaign = _campaign(services, seeded)
    services.catalog.delete_target(seeded["target"].id)

    target = services.catalog.create_target("@channel_c")
    services.campaigns.update(_fresh(storage, campaign),
                              {"target_ids": [target.id]})

    fresh = _fresh(storage, campaign)
    assert fresh.raw_state == CampaignState.PAUSED

    services.campaigns.start(fresh)

    assert _fresh(storage, campaign).raw_state == CampaignState.SCHEDULED


# ── an account ──────────────────────────────────────────────────────────
def test_deleting_an_account_stops_its_campaigns(services, storage, seeded):
    campaign = _campaign(services, seeded)

    services.accounts.delete(seeded["account"].id)

    fresh = _fresh(storage, campaign)
    assert fresh is not None, "the campaign is the user's own record"
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.account_id == "", "no id of a record that does not exist"
    assert fresh.last_error["code"] == "stop.no_account"
    assert services.state.campaign_effective(fresh) == EffectiveState.ERROR


# ── an API profile ──────────────────────────────────────────────────────
def test_deleting_an_api_profile_switches_its_accounts_off(
        services, storage, seeded):
    campaign = _campaign(services, seeded)

    services.profiles.delete_api(seeded["profile"].id)

    account = storage.accounts.get(seeded["account"].id)
    assert account.api_profile_id is None, "no leftover id"
    assert account.disabled, "it cannot connect without the keys"
    assert account.stop_note["code"] == "note.api_deleted",         "and the card says why it is off"
    assert services.state.account_effective(account) == EffectiveState.ERROR
    fresh = _fresh(storage, campaign)
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.last_error["code"] == "note.api_deleted"


def test_an_operator_also_lets_go_of_a_deleted_api_profile(
        services, storage, seeded):
    operator = Operator(username="helper", key="op_session_1",
                        api_profile_id=seeded["profile"].id)
    storage.operators.add(operator)

    services.profiles.delete_api(seeded["profile"].id)

    assert storage.operators.get(operator.id).api_profile_id is None


def test_an_account_on_another_api_profile_is_left_alone(
        services, storage, seeded):
    from app.models import ApiProfile
    other = ApiProfile(name="Spare", api_id=999, api_hash="h")
    storage.api_profiles.add(other)
    campaign = _campaign(services, seeded)

    services.profiles.delete_api(other.id)

    account = storage.accounts.get(seeded["account"].id)
    assert account.api_profile_id == seeded["profile"].id
    assert not account.disabled
    assert _fresh(storage, campaign).raw_state == CampaignState.SCHEDULED


# ── a proxy ─────────────────────────────────────────────────────────────
def _proxied(storage, seeded):
    proxy = NetworkProfile(name="Proxy_1", host="127.0.0.1", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    storage.accounts.upsert(account)
    return proxy


def test_deleting_a_proxy_stops_the_account_with_a_warning(
        services, storage, seeded):
    """Without a proxy the account still works - from the home address. That
    is the user's decision to make, not something to start doing quietly."""
    proxy = _proxied(storage, seeded)
    campaign = _campaign(services, seeded)

    services.profiles.delete_proxy(proxy.id)

    account = storage.accounts.get(seeded["account"].id)
    assert account.network_profile_id is None, "the route is gone"
    assert account.disabled, "and nothing goes out until the user decides"
    assert account.stop_note["code"] == "note.proxy_deleted"
    issues = services.state.account_issues(account)
    assert [i.level for i in issues if i.code == "account.stopped"] == ["warning"]
    assert services.state.account_effective(account) == EffectiveState.DISABLED,         "у аккаунта два состояния: включён и выключен"
    assert _fresh(storage, campaign).raw_state == CampaignState.PAUSED


def test_switching_the_account_back_on_clears_the_warning(services, storage,
                                                          seeded):
    proxy = _proxied(storage, seeded)
    services.profiles.delete_proxy(proxy.id)
    account = storage.accounts.get(seeded["account"].id)

    services.accounts.set_disabled(account, False)

    assert account.stop_note is None
    assert services.state.account_effective(account) == EffectiveState.READY


def test_a_deleted_proxy_is_not_used_by_the_next_connection(
        services, storage, seeded):
    """`creds_for` is what the Telegram layer asks before it connects."""
    proxy = _proxied(storage, seeded)

    services.profiles.delete_proxy(proxy.id)

    assert services.accounts.creds_for(seeded["account"].key)["proxy"] is None


def test_an_operator_goes_direct_and_is_told(services, storage, seeded):
    """An operator has no switch - the user is holding its chat open - so it
    gets the note and keeps working."""
    proxy = _proxied(storage, seeded)
    operator = Operator(username="helper", key="op_session_1",
                        network_profile_id=proxy.id)
    storage.operators.add(operator)

    services.profiles.delete_proxy(proxy.id)

    fresh = storage.operators.get(operator.id)
    assert fresh.network_profile_id is None
    assert fresh.stop_note["code"] == "note.proxy_deleted_operator"


# ── an operator ─────────────────────────────────────────────────────────
def test_deleting_an_operator_stops_the_campaigns_that_name_one(
        services, storage, seeded):
    operator = services.accounts.promote_to_operator(seeded["account"])
    services.accounts.set_operator(seeded["account"], operator.id)
    campaign = services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id],
        "messages": [{"text": "пишите на @operator"}]})
    services.campaigns.start(campaign)

    services.operators.delete(operator.id)

    fresh = _fresh(storage, campaign)
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.last_error["code"] == "stop.no_operator"
    assert services.state.campaign_effective(fresh) == EffectiveState.ERROR


def test_a_campaign_without_the_token_keeps_running(services, storage, seeded):
    operator = services.accounts.promote_to_operator(seeded["account"])
    services.accounts.set_operator(seeded["account"], operator.id)
    campaign = _campaign(services, seeded)

    services.operators.delete(operator.id)

    assert _fresh(storage, campaign).raw_state == CampaignState.SCHEDULED


# ── выключен — значит выключен ───────────────────────────────────────────
def test_switching_an_account_off_drops_its_connection(services, storage,
                                                       telegram, seeded):
    """Живой клиент держит соединение с Telegram. Выключенный аккаунт,
    который всё ещё подключён, — это трафик, которого никто не просил."""
    account = seeded["account"]
    telegram.invalidated.clear()

    services.accounts.set_disabled(account, True)

    assert account.key in telegram.invalidated


def test_the_app_switching_it_off_drops_the_connection_too(services, storage,
                                                           telegram, seeded):
    proxy = _proxied(storage, seeded)
    telegram.invalidated.clear()

    services.profiles.delete_proxy(proxy.id)

    assert seeded["account"].key in telegram.invalidated


def test_deleting_an_account_forgets_who_wrote_to_it(services, storage, seeded):
    """Автоответчик помнит, кому уже отвечал, чтобы не здороваться дважды.
    Вместе с аккаунтом эта память тоже уходит."""
    from app.models import ConversationState
    account = seeded["account"]
    storage.conversations.add(ConversationState(
        account_id=account.id, peer_id=777, peer_name="Кто-то"))
    other = services.accounts.create(key="session_888", username="spare")
    storage.conversations.add(ConversationState(
        account_id=other.id, peer_id=777, peer_name="Кто-то"))

    services.accounts.delete(account.id)

    left = storage.conversations.all()
    assert [c.account_id for c in left] == [other.id]
