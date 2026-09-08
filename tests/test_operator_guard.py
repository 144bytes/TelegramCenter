"""@operator must be validated at all five points named in the spec.

The rule that matters most is the last one: whatever state the data is in,
the literal string "@operator" must never be delivered to a user.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import AutoReplyConfig, AutoReplyRule, ConversationState, Operator
from app.models.enums import AutoReplyKind
from app.services.autoreply import ValidationError
from app.services.campaigns import CampaignError


def _bind_operator(storage, account, username="helper"):
    op = Operator(username=username, key="op_1", session_file="op_1.session",
                  telegram_id=1)
    storage.operators.add(op)
    account.operator_id = op.id
    storage.accounts.upsert(account)
    return op


# ── point 1: creating a rule ────────────────────────────────────────────
def test_create_rule_with_operator_rejected(services, seeded):
    with pytest.raises(ValidationError, match="@operator"):
        services.autoreply.save_config(seeded["account"].id, {
            "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
            "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                       "response": "write to @operator"}],
        })


def test_create_rule_with_operator_allowed_once_bound(services, storage, seeded):
    _bind_operator(storage, seeded["account"])
    cfg = services.autoreply.save_config(seeded["account"].id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "write to @operator"}],
    })
    assert cfg.rules[0].enabled is True


# ── point 2: editing a rule ─────────────────────────────────────────────
def test_editing_a_clean_rule_into_a_bad_one_is_rejected(services, seeded):
    account = seeded["account"]
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "hello"}],
    })
    with pytest.raises(ValidationError):
        services.autoreply.save_config(account.id, {
            "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
            "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                       "response": "ask @operator"}],
        })


# ── point 3: enabling a rule ────────────────────────────────────────────
def test_enabling_a_config_with_operator_is_rejected(services, storage, seeded):
    account = seeded["account"]
    cfg = AutoReplyConfig(owner_id=account.id, enabled=False,
                          delay_min_sec=1, delay_max_sec=2,
                          rules=[AutoReplyRule(kind=AutoReplyKind.FAQ, enabled=True,
                                               match="hi",
                                               response="ping @operator")])
    storage.auto_reply.add(cfg)

    with pytest.raises(ValidationError):
        services.autoreply.set_enabled(account.id, True)
    # the rejected switch must not have been half-applied
    assert storage.auto_reply.get(account.id).enabled is False


# ── point 4: unbinding the operator ─────────────────────────────────────
def test_unbinding_operator_disarms_rules(services, storage, seeded):
    account = seeded["account"]
    _bind_operator(storage, account)
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "ask @operator"},
                  {"kind": "FIRST_MESSAGE", "enabled": True, "response": "hello"}],
    })

    services.accounts.set_operator(account, None)

    cfg = storage.auto_reply.get(account.id)
    faq = next(r for r in cfg.rules if r.kind == AutoReplyKind.FAQ)
    first = next(r for r in cfg.rules if r.kind == AutoReplyKind.FIRST_MESSAGE)
    assert faq.enabled is False, "the @operator rule must be switched off"
    assert first.enabled is True, "unrelated rules must be left alone"


def test_deleting_operator_disarms_rules(services, storage, seeded):
    account = seeded["account"]
    op = _bind_operator(storage, account)
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "ask @operator"}],
    })

    services.operators.delete(op.id)

    assert storage.accounts.get(account.id).operator_id is None
    assert storage.auto_reply.get(account.id).rules[0].enabled is False


# ── point 5: immediately before sending ─────────────────────────────────
def test_autoreply_not_sent_when_operator_vanishes_during_delay(
        services, storage, telegram, seeded):
    """The rule was valid when armed. The operator is removed while the reply
    is waiting out its delay. Nothing may be sent."""
    account = seeded["account"]
    op = _bind_operator(storage, account)
    cfg = services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "write to @operator"}],
    })

    # simulate the window closing behind us
    storage.operators.delete(op.id)
    account.operator_id = None
    storage.accounts.upsert(account)

    conv = ConversationState(account_id=account.id, peer_id=5)
    storage.conversations.add(conv)
    rule = cfg.rules[0]
    asyncio.run(services.responder._deliver(
        account, rule, {"peer_id": 5, "account_key": account.key}, conv))

    assert telegram.sent == [], "no message may go out without an operator"


def test_autoreply_substitutes_operator_handle(services, storage, telegram, seeded):
    account = seeded["account"]
    _bind_operator(storage, account, username="supporthero")
    cfg = services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "Please write to @operator, thanks"}],
    })
    conv = ConversationState(account_id=account.id, peer_id=5)
    storage.conversations.add(conv)

    asyncio.run(services.responder._deliver(
        account, cfg.rules[0], {"peer_id": 5, "account_key": account.key}, conv))

    assert len(telegram.sent) == 1
    text = telegram.sent[0][2]
    assert "@supporthero" in text
    assert "@operator" not in text.lower()


def test_campaign_with_operator_rejected_at_save(services, seeded):
    with pytest.raises(CampaignError, match="@operator"):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id],
            "message_text": "hello, ping @operator",
        })


def test_campaign_blocked_before_sending_when_operator_gone(
        services, storage, telegram, seeded):
    account = seeded["account"]
    op = _bind_operator(storage, account)
    campaign = services.campaigns.create({
        "name": "c", "account_id": account.id,
        "target_ids": [seeded["target"].id],
        "message_text": "ping @operator",
    })

    storage.operators.delete(op.id)
    account.operator_id = None
    storage.accounts.upsert(account)

    assert services.scheduler._resolve_text(campaign, account) is None
    assert telegram.sent == []


def test_operator_token_matching_is_case_insensitive(services, seeded):
    with pytest.raises(CampaignError):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id],
            "message_text": "write to @Operator please",
        })
