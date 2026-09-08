"""One action over many records.

Nothing here does any work of its own: every action names a service method
that already exists, so a bulk edit and a single edit cannot come to mean
different things. What is worth testing is the loop around them — that one
refusal does not cancel the rest, and that the caller is told which ones
refused.
"""
from __future__ import annotations

from app.models import Account, ApiProfile, NetworkProfile, Operator, Target
from app.models.enums import AccountState, CampaignState, ProbeState
from app.services.bulk import BulkError

import pytest


def _campaign(services, seeded, name, targets=None):
    return services.campaigns.create({
        "name": name, "account_id": seeded["account"].id,
        "target_ids": targets if targets is not None else [seeded["target"].id],
        "messages": [{"text": "hi"}], "interval_min_sec": 0, "interval_max_sec": 0})


# ── the loop ────────────────────────────────────────────────────────────
def test_starting_everything_selected_is_one_call(services, storage, seeded):
    ids = [_campaign(services, seeded, f"c{i}").id for i in range(3)]

    report = services.bulk.apply("campaigns", ids, "start")

    assert report == {"done": 3, "failed": [], "total": 3}
    assert all(storage.campaigns.get(i).raw_state == CampaignState.SCHEDULED
               for i in ids)


def test_one_refusal_does_not_cancel_the_rest(services, storage, seeded):
    """Sixteen changed and two refused is the normal outcome of a bulk edit.
    "Nothing happened because one of them had no channels" is not."""
    good = [_campaign(services, seeded, f"c{i}").id for i in range(2)]
    empty = _campaign(services, seeded, "no channels", targets=[])

    report = services.bulk.apply("campaigns", good + [empty.id], "start")

    assert report["done"] == 2
    assert report["total"] == 3
    assert len(report["failed"]) == 1
    assert report["failed"][0]["name"] == "no channels"
    refusal = report["failed"][0]["error"]
    assert refusal["params"]["reason"]["code"] == "issue.campaign.no_targets"
    assert all(storage.campaigns.get(i).raw_state == CampaignState.SCHEDULED
               for i in good)


def test_a_record_that_has_gone_is_reported_not_raised(services, seeded):
    report = services.bulk.apply("campaigns", ["camp_nope"], "pause")
    assert report["done"] == 0
    assert report["failed"][0]["id"] == "camp_nope"


def test_an_unknown_action_is_refused_outright(services, seeded):
    campaign = _campaign(services, seeded, "c")
    with pytest.raises(BulkError):
        services.bulk.apply("campaigns", [campaign.id], "self_destruct")


def test_an_unknown_kind_is_refused_outright(services):
    with pytest.raises(BulkError):
        services.bulk.apply("unicorns", ["x"], "start")


def test_nothing_selected_is_refused(services):
    with pytest.raises(BulkError):
        services.bulk.apply("campaigns", [], "start")


# ── editing several campaigns ───────────────────────────────────────────
def test_only_the_fields_that_were_sent_are_changed(services, storage, seeded):
    """A bulk form is a form of blanks. A field left alone must not become
    "set this to empty on all twenty of them"."""
    campaign = _campaign(services, seeded, "keep my texts")
    before = [m.text for m in campaign.messages]

    services.bulk.apply("campaigns", [campaign.id], "update",
                        {"interval_min_sec": 90, "interval_max_sec": 120})

    fresh = storage.campaigns.get(campaign.id)
    assert (fresh.interval_min_sec, fresh.interval_max_sec) == (90, 120)
    assert [m.text for m in fresh.messages] == before
    assert fresh.target_ids == campaign.target_ids


def test_the_same_validation_applies_as_to_one_campaign(services, storage, seeded):
    campaign = _campaign(services, seeded, "c")

    report = services.bulk.apply("campaigns", [campaign.id], "update",
                                 {"interval_min_sec": 500, "interval_max_sec": 10})

    assert report["done"] == 0, "a bulk edit is not a way round the rules"
    assert report["failed"][0]["error"]["code"] == "err.interval.inverted"


