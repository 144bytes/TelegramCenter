"""A session Telegram has thrown away, and the things that used to cause it.

Telegram invalidates an auth key the moment it sees it connect from two
addresses at once. The app did that to itself in two ways: it copied a session
file when an account was promoted to operator, and it kept a pooled client
alive after the account's API or proxy profile changed underneath it.
"""
from __future__ import annotations

from app import config
from app.models import Account, ApiProfile, Campaign, CampaignMessage, NetworkProfile, Operator
from app.models.enums import AccountState, EffectiveState
from app.services.probing import apply_probe_result
from app.state import StateManager
from app.telegram.errors import is_dead_session, tg_error

AUTH_KEY_DUPLICATED = (
    "AuthKeyDuplicatedError: The authorization key (session file) was used "
    "under two different IP addresses simultaneously, and can no longer be "
    "used. Use the same session exclusively, or use different sessions "
    "(caused by GetUsersRequest)")


class AuthKeyDuplicatedError(Exception):
    """Named to match Telethon's, which is what the table looks up."""


# ── recognising it ──────────────────────────────────────────────────────
def test_the_error_has_wording_of_its_own():
    message = tg_error(AuthKeyDuplicatedError("two IPs"))
    assert message["code"] == "tg.AuthKeyDuplicatedError"
    assert is_dead_session(message), "and it is still recognised"


def test_a_stored_failure_is_recognised_from_its_text():
    assert is_dead_session(AUTH_KEY_DUPLICATED)
    assert not is_dead_session("ConnectionError: timed out")
    assert not is_dead_session(None)


def test_a_probe_result_becomes_a_dead_session_not_a_plain_error():
    account = Account(key="session_1")
    apply_probe_result(account, {"ok": False, "detail": AUTH_KEY_DUPLICATED})

    assert account.raw_state == AccountState.AUTH_DEAD
    assert account.raw_state != AccountState.ERROR


def test_an_ordinary_failure_is_still_an_ordinary_error():
    account = Account(key="session_1")
    apply_probe_result(account, {"ok": False, "detail": "TimeoutError: slow"})
    assert account.raw_state == AccountState.ERROR


# ── what the interface is told ──────────────────────────────────────────
def _dead_account(storage):
    profile = ApiProfile(name="Main", api_id=1, api_hash="h")
    storage.api_profiles.add(profile)
    account = Account(key="session_7", telegram_id=7, username="dead",
                      api_profile_id=profile.id,
                      raw_state=AccountState.AUTH_DEAD,
                      last_error=AUTH_KEY_DUPLICATED)
    storage.accounts.add(account)
    return account


def test_the_account_reads_as_a_dead_session_rather_than_an_error(storage):
    account = _dead_account(storage)
    view = StateManager(storage).account_view(account)

    assert view["effective"] == EffectiveState.AUTH_DEAD
    assert "account.auth_dead" in [i["code"] for i in view["issues"]]


def test_an_operator_reads_the_same_way(storage):
    profile = ApiProfile(name="Main", api_id=1, api_hash="h")
    storage.api_profiles.add(profile)
    operator = Operator(username="op", key="op_7", telegram_id=7,
                        api_profile_id=profile.id,
                        raw_state=AccountState.AUTH_DEAD,
                        last_error=AUTH_KEY_DUPLICATED)
    storage.operators.add(operator)

    assert StateManager(storage).operator_view(operator)["effective"] == (
        EffectiveState.AUTH_DEAD)


def test_a_campaign_on_a_dead_session_is_blocked_with_that_reason(storage):
    account = _dead_account(storage)
    campaign = Campaign(name="c", account_id=account.id,
                        messages=[CampaignMessage(text="hi")],
                        target_ids=[])
    storage.campaigns.add(campaign)

    block = StateManager(storage).run_block(campaign)

    assert block is not None and not block.transient
    assert block.reason["code"] == "issue.campaign.account_auth_dead"


# ── cause 1: the session used to be copied ──────────────────────────────
def test_promoting_shares_the_session_instead_of_copying_it(
        services, storage, seeded):
    account = seeded["account"]
    operator = services.accounts.promote_to_operator(account)

    assert operator.account_id == account.id and operator.key == "",         "a link to the account, not a key of its own"
    copies = list(config.OPERATOR_SESSIONS_DIR.glob("*.session"))
    assert copies == [], "no second file holding the same auth key"


