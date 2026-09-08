"""Auto-reply: rule shape, selection, delay and inheritance."""
from __future__ import annotations

import asyncio

import pytest

from app.models import ConversationState
from app.models.enums import AutoReplyKind, GLOBAL_OWNER
from app.services.autoreply import ValidationError
from app.util import now_iso


def _save(services, owner, rules, enabled=True, lo=0, hi=0):
    return services.autoreply.save_config(owner, {
        "enabled": enabled, "delay_min_sec": lo, "delay_max_sec": hi,
        "rules": rules,
    })


# ── shape ───────────────────────────────────────────────────────────────
def test_only_one_first_message_rule(services, seeded):
    with pytest.raises(ValidationError):
        _save(services, seeded["account"].id, [
            {"kind": "FIRST_MESSAGE", "response": "a"},
            {"kind": "FIRST_MESSAGE", "response": "b"},
        ])


def test_only_one_periodic_rule(services, seeded):
    with pytest.raises(ValidationError):
        _save(services, seeded["account"].id, [
            {"kind": "PERIODIC", "response": "a"},
            {"kind": "PERIODIC", "response": "b"},
        ])


def test_faq_limit_is_enforced(services, storage, seeded):
    storage.settings.set("autoreply.faq_limit", 2)
    rules = [{"kind": "FAQ", "match": f"k{i}", "response": f"r{i}"} for i in range(3)]
    with pytest.raises(ValidationError, match="лимит"):
        _save(services, seeded["account"].id, rules)


def test_faq_limit_of_zero_forbids_any_faq(services, storage, seeded):
    storage.settings.set("autoreply.faq_limit", 0)
    with pytest.raises(ValidationError):
        _save(services, seeded["account"].id,
              [{"kind": "FAQ", "match": "k", "response": "r"}])


def test_blank_trailing_rows_are_dropped(services, seeded):
    cfg = _save(services, seeded["account"].id, [
        {"kind": "FAQ", "match": "price", "response": "500"},
        {"kind": "FAQ", "match": "", "response": ""},
    ])
    assert len(cfg.rules) == 1


def test_inverted_delay_rejected(services, seeded):
    # needs a real rule: an empty set means "go back to the shared default",
    # and nothing is stored to validate in that case
    with pytest.raises(ValidationError):
        _save(services, seeded["account"].id,
              [{"kind": "FAQ", "match": "a", "response": "b"}], lo=100, hi=10)


# ── inheritance ─────────────────────────────────────────────────────────
def test_account_without_config_inherits_the_global_one(services, storage, seeded):
    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "shared"}])
    account = seeded["account"]

    assert storage.has_own_autoreply(account.id) is False
    assert storage.autoreply_for(account.id).owner_id == GLOBAL_OWNER


def test_global_changes_reach_inheriting_accounts_immediately(
        services, storage, seeded):
    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "v1"}])
    account = seeded["account"]
    assert storage.autoreply_for(account.id).rules[0].response == "v1"

    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "v2"}])

    # the link is live on purpose — no copying, no staleness
    assert storage.autoreply_for(account.id).rules[0].response == "v2"


def test_own_config_overrides_the_global_one(services, storage, seeded):
    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "shared"}])
    account = seeded["account"]
    _save(services, account.id, [{"kind": "FIRST_MESSAGE", "response": "mine"}])

    assert storage.autoreply_for(account.id).rules[0].response == "mine"


def test_reset_returns_an_account_to_the_global_config(services, storage, seeded):
    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "shared"}])
    account = seeded["account"]
    _save(services, account.id, [{"kind": "FIRST_MESSAGE", "response": "mine"}])

    services.autoreply.reset_to_global(account.id)

    assert storage.has_own_autoreply(account.id) is False
    assert storage.autoreply_for(account.id).rules[0].response == "shared"


# ── selection ───────────────────────────────────────────────────────────
def _conv(storage, account, **kw):
    conv = ConversationState(account_id=account.id, peer_id=5, **kw)
    storage.conversations.add(conv)
    return conv


def test_faq_beats_the_first_message_rule(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id, [
        {"kind": "FIRST_MESSAGE", "response": "welcome"},
        {"kind": "FAQ", "match": "price", "response": "500 rub"},
    ])
    conv = _conv(storage, account)

    rule = services.responder.pick(cfg, "what is the price?", conv)
    assert rule.kind == AutoReplyKind.FAQ