def test_picked_channels_replace_the_list_of_every_campaign(
        services, storage, seeded):
    """A=[1,2], B=[] -> channel 10 picked -> A=[10], B=[10]."""
    second = Target(title="B", username="channel_b")
    tenth = Target(title="J", username="channel_j")
    storage.targets.add(second)
    storage.targets.add(tenth)
    a = _campaign(services, seeded, "a", [seeded["target"].id, second.id])
    b = _campaign(services, seeded, "b", [])

    services.bulk.apply("campaigns", [a.id, b.id], "update",
                        {"target_ids": [tenth.id]})

    assert storage.campaigns.get(a.id).target_ids == [tenth.id]
    assert storage.campaigns.get(b.id).target_ids == [tenth.id]
    assert [r.target_id for r in storage.campaigns.get(a.id).results] == [tenth.id]


def test_no_channels_picked_keeps_each_list(services, storage, seeded):
    second = Target(title="B", username="channel_b")
    storage.targets.add(second)
    a = _campaign(services, seeded, "a", [seeded["target"].id, second.id])
    b = _campaign(services, seeded, "b", [second.id])

    services.bulk.apply("campaigns", [a.id, b.id], "update",
                        {"target_ids": [], "interval_min_sec": 5,
                         "interval_max_sec": 9})

    assert storage.campaigns.get(a.id).target_ids == [seeded["target"].id, second.id]
    assert storage.campaigns.get(b.id).target_ids == [second.id]
    assert storage.campaigns.get(b.id).interval_max_sec == 9


# ── accounts ────────────────────────────────────────────────────────────
def _accounts(storage, n=3):
    profile = storage.api_profiles.find(lambda p: True)
    out = []
    for i in range(n):
        account = Account(key=f"session_{i}", telegram_id=i, username=f"a{i}",
                          api_profile_id=profile.id,
                          raw_state=AccountState.READY)
        storage.accounts.add(account)
        out.append(account)
    return out


def test_the_proxy_can_be_changed_on_all_of_them_at_once(services, storage,
                                                         telegram, seeded):
    """The thing eighteen accounts behind one address actually needs."""
    accounts = _accounts(storage)
    proxy = NetworkProfile(name="Second", host="10.0.0.1", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)

    report = services.bulk.apply(
        "accounts", [a.id for a in accounts], "update",
        {"network_profile_id": proxy.id})

    assert report["done"] == 3
    assert all(storage.accounts.get(a.id).network_profile_id == proxy.id
               for a in accounts)
    assert set(telegram.invalidated) >= {a.key for a in accounts}, \
        "each one reconnects through the new proxy instead of keeping the old link"


def test_a_new_api_and_proxy_are_one_reconnect_per_account(services, storage,
                                                           telegram, seeded):
    """They used to be two requests: in between, every account reconnected
    with the new API through the old proxy."""
    accounts = _accounts(storage)
    api = ApiProfile(name="New", api_id=777, api_hash="h" * 32)
    storage.api_profiles.add(api)
    proxy = NetworkProfile(name="Second", host="10.0.0.1", port=1080)
    storage.network_profiles.add(proxy)
    operator = Operator(username="support")
    storage.operators.add(operator)

    report = services.bulk.apply(
        "accounts", [a.id for a in accounts], "update",
        {"api_profile_id": api.id, "network_profile_id": proxy.id,
         "operator_id": operator.id})

    assert report == {"done": 3, "failed": [], "total": 3}
    for a in accounts:
        fresh = storage.accounts.get(a.id)
        assert (fresh.api_profile_id, fresh.network_profile_id,
                fresh.operator_id) == (api.id, proxy.id, operator.id)
        assert telegram.invalidated.count(a.key) == 1


def test_only_what_was_chosen_changes(services, storage, seeded):
    accounts = _accounts(storage, 2)
    before = [a.api_profile_id for a in accounts]

    services.bulk.apply("accounts", [a.id for a in accounts], "update",
                        {"operator_id": ""})

    assert [storage.accounts.get(a.id).api_profile_id for a in accounts] == before