def test_deleting_the_operator_does_not_take_the_account_down(
        services, storage, telegram, seeded):
    account = seeded["account"]
    operator = services.accounts.promote_to_operator(account)

    services.operators.delete(operator.id)

    assert telegram.removed == [], "the account is still using that session"
    assert (config.CAMPAIGN_SESSIONS_DIR / f"{account.key}.session").is_file()


def test_deleting_the_account_can_keep_its_operator_as_a_handle(
        services, storage, telegram, seeded):
    account = seeded["account"]
    operator = services.accounts.promote_to_operator(account)

    services.accounts.delete(account.id, keep_operator=True)

    fresh = storage.operators.get(operator.id)
    assert fresh is not None and fresh.account_id is None
    assert fresh.username == "sender", "the handle it had through the account"
    assert not (config.CAMPAIGN_SESSIONS_DIR / f"{account.key}.session").exists()


def test_deleting_the_account_takes_its_operator_by_default(
        services, storage, telegram, seeded):
    account = seeded["account"]
    operator = services.accounts.promote_to_operator(account)

    services.accounts.delete(account.id)

    assert storage.operators.get(operator.id) is None


def test_the_last_owner_does_release_the_session(services, storage, telegram,
                                                 seeded):
    account = seeded["account"]
    services.accounts.delete(account.id)

    assert telegram.removed == [account.key]
    assert not (config.CAMPAIGN_SESSIONS_DIR / f"{account.key}.session").is_file()


# ── cause 2: the pooled client outlived its settings ────────────────────
def test_changing_the_api_profile_drops_the_pooled_client(
        services, storage, telegram, seeded):
    other = ApiProfile(name="Other", api_id=2, api_hash="h2")
    storage.api_profiles.add(other)

    services.accounts.update(seeded["account"], api_profile_id=other.id)

    assert telegram.invalidated == [seeded["account"].key]


def test_changing_the_proxy_drops_the_pooled_client(services, storage,
                                                    telegram, seeded):
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080)
    storage.network_profiles.add(proxy)

    services.accounts.update(seeded["account"], network_profile_id=proxy.id)

    assert telegram.invalidated == [seeded["account"].key]


def test_an_unrelated_edit_leaves_the_client_alone(services, telegram, seeded):
    services.accounts.update(seeded["account"], first_name="Renamed")
    assert telegram.invalidated == []


def test_switching_a_proxy_off_drops_the_clients_that_used_it(
        services, storage, telegram, seeded):
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080, enabled=True)
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    storage.accounts.upsert(account)
    telegram.invalidated.clear()

    services.profiles.update_proxy(proxy, enabled=False)

    assert account.key in telegram.invalidated


def test_editing_an_api_profile_drops_the_clients_that_used_it(
        services, storage, telegram, seeded):
    services.profiles.update_api(seeded["profile"], api_hash="new-hash")
    assert seeded["account"].key in telegram.invalidated


def test_renaming_a_profile_reconnects_nothing(services, storage, telegram,
                                               seeded):
    """A name is not what a connection is built from: every account behind
    the proxy reconnecting for it was a pointless round of new logins."""
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080)
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    storage.accounts.upsert(account)
    telegram.invalidated.clear()

    services.profiles.update_proxy(proxy, name="Frankfurt", host="1.2.3.4")
    services.profiles.update_api(seeded["profile"], name="Main, renamed")

    assert telegram.invalidated == []
    assert storage.network_profiles.get(proxy.id).name == "Frankfurt"


def test_an_operator_changing_its_proxy_drops_its_client(services, storage,
                                                         telegram):
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080)
    storage.network_profiles.add(proxy)
    operator = Operator(username="op", key="op_5", telegram_id=5)
    storage.operators.add(operator)

    services.operators.update(operator, network_profile_id=proxy.id)

    assert telegram.invalidated == ["op_5"]


# ── the pool itself ─────────────────────────────────────────────────────
def test_invalidate_removes_the_client_from_the_pool():
    from app.telegram.service import TelegramService

    class _Client:
        def __init__(self):
            self.disconnected = False

        def is_connected(self):
            return True

    svc = TelegramService()
    svc.clients["session_1"] = _Client()
    svc._incoming[("autoreply", "session_1")] = object()

    svc.invalidate("session_1")

    assert "session_1" not in svc.clients, "the next use must build a new one"
    assert svc._incoming == {}


def test_invalidate_on_an_unknown_key_is_harmless():
    from app.telegram.service import TelegramService
    TelegramService().invalidate("nothing_here")
