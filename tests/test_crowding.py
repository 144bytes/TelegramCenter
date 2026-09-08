"""Accounts crowded behind one address.

Nothing is wrong with any single one of them, which is exactly why nothing
said so: eighteen accounts went out through one IP, every dot was green, and
Telegram froze them one after another. This is the only row on the overview
that is about the shape of the setup rather than about a record.
"""
from __future__ import annotations

from app.models import Account, ApiProfile, NetworkProfile
from app.models.enums import AccountState, ProbeState


def _accounts(storage, n, *, proxy_id=None, api_id=None, disabled=False):
    for i in range(n):
        storage.accounts.add(Account(
            key=f"session_{i}", telegram_id=i, username=f"a{i}",
            api_profile_id=api_id, network_profile_id=proxy_id,
            disabled=disabled, raw_state=AccountState.READY))


def _proxy(storage, name="Main proxy"):
    proxy = NetworkProfile(name=name, host="203.0.113.10", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)
    return proxy


def _codes(services):
    return {i["code"] for row in services.state.problems() for i in row["issues"]}


def test_a_crowd_behind_one_proxy_is_pointed_out(services, storage):
    proxy = _proxy(storage)
    _accounts(storage, 6, proxy_id=proxy.id)

    assert "account.shared_proxy" in _codes(services)


def test_a_spare_and_the_one_in_use_are_not_a_crowd(services, storage):
    proxy = _proxy(storage)
    _accounts(storage, 2, proxy_id=proxy.id)

    assert "account.shared_proxy" not in _codes(services)


def test_switched_off_accounts_do_not_count(services, storage):
    """They are not sending, so they are not what Telegram is looking at."""
    proxy = _proxy(storage)
    _accounts(storage, 6, proxy_id=proxy.id, disabled=True)

    assert "account.shared_proxy" not in _codes(services)


def test_one_api_profile_for_everybody_is_pointed_out_too(services, storage):
    profile = ApiProfile(name="Main API", api_id=1, api_hash="h")
    storage.api_profiles.add(profile)
    _accounts(storage, 5, api_id=profile.id)

    assert "account.shared_api" in _codes(services)


def test_it_is_advice_and_never_a_refusal(services, storage):
    """Sharing a proxy is a legitimate thing to do; only the user knows what
    the accounts are for."""
    profile = ApiProfile(name="Main API", api_id=1, api_hash="h")
    storage.api_profiles.add(profile)
    proxy = _proxy(storage)
    _accounts(storage, 6, proxy_id=proxy.id, api_id=profile.id)

    rows = [r for r in services.state.problems() if r["roles"] == ["advice"]]

    assert rows, "it shows up"
    assert all(i["level"] == "warning" for r in rows for i in r["issues"])
    account = storage.accounts.find(lambda a: True)
    assert services.state.account_effective(account) == "READY", \
        "and no individual account is called broken over it"


def test_the_row_says_how_many_and_which_profile(services, storage):
    proxy = _proxy(storage, name="Единственная прокси")
    _accounts(storage, 7, proxy_id=proxy.id)

    row = next(r for r in services.state.problems()
               if r["issues"][0]["code"] == "account.shared_proxy")

    assert row["issues"][0]["params"]["count"] == 7
    assert row["title"] == "Единственная прокси"
    assert row["route"] == "settings", "the place where profiles are made"
