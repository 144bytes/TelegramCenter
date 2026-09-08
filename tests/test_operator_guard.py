"""@operator in an auto-reply or a campaign.

A rule saying @operator is never switched off by the app: the page warns
while there is no operator. What matters most holds whatever the data says:
the literal string "@operator" is never delivered to anybody.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import ConversationState, Operator
from app.services.campaigns import CampaignError


def _bind_operator(storage, account, username="helper"):
    op = Operator(username=username, key="op_1", session_file="op_1.session",
                  telegram_id=1)
    storage.operators.add(op)
    account.operator_id = op.id
    storage.accounts.upsert(account)
    return op


# ── the page warns; nothing is switched off behind the user's back ─────
def _rules_with_operator(services, account_id):
    return services.autoreply.save_config(account_id, {
        "enabled": True, "delay_min_sec": 1, "delay_max_sec": 2,
        "rules": [{"kind": "FAQ", "enabled": True, "match": "hi",
                   "response": "ask @operator"},
                  {"kind": "FIRST_MESSAGE", "enabled": True, "response": "hello"}],
    })


def test_a_rule_with_operator_saves_and_the_page_warns(services, storage, seeded):
    account = seeded["account"]

    cfg = _rules_with_operator(services, account.id)

    assert all(r.enabled for r in cfg.rules)
    codes = [i["code"] for i in services.state.autoreply_view(cfg)["issues"]]
    assert "operator.required" in codes


def test_create_rule_with_operator_allowed_once_bound(services, storage, seeded):
    _bind_operator(storage, seeded["account"])
    cfg = _rules_with_operator(services, seeded["account"].id)
    assert services.state.operator_gap(seeded["account"].id) is None
    assert cfg.rules[0].enabled is True


def test_unbinding_the_operator_leaves_the_rules_as_they_are(services, storage,
                                                            seeded):
    account = seeded["account"]
    _bind_operator(storage, account)
    _rules_with_operator(services, account.id)

    services.accounts.set_operator(account, None)

    assert all(r.enabled for r in storage.auto_reply.get(account.id).rules)


def test_deleting_the_operator_leaves_the_rules_as_they_are(services, storage,
                                                           seeded):
    account = seeded["account"]
    op = _bind_operator(storage, account)
    _rules_with_operator(services, account.id)

    services.operators.delete(op.id)

    assert storage.accounts.get(account.id).operator_id is None
    assert all(r.enabled for r in storage.auto_reply.get(account.id).rules)


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
    with pytest.raises(CampaignError, match="err.operator.required"):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id],
            "messages": [{"text": "hello, ping @operator"}],
        })


def test_campaign_blocked_before_sending_when_operator_gone(
        services, storage, telegram, seeded):
    account = seeded["account"]
    op = _bind_operator(storage, account)
    campaign = services.campaigns.create({
        "name": "c", "account_id": account.id,
        "target_ids": [seeded["target"].id],
        "messages": [{"text": "ping @operator"}],
    })

    storage.operators.delete(op.id)
    account.operator_id = None
    storage.accounts.upsert(account)

    assert services.scheduler._resolve_text(
        campaign, account, campaign.messages[0]) is None
    assert telegram.sent == []


def test_operator_token_matching_is_case_insensitive(services, seeded):
    with pytest.raises(CampaignError):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id],
            "messages": [{"text": "write to @Operator please"}],
        })
