"""What stops a campaign, and what does not.

Two separate mistakes met here in the field. A check being queued sets every
account to QUEUED before it runs, and "not READY" was reason enough to stop
whatever was sending - so every antispam pass stopped every campaign. And a
campaign stopped that way went to PAUSED with no next run, which is where it
stayed: eighteen of them had to be started by hand afterwards, every time.

The first is the bug, and the fix is telling "wait a moment" apart from
"something is wrong": a queued check leaves the campaign armed and the tick
comes back to it seconds later.

The second is not a bug and is deliberately not fixed here. A campaign that
stopped stays stopped until the user starts it, whatever the reason was and
whether or not the reason has since gone. The app does not decide by itself
that a campaign should be sending again.
"""
from __future__ import annotations

import asyncio

from app.models.enums import (AccountState, CampaignState, SpamState,
                              TargetResultStatus)
from app.services.probing import mark_frozen


def _campaign(services, seeded, **extra):
    payload = {"name": "blast", "account_id": seeded["account"].id,
               "target_ids": [seeded["target"].id],
               "messages": [{"text": "hi"}],
               "interval_min_sec": 0, "interval_max_sec": 0}
    payload.update(extra)
    campaign = services.campaigns.create(payload)
    return services.campaigns.start(campaign)


# ── a check passing through ─────────────────────────────────────────────
def test_queued_for_a_check_is_not_a_reason_to_stop(services, storage, seeded):
    campaign = _campaign(services, seeded)
    seeded["account"].raw_state = AccountState.QUEUED
    storage.accounts.upsert(seeded["account"])

    block = services.state.run_block(campaign)

    assert block is not None
    assert block.transient is True, "a queued check clears by itself"


def test_a_queued_check_leaves_the_campaign_armed(services, storage, telegram,
                                                  seeded):
    """The exact sequence from the field: queue every account, then let the
    scheduler reach a campaign that was about to send."""
    campaign = _campaign(services, seeded)
    seeded["account"].raw_state = AccountState.QUEUED
    storage.accounts.upsert(seeded["account"])

    asyncio.run(services.scheduler._run(campaign))

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.raw_state == CampaignState.SCHEDULED, "not paused over a check"
    assert fresh.next_run_at, "and it still knows when to try again"
    assert telegram.sent == [], "nothing was sent while the account was queued"


def test_the_campaign_sends_once_the_check_is_done(services, storage, telegram,
                                                   seeded):
    campaign = _campaign(services, seeded)
    seeded["account"].raw_state = AccountState.QUEUED
    storage.accounts.upsert(seeded["account"])
    asyncio.run(services.scheduler._run(campaign))

    seeded["account"].raw_state = AccountState.READY
    storage.accounts.upsert(seeded["account"])
    asyncio.run(services.scheduler._run(storage.campaigns.get(campaign.id)))

    assert len(telegram.sent) == 1


# ── a real fault ────────────────────────────────────────────────────────
def test_a_bad_verdict_does_stop_the_campaign(services, storage, seeded):
    """The user wants a limited or blocked account to stop its campaigns so
    they can decide what to do about it."""
    campaign = _campaign(services, seeded)
    seeded["account"].spam_state = SpamState.BLOCKED
    storage.accounts.upsert(seeded["account"])

    asyncio.run(services.scheduler._run(campaign))

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.last_error, "and the card says why"


def test_a_stopped_campaign_stays_stopped_when_the_reason_goes(
        services, storage, telegram, seeded):
    """The app never decides on its own that sending should start again.

    It used to: a campaign it had stopped came back as soon as the fault
    cleared. That is one more thing happening without anybody asking for it,
    and the user manages the life of a campaign by hand.
    """
    campaign = _campaign(services, seeded)
    mark_frozen(seeded["account"], storage, services.bus)
    asyncio.run(services.scheduler._run(campaign))
    assert storage.campaigns.get(campaign.id).raw_state == CampaignState.PAUSED

    services.accounts.set_disabled(seeded["account"], False)   # clears the freeze

    async def tick_and_settle():
        services.scheduler._tick()
        for task in list(services.scheduler._runs.values()):
            await task
    asyncio.run(tick_and_settle())

    assert storage.campaigns.get(campaign.id).raw_state == CampaignState.PAUSED
    assert telegram.sent == [], "waiting for the user, not for the fault"


def test_the_user_starts_it_again_and_it_sends(services, storage, telegram,
                                               seeded):
    campaign = _campaign(services, seeded)
    mark_frozen(seeded["account"], storage, services.bus)
    asyncio.run(services.scheduler._run(campaign))
    services.accounts.set_disabled(seeded["account"], False)

    services.campaigns.start(storage.campaigns.get(campaign.id))

    async def tick_and_settle():
        services.scheduler._tick()
        for task in list(services.scheduler._runs.values()):
            await task
    asyncio.run(tick_and_settle())

    fresh = storage.campaigns.get(campaign.id)
    assert not fresh.last_error, "starting it clears what stopped it"
    assert len(telegram.sent) == 1


def test_a_queued_account_does_not_refuse_a_start(services, storage, seeded):
    """Pressing Start during a check has to work: the check is seconds long
    and the campaign would only have to be pressed again afterwards."""
    campaign = services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}]})
    seeded["account"].raw_state = AccountState.QUEUED
    storage.accounts.upsert(seeded["account"])

    services.campaigns.start(campaign)

    assert campaign.raw_state == CampaignState.SCHEDULED


# ── a channel that is gone ──────────────────────────────────────────────
def test_a_channel_deleted_mid_run_is_skipped_with_a_reason(
        services, storage, seeded):
    """Deleting a channel takes it out of the campaigns, but a run already in
    flight is holding its own copy of the list."""
    campaign = _campaign(services, seeded)
    storage.targets.delete(seeded["target"].id)

    asyncio.run(services.scheduler._run(campaign))

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.status == TargetResultStatus.SKIPPED
    assert result.error["code"] == "result.channel_deleted"