def test_first_message_fires_only_once_per_peer(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id, [{"kind": "FIRST_MESSAGE", "response": "hi"}])

    conv = _conv(storage, account)
    assert services.responder.pick(cfg, "hello", conv).kind == \
        AutoReplyKind.FIRST_MESSAGE

    conv.first_replied_at = now_iso()
    assert services.responder.pick(cfg, "hello again", conv) is None


def test_periodic_waits_out_the_delay_window(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id,
                [{"kind": "PERIODIC", "response": "we got it"}], lo=60, hi=120)
    conv = _conv(storage, account, first_replied_at=now_iso())

    assert services.responder.pick(cfg, "hey", conv).kind == AutoReplyKind.PERIODIC

    conv.last_periodic_at = now_iso()
    assert services.responder.pick(cfg, "hey again", conv) is None


def test_several_matching_faq_rules_are_all_reachable(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id, [
        {"kind": "FAQ", "match": "hi", "response": "A"},
        {"kind": "FAQ", "match": "hi", "response": "B"},
    ])
    conv = _conv(storage, account, first_replied_at=now_iso())

    seen = {services.responder.pick(cfg, "hi there", conv).response
            for _ in range(60)}
    assert seen == {"A", "B"}, "a single matching rule must not monopolise"


def test_rules_with_empty_response_are_ignored(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id, [{"kind": "FAQ", "match": "hi", "response": "x"}])
    cfg.rules[0].response = "   "
    conv = _conv(storage, account, first_replied_at=now_iso())
    assert services.responder.pick(cfg, "hi", conv) is None


def test_disabled_rules_are_ignored(services, storage, seeded):
    account = seeded["account"]
    cfg = _save(services, account.id, [{"kind": "FAQ", "match": "hi", "response": "x"}])
    cfg.rules[0].enabled = False
    conv = _conv(storage, account, first_replied_at=now_iso())
    assert services.responder.pick(cfg, "hi", conv) is None


# ── delay ───────────────────────────────────────────────────────────────
def test_delay_comes_from_the_account_range(services, seeded):
    cfg = _save(services, seeded["account"].id,
                [{"kind": "FAQ", "match": "a", "response": "b"}], lo=30, hi=45)
    values = {services.responder._delay_for(cfg, cfg.rules[0]) for _ in range(80)}
    assert values, "a delay must be produced"
    assert min(values) >= 30 and max(values) <= 45


def test_rule_delay_overrides_the_account_range(services, seeded):
    cfg = _save(services, seeded["account"].id, [
        {"kind": "FAQ", "match": "a", "response": "b",
         "delay_min_sec": 5, "delay_max_sec": 5},
    ], lo=300, hi=600)
    assert services.responder._delay_for(cfg, cfg.rules[0]) == 5


def test_default_delay_range_comes_from_settings(services, storage, seeded):
    storage.settings.update({"autoreply.default_delay_min_sec": 111,
                             "autoreply.default_delay_max_sec": 222})
    cfg = services.autoreply.save_config(seeded["account"].id, {
        "enabled": False, "rules": []})
    assert (cfg.delay_min_sec, cfg.delay_max_sec) == (111, 222)


# ── listener wiring ─────────────────────────────────────────────────────
def test_listeners_attach_only_for_enabled_accounts(services, telegram, seeded):
    account = seeded["account"]
    asyncio.run(services.responder.refresh())
    assert telegram.attached_keys() == set(), "nothing enabled yet"

    _save(services, account.id, [{"kind": "FAQ", "match": "a", "response": "b"}])
    asyncio.run(services.responder.refresh())
    assert telegram.attached_keys() == {account.key}


def test_listener_detaches_when_autoreply_is_switched_off(
        services, storage, telegram, seeded):
    account = seeded["account"]
    _save(services, account.id, [{"kind": "FAQ", "match": "a", "response": "b"}])
    asyncio.run(services.responder.refresh())
    assert telegram.attached_keys() == {account.key}

    cfg = storage.auto_reply.get(account.id)
    cfg.enabled = False
    storage.auto_reply.upsert(cfg)
    asyncio.run(services.responder.refresh())

    assert telegram.attached_keys() == set()


def test_refresh_is_idempotent(services, telegram, seeded):
    account = seeded["account"]
    _save(services, account.id, [{"kind": "FAQ", "match": "a", "response": "b"}])
    for _ in range(4):
        asyncio.run(services.responder.refresh())
    assert telegram.attached_keys() == {account.key}
    assert len(telegram.attached) == 1, "handlers must never stack"


