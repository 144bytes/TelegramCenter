"""Everything the account's window changes is one save.

It used to be two requests - the profiles, then the switch - and the own
auto-reply went on its own the moment it was clicked. In between the app
acted on half of the change: an account switched off and moved to a new
proxy first reconnected through that proxy. Now the window sends what it
changed, once, and AccountService.edit applies it in the one safe order.
"""
from __future__ import annotations

import pytest

from app.messages import AppError
from app.models import ApiProfile, NetworkProfile, Operator


def _proxy(storage):
    proxy = NetworkProfile(name="New", host="10.0.0.9", port=1080)
    storage.network_profiles.add(proxy)
    return proxy


def _own_reply(services, account):
    services.autoreply.save_config(account.id, {
        "rules": [{"kind": "FAQ", "match": "a", "response": "b"}]})


def _steps(services, monkeypatch):
    """What `edit` did, in order, and whether the account was off just then."""
    seen = []
    accounts = services.accounts
    for name in ("set_disabled", "update", "set_operator"):
        original = getattr(accounts, name)

        def watch(account, *args, _name=name, _original=original, **kwargs):
            seen.append((_name, account.disabled))
            return _original(account, *args, **kwargs)

        monkeypatch.setattr(accounts, name, watch)
    return seen


def test_everything_the_window_changed_is_saved_at_once(services, storage,
                                                        seeded):
    account = seeded["account"]
    _own_reply(services, account)
    api = ApiProfile(name="Other", api_id=5, api_hash="x" * 32)
    storage.api_profiles.add(api)
    proxy = _proxy(storage)
    operator = Operator(username="support")
    storage.operators.add(operator)

    services.accounts.edit(account, {
        "api_profile_id": api.id, "network_profile_id": proxy.id,
        "operator_id": operator.id, "disabled": True, "autoreply": False})

    fresh = storage.accounts.get(account.id)
    assert (fresh.api_profile_id, fresh.network_profile_id,
            fresh.operator_id) == (api.id, proxy.id, operator.id)
    assert fresh.disabled is True
    assert storage.auto_reply.get(account.id).enabled is False


def test_switching_off_comes_before_the_new_proxy(services, storage, seeded,
                                                  monkeypatch):
    """Otherwise the account reconnects through the new proxy on its way out."""
    seen = _steps(services, monkeypatch)

    services.accounts.edit(seeded["account"], {
        "network_profile_id": _proxy(storage).id, "disabled": True})

    assert seen == [("set_disabled", False), ("update", True)]


def test_switching_on_comes_after_the_new_proxy(services, storage, seeded,
                                                monkeypatch):
    """So the first connection is made with the whole change in place."""
    account = seeded["account"]
    account.disabled = True
    storage.accounts.upsert(account)
    seen = _steps(services, monkeypatch)

    services.accounts.edit(account, {
        "network_profile_id": _proxy(storage).id, "disabled": False})

    assert seen == [("update", True), ("set_disabled", True)]
    assert account.disabled is False


def test_only_what_was_sent_is_touched(services, storage, seeded, monkeypatch):
    account = seeded["account"]
    profile = account.api_profile_id
    seen = _steps(services, monkeypatch)

    services.accounts.edit(account, {"id": account.id, "operator_id": ""})

    assert seen == [("set_operator", False)]
    assert account.api_profile_id == profile
    assert account.operator_id is None


def test_a_name_that_does_not_exist_changes_nothing(services, storage, seeded):
    account = seeded["account"]
    _own_reply(services, account)

    with pytest.raises(AppError) as refused:
        services.accounts.edit(account, {
            "disabled": True, "autoreply": False,
            "network_profile_id": "net_nope"})

    assert refused.value.msg["code"] == "err.not_found.profile"
    assert account.disabled is False
    assert storage.auto_reply.get(account.id).enabled is True


def test_an_auto_reply_that_refuses_changes_nothing(services, storage, seeded):
    """An account without rules of its own has no switch of its own."""
    account = seeded["account"]
    proxy = _proxy(storage)

    with pytest.raises(AppError) as refused:
        services.accounts.edit(account, {
            "network_profile_id": proxy.id, "disabled": True,
            "autoreply": True})

    assert refused.value.msg["code"] == "err.autoreply.not_set_up"
    assert account.network_profile_id is None
    assert account.disabled is False
