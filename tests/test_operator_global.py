"""`@operator` in the shared auto-reply.

The token is substituted at the moment of sending, against the account that
received the message - never against the config that holds the text. The
shared default is used by every account that has no config of its own, so it
has no operator and never could have one. Asking it for one is what made a
perfectly working global reply refuse to save.
"""
from __future__ import annotations

import asyncio

from app.models import Account, ConversationState, Operator
from app.models.enums import AccountState, EffectiveState, GLOBAL_OWNER, IssueLevel


def _operator(storage, username, key):
    op = Operator(username=username, key=key, session_file=f"{key}.session",
                  telegram_id=abs(hash(key)) % 10_000)
    storage.operators.add(op)
    return op


def _account(storage, seeded, username, operator=None, **fields):
    acc = Account(key=f"session_{username}", session_file=f"session_{username}.session",
                  username=username, api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY,
                  operator_id=operator.id if operator else None)
    for k, v in fields.items():
        setattr(acc, k, v)
    storage.accounts.add(acc)
    return acc


def _bind(storage, account, username="helper"):
    op = _operator(storage, username, f"op_{username}")
    account.operator_id = op.id
    storage.accounts.upsert(account)
    return op


GLOBAL_RULES = {
    "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
    "rules": [{"kind": "PERIODIC", "enabled": True,
               "response": "Напиши на @operator, спасибо"}],
}


# ── the bug: every account had an operator and it still refused ─────────
def test_global_reply_saves_when_every_account_has_an_operator(
        services, storage, seeded):
    _bind(storage, seeded["account"], "first")
    _bind(storage, _account(storage, seeded, "second"), "other")

    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)

    assert cfg.rules[0].enabled is True
    assert services.state.autoreply_issues(cfg) == []
    assert services.state.autoreply_effective(cfg) == EffectiveState.READY


def test_global_reply_can_be_switched_on(services, storage, seeded):
    _bind(storage, seeded["account"])
    services.autoreply.save_config(GLOBAL_OWNER, {**GLOBAL_RULES, "enabled": False})

    cfg = services.autoreply.set_enabled(GLOBAL_OWNER, True)

    assert cfg.enabled is True


# ── partial coverage: a warning, never a refusal ────────────────────────
def test_an_account_without_an_operator_only_warns(services, storage, seeded):
    _bind(storage, seeded["account"], "first")
    _account(storage, seeded, "lonely")          # no operator

    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)
    issues = services.state.autoreply_issues(cfg)

    assert [i.code for i in issues] == ["operator.partial_coverage"]
    assert issues[0].level == IssueLevel.WARNING
    assert issues[0].params == {"missing": 1, "total": 2}
    # a warning must not block: the accounts that do have an operator answer
    assert services.state.autoreply_effective(cfg) == EffectiveState.READY


def test_the_warning_counts_only_accounts_that_use_the_shared_default(
        services, storage, seeded):
    """An account with a config of its own never sends the shared text, so a
    missing operator there is not the shared default's problem."""
    _bind(storage, seeded["account"], "first")
    own = _account(storage, seeded, "has_own")
    services.autoreply.save_config(own.id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "PERIODIC", "enabled": True, "response": "мой ответ"}],
    })

    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)

    assert services.state.autoreply_issues(cfg) == []


def test_a_switched_off_account_is_not_counted(services, storage, seeded):
    _bind(storage, seeded["account"], "first")
    _account(storage, seeded, "parked", disabled=True)

    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)

    assert services.state.autoreply_issues(cfg) == []


def test_one_warning_however_many_rules_say_operator(services, storage, seeded):
    _account(storage, seeded, "lonely")

    cfg = services.autoreply.save_config(GLOBAL_OWNER, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [
            {"kind": "FIRST_MESSAGE", "enabled": True, "response": "@operator 1"},
            {"kind": "PERIODIC", "enabled": True, "response": "@operator 2"},
            {"kind": "FAQ", "enabled": True, "match": "hi",
             "response": "@operator 3"},
        ],
    })

    codes = [i.code for i in services.state.autoreply_issues(cfg)]
    assert codes.count("operator.partial_coverage") == 1


# ── an account's own config still refuses ───────────────────────────────
def test_an_account_config_without_an_operator_is_saved_with_a_red_mark(
        services, seeded):
    cfg = services.autoreply.save_config(seeded["account"].id, GLOBAL_RULES)
    issues = services.state.autoreply_view(cfg)["issues"]
    assert [i["level"] for i in issues if i["code"] == "operator.required"]         == ["error"]


def test_the_account_gap_is_an_error_the_global_one_is_a_warning(
        services, storage, seeded):
    account = seeded["account"]

    own = services.state.operator_gap(account.id)
    assert own is not None and own.level == IssueLevel.ERROR

    shared = services.state.operator_gap(GLOBAL_OWNER)
    assert shared is not None and shared.level == IssueLevel.WARNING


def test_no_gap_at_all_once_the_operator_is_bound(services, storage, seeded):
    _bind(storage, seeded["account"])
    assert services.state.operator_gap(seeded["account"].id) is None
    assert services.state.operator_gap(GLOBAL_OWNER) is None


# ── and the runtime does the right thing per account ────────────────────
def _incoming(account, text="привет"):
    return {"account_key": account.key, "peer_id": 5, "sender_id": 5,
            "sender_name": "Client", "sender_username": "client",
            "sender_bot": False, "message_id": 1, "text": text,
            "media": None, "reply_to": None, "date": "", "out": False,
            "private": True}


def _conversation(storage, account):
    conv = ConversationState(account_id=account.id, peer_id=5, peer_name="Client")
    storage.conversations.add(conv)
    return conv


def test_each_account_substitutes_its_own_operator(services, storage, telegram,
                                                   seeded):
    """The whole point of the shared text: one rule, a different handle per
    account."""
    first = seeded["account"]
    _bind(storage, first, "alice")
    second = _account(storage, seeded, "second")
    _bind(storage, second, "bob")
    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)
    rule = cfg.rules[0]

    for account in (first, second):
        asyncio.run(services.responder._deliver(
            account, rule, _incoming(account), _conversation(storage, account)))

    sent = {key: text for key, _peer, text in telegram.sent}
    assert "@alice" in sent[first.key]
    assert "@bob" in sent[second.key]
    assert "@operator" not in sent[first.key].lower()


def test_an_account_without_an_operator_simply_stays_silent(
        services, storage, telegram, seeded):
    """Validation point 5 still holds: the literal token never goes out, and
    the account that cannot resolve it sends nothing at all."""
    _bind(storage, seeded["account"], "alice")
    lonely = _account(storage, seeded, "lonely")
    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)

    asyncio.run(services.responder._deliver(
        lonely, cfg.rules[0], _incoming(lonely), _conversation(storage, lonely)))

    assert telegram.sent == []


# ── what the interface is handed ────────────────────────────────────────
def test_the_views_carry_the_answer_so_the_page_never_derives_it(
        services, storage, seeded):
    _account(storage, seeded, "lonely")

    account_view = services.state.account_view(seeded["account"])
    assert account_view["operator_gap"]["code"] == "operator.required"

    cfg = services.autoreply.save_config(GLOBAL_OWNER, GLOBAL_RULES)
    shared_view = services.state.autoreply_view(cfg)
    assert shared_view["operator_gap"]["code"] == "operator.partial_coverage"

    _bind(storage, seeded["account"])
    assert services.state.account_view(seeded["account"])["operator_gap"] is None