# ── inheriting back when the fields are emptied ─────────────────────────
def test_clearing_every_rule_returns_the_account_to_the_shared_default(
        services, storage, seeded):
    account = seeded["account"]
    _save(services, GLOBAL_OWNER, [{"kind": "FIRST_MESSAGE", "response": "shared"}])
    _save(services, account.id, [{"kind": "FIRST_MESSAGE", "response": "mine"}])
    assert storage.has_own_autoreply(account.id) is True

    # the user wiped the fields: nothing distinguishes it from the default
    _save(services, account.id, [])

    assert storage.has_own_autoreply(account.id) is False
    assert storage.autoreply_for(account.id).rules[0].response == "shared"


def test_saving_nothing_never_creates_an_empty_account_config(services, storage,
                                                              seeded):
    _save(services, seeded["account"].id, [])
    assert storage.has_own_autoreply(seeded["account"].id) is False


def test_the_global_default_may_be_emptied_without_vanishing(services, storage):
    _save(services, GLOBAL_OWNER, [{"kind": "FAQ", "match": "a", "response": "b"}])
    _save(services, GLOBAL_OWNER, [])
    assert storage.auto_reply.get(GLOBAL_OWNER) is not None


def test_a_first_rule_set_arrives_switched_on(services, storage, seeded):
    account = seeded["account"]
    cfg = services.autoreply.save_config(account.id, {
        "rules": [{"kind": "FAQ", "match": "a", "response": "b"}]})
    assert cfg.enabled is True, "adding the first rule should make it live"


def test_the_users_own_switch_choice_is_kept_on_later_saves(services, storage,
                                                            seeded):
    account = seeded["account"]
    services.autoreply.save_config(account.id, {
        "rules": [{"kind": "FAQ", "match": "a", "response": "b"}]})
    services.autoreply.set_enabled(account.id, False)

    # a later edit that does not mention `enabled` must not silently re-arm it
    cfg = services.autoreply.save_config(account.id, {
        "rules": [{"kind": "FAQ", "match": "a", "response": "c"}]})
    assert cfg.enabled is False


# ── one answer per burst ────────────────────────────────────────────────
def _flood(services, account, text="привет", count=20, peer=5):
    async def run():
        payloads = [{"account_key": account.key, "peer_id": peer,
                     "sender_name": "Client", "sender_username": "client",
                     "message_id": i, "text": text, "out": False,
                     "date": ""} for i in range(count)]
        await asyncio.gather(*(services.responder._handle(p) for p in payloads))
    asyncio.run(run())


def test_a_burst_of_the_same_keyword_gets_exactly_one_reply(
        services, storage, telegram, seeded):
    """100 "привет" must not produce 100 answers."""
    account = seeded["account"]
    _save(services, account.id,
          [{"kind": "FAQ", "match": "привет", "response": "здравствуйте"}])
    services.responder._running = True

    _flood(services, account, count=25)

    assert len(telegram.sent) == 1


def test_a_later_burst_earns_a_fresh_reply(services, storage, telegram, seeded):
    """Once the answer is out, writing again gets one more - just one."""
    account = seeded["account"]
    _save(services, account.id,
          [{"kind": "FAQ", "match": "привет", "response": "здравствуйте"}])
    services.responder._running = True

    _flood(services, account, count=15)
    _flood(services, account, count=15)

    assert len(telegram.sent) == 2


def test_the_guard_is_released_even_when_sending_fails(
        services, storage, telegram, seeded):
    account = seeded["account"]
    _save(services, account.id,
          [{"kind": "FAQ", "match": "привет", "response": "здравствуйте"}])
    services.responder._running = True
    telegram.fail_on.add(5)

    _flood(services, account, count=5)
    assert telegram.sent == []
    assert services.responder._inflight == set(), "a failure must not wedge the guard"

    telegram.fail_on.clear()
    _flood(services, account, count=5)
    assert len(telegram.sent) == 1


def test_different_people_are_answered_independently(
        services, storage, telegram, seeded):
    account = seeded["account"]
    _save(services, account.id,
          [{"kind": "FAQ", "match": "привет", "response": "здравствуйте"}])
    services.responder._running = True

    _flood(services, account, count=10, peer=5)
    _flood(services, account, count=10, peer=6)

    assert len(telegram.sent) == 2, "the guard is per person, not global"