def test_a_profile_that_does_not_exist_is_refused(services, storage, seeded):
    accounts = _accounts(storage, 2)
    proxy = NetworkProfile(name="Real", host="10.0.0.3", port=1080)
    storage.network_profiles.add(proxy)

    report = services.bulk.apply(
        "accounts", [a.id for a in accounts], "update",
        {"network_profile_id": proxy.id, "api_profile_id": "api_nope"})

    assert report["done"] == 0, "twenty accounts pointing at nothing is worse"
    assert all(f["error"]["code"] == "err.not_found.profile"
               for f in report["failed"])
    assert all(storage.accounts.get(a.id).network_profile_id is None
               for a in accounts), "nothing half-applied"


def test_an_empty_choice_means_none_rather_than_an_error(services, storage,
                                                         seeded):
    accounts = _accounts(storage, 2)
    for a in accounts:
        a.network_profile_id = "some_old_proxy"
        storage.accounts.upsert(a)

    report = services.bulk.apply("accounts", [a.id for a in accounts],
                                 "update", {"network_profile_id": ""})

    assert report["done"] == 2
    assert all(storage.accounts.get(a.id).network_profile_id is None
               for a in accounts)


def test_accounts_can_be_switched_off_together(services, storage, seeded):
    accounts = _accounts(storage)
    services.bulk.apply("accounts", [a.id for a in accounts], "disable")
    assert all(storage.accounts.get(a.id).disabled for a in accounts)

    services.bulk.apply("accounts", [a.id for a in accounts], "enable")
    assert not any(storage.accounts.get(a.id).disabled for a in accounts)


# ── channels ────────────────────────────────────────────────────────────
def test_channels_can_be_switched_off_together(services, storage, seeded):
    ids = []
    for i in range(3):
        target = Target(title=f"T{i}", username=f"t{i}")
        storage.targets.add(target)
        ids.append(target.id)

    services.bulk.apply("targets", ids, "disable")

    assert all(storage.targets.get(i).active is False for i in ids)


def test_deleting_channels_takes_them_out_of_the_campaigns(
        services, storage, seeded):
    """The standing rule is now the opposite one: deleting deletes.

    A campaign used to keep the id and report "Канал не найден" for ever,
    with no way to clear the row from the campaign side.
    """
    campaign = _campaign(services, seeded, "c")

    services.bulk.apply("targets", [seeded["target"].id], "delete")

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.target_ids == []
    assert fresh.results == [], "its row and its history go with it"
    assert not any(i.code == "campaign.target_missing"
                   for i in services.state.campaign_issues(fresh))


# ── operators ───────────────────────────────────────────────────────────
def test_a_linked_operators_proxy_is_set_on_its_account(services, storage,
                                                        seeded):
    """One setting (R5): the operator made from an account has no proxy of
    its own, so a bulk edit sets the account's."""
    linked = services.accounts.promote_to_operator(seeded["account"])
    alone = Operator(username="alone", key="op_5", telegram_id=5,
                     api_profile_id=seeded["profile"].id,
                     raw_state=AccountState.READY)
    storage.operators.add(alone)
    proxy = NetworkProfile(name="P", host="10.0.0.2", port=1080)
    storage.network_profiles.add(proxy)

    report = services.bulk.apply("operators", [linked.id, alone.id],
                                 "update", {"network_profile_id": proxy.id})

    assert report["done"] == 2
    assert storage.accounts.get(seeded["account"].id).network_profile_id == proxy.id
    assert storage.operators.get(alone.id).network_profile_id == proxy.id
    assert storage.operators.get(linked.id).network_profile_id is None


def test_operators_can_be_deleted_together_and_are_named_in_a_report(
        services, storage, seeded):
    ops = [Operator(username=f"op{i}") for i in range(2)]
    for op in ops:
        storage.operators.add(op)

    report = services.bulk.apply("operators", [ops[0].id, "op_nope"], "delete")

    assert report["done"] == 1
    assert storage.operators.get(ops[0].id) is None
    assert storage.operators.get(ops[1].id) is not None
    assert report["failed"][0]["id"] == "op_nope"


def test_checking_a_linked_operator_checks_its_account(services, storage,
                                                       seeded):
    linked = services.accounts.promote_to_operator(seeded["account"])

    progress = services.checkup.start("operators", operator_ids=[linked.id])

    assert progress["total"] == 1
    assert storage.accounts.get(seeded["account"].id).last_check_at is not None
