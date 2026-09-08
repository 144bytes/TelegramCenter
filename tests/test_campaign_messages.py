"""Several texts per campaign, and which one a cycle sends.

One cycle picks one message and sends that same message to every channel, so
a pass reads as one posting rather than a mix. The rule that picks it lives on
the model, because the backend is what actually sends.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import Campaign, CampaignMessage, Schedule, Target
from app.models.enums import CampaignState, ScheduleMode, TargetResultStatus
from app.services.campaigns import MESSAGE_CAP, CampaignError
from app.storage import Storage


def _campaign(texts, **fields):
    return Campaign(name="c", account_id="acc",
                    messages=[CampaignMessage(text=t) for t in texts], **fields)


def _sequence(c: Campaign, cycles: int) -> list[str]:
    return [c.pick_message().text for _ in range(cycles)]


def _made(services, storage, seeded, texts, targets=1, gap=0):
    ids = [seeded["target"].id]
    for i in range(targets - 1):
        extra = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra)
        ids.append(extra.id)
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": ids, "interval_min_sec": gap, "interval_max_sec": gap,
        "messages": [{"text": t} for t in texts],
        "schedule": {"mode": "ONCE", "at": "2020-01-01T00:00:00"},
    })


# ── 1. one message ──────────────────────────────────────────────────────
def test_one_message_is_always_the_one():
    c = _campaign(["A"])
    assert _sequence(c, 6) == ["A"] * 6


# ── 2. two messages alternate strictly ──────────────────────────────────
def test_two_messages_alternate_starting_with_the_first():
    c = _campaign(["A", "B"])
    assert _sequence(c, 6) == ["A", "B", "A", "B", "A", "B"]


def test_two_messages_keep_alternating_from_wherever_they_are():
    c = _campaign(["A", "B"])
    c.last_message_id = c.messages[1].id      # as if the last cycle sent B
    assert _sequence(c, 4) == ["A", "B", "A", "B"]


# ── 3-4. three or more never repeat back to back ────────────────────────
@pytest.mark.parametrize("count", [3, 4, 10])
def test_never_the_same_message_twice_in_a_row(count):
    texts = [f"M{i}" for i in range(count)]
    c = _campaign(texts)
    seen = _sequence(c, 400)

    assert all(a != b for a, b in zip(seen, seen[1:])), \
        "a text must never follow itself"
    assert set(seen) == set(texts), "over 400 cycles every text gets used"


def test_three_messages_do_use_randomness():
    """Not a fixed rotation: with three texts the next one is a real choice
    between the two that are not the previous one."""
    c = _campaign(["A", "B", "C"])
    seen = _sequence(c, 200)
    pairs = {(a, b) for a, b in zip(seen, seen[1:])}
    assert len(pairs) > 3, "every allowed transition should show up"


def test_the_first_cycle_of_three_is_free_to_pick_any(monkeypatch):
    c = _campaign(["A", "B", "C"])
    monkeypatch.setattr("app.models.campaign.random.choice", lambda pool: pool[-1])
    assert c.pick_message().text == "C", "no previous cycle excludes nothing"


def test_a_deleted_last_message_does_not_wedge_the_rotation():
    c = _campaign(["A", "B", "C"])
    c.last_message_id = "msg_gone"
    assert c.pick_message() is not None, "an unknown id simply excludes nothing"


def test_a_campaign_with_no_messages_picks_nothing():
    assert Campaign(name="c").pick_message() is None


# ── 5. a fresh text for every channel ───────────────────────────────────
def test_each_channel_of_one_pass_gets_its_own_text(services, storage,
                                                    telegram, seeded):
    """One text repeated across twenty chats inside a few minutes is the
    behaviour being punished. Each chat's own anti-spam sees a single
    message; anything comparing across chats sees one text copied."""
    campaign = _made(services, storage, seeded, ["A", "B", "C"], targets=6)

    asyncio.run(services.scheduler._run(campaign))

    texts = [text for _key, _peer, text in telegram.sent]
    assert len(texts) == 6
    assert len(set(texts)) > 1, f"the whole pass went out as one text: {texts}"


def test_no_channel_gets_the_text_the_one_before_it_got(services, storage,
                                                        telegram, seeded):
    campaign = _made(services, storage, seeded, ["A", "B", "C"], targets=8)

    asyncio.run(services.scheduler._run(campaign))

    texts = [text for _key, _peer, text in telegram.sent]
    assert all(a != b for a, b in zip(texts, texts[1:])),         f"the same text twice in a row: {texts}"


def test_two_texts_alternate_channel_by_channel(services, storage, telegram,
                                                seeded):
    campaign = _made(services, storage, seeded, ["A", "B"], targets=4)

    asyncio.run(services.scheduler._run(campaign))

    assert [text for _k, _p, text in telegram.sent] == ["A", "B", "A", "B"]


def test_one_text_is_simply_sent_every_time(services, storage, telegram,
                                            seeded):
    campaign = _made(services, storage, seeded, ["Only"], targets=3)

    asyncio.run(services.scheduler._run(campaign))

    assert [text for _k, _p, text in telegram.sent] == ["Only"] * 3


def test_the_next_cycle_does_not_repeat_the_last_text_of_the_previous(
        services, storage, telegram, seeded):
    campaign = _made(services, storage, seeded, ["A", "B"], targets=2)
    campaign.schedule = Schedule(mode=ScheduleMode.LOOP)
    storage.campaigns.upsert(campaign)

    asyncio.run(services.scheduler._run(campaign))
    first = [text for _k, _p, text in telegram.sent]
    telegram.sent.clear()

    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))
    second = [text for _k, _p, text in telegram.sent]

    assert first == ["A", "B"]
    assert second[0] != first[-1], "the join between cycles is a join too"


def test_the_choice_survives_a_restart(services, storage, telegram, seeded):
    """`last_message_id` is saved with every target, not held in memory, so
    stopping the app between two channels cannot repeat a text."""
    campaign = _made(services, storage, seeded, ["A", "B"], targets=1)

    asyncio.run(services.scheduler._run(campaign))
    last = storage.campaigns.get(campaign.id).last_message_id

    assert last, "written down"
    reloaded = storage.campaigns.get(campaign.id)
    assert reloaded.pick_message().id != last


# ── 6. campaigns are independent ────────────────────────────────────────
def test_two_campaigns_rotate_independently():
    first = _campaign(["A", "B"])
    second = _campaign(["A", "B"])
    second.pick_message()                      # put them out of step

    assert _sequence(first, 4) == ["A", "B", "A", "B"]
    assert _sequence(second, 3) == ["B", "A", "B"]


def test_one_campaign_running_does_not_touch_another(services, storage,
                                                     telegram, seeded):
    one = _made(services, storage, seeded, ["A", "B"])
    two = _made(services, storage, seeded, ["A", "B"])
    two.name = "second"
    storage.campaigns.upsert(two)

    asyncio.run(services.scheduler._run(one))
    asyncio.run(services.scheduler._run(two))

    assert [text for _k, _p, text in telegram.sent] == ["A", "A"], \
        "each campaign starts its own rotation at its own first message"


# ── 7. the choice survives a restart ────────────────────────────────────
def test_the_last_message_survives_a_reload(services, storage, seeded):
    campaign = _made(services, storage, seeded, ["A", "B", "C"])
    chosen = campaign.pick_message()
    storage.campaigns.upsert(campaign)

    reloaded = Storage().campaigns.get(campaign.id)

    assert reloaded.last_message_id == chosen.id
    assert reloaded.pick_message().id != chosen.id, \
        "the rule still holds after a restart"


def test_a_finished_run_persists_the_choice(services, storage, telegram,
                                            seeded):
    campaign = _made(services, storage, seeded, ["A", "B", "C"])
    asyncio.run(services.scheduler._run(campaign))

    reloaded = Storage().campaigns.get(campaign.id)
    assert reloaded.last_message_id is not None
    assert reloaded.message_for(reloaded.last_message_id).text == \
        telegram.sent[0][2]


# ── 9-11. the cap ───────────────────────────────────────────────────────
# There is no adjustable limit any more. The editor takes a whole pack of
# texts at once, so a slider capping it at ten was in the way rather than
# protecting anything; what is left is one hard cap against a paste that was
# never meant to be one.
def test_the_cap_is_a_hundred():
    assert MESSAGE_CAP == 100


def test_a_pack_under_the_cap_is_accepted(services, storage, seeded):
    campaign = _made(services, storage, seeded,
                     [f"M{i}" for i in range(MESSAGE_CAP)])
    assert len(campaign.messages) == MESSAGE_CAP


def test_a_pack_over_the_cap_is_refused(services, storage, seeded):
    with pytest.raises(CampaignError, match=str(MESSAGE_CAP)):
        _made(services, storage, seeded,
              [f"M{i}" for i in range(MESSAGE_CAP + 1)])


def test_the_refusal_says_what_to_do(services, storage, seeded):
    """A number alone leaves the user counting their own paste."""
    with pytest.raises(CampaignError) as err:
        _made(services, storage, seeded,
              [f"M{i}" for i in range(MESSAGE_CAP + 5)])
    assert err.value.msg == {"code": "err.campaign.too_many_messages",
                             "params": {"n": MESSAGE_CAP + 5, "limit": MESSAGE_CAP}}


def test_an_edit_that_does_not_grow_the_list_is_allowed(services, storage,
                                                        seeded):
    """A campaign already holding more than the cap - hand-edited, or from an
    older build - must still be editable. The user was promised their texts
    would be kept, and a refusal here would mean losing all of them."""
    campaign = _made(services, storage, seeded, ["A", "B", "C"])
    campaign.messages += [CampaignMessage(text=f"X{i}")
                          for i in range(MESSAGE_CAP)]
    storage.campaigns.upsert(campaign)

    services.campaigns.update(campaign, {"name": "renamed"})

    assert len(storage.campaigns.get(campaign.id).messages) == MESSAGE_CAP + 3
    assert storage.campaigns.get(campaign.id).name == "renamed"


def test_a_copy_of_an_over_cap_campaign_is_still_allowed(services, storage,
                                                         seeded):
    campaign = _made(services, storage, seeded, ["A", "B", "C"])
    campaign.messages += [CampaignMessage(text=f"X{i}")
                          for i in range(MESSAGE_CAP)]
    storage.campaigns.upsert(campaign)

    copy = services.campaigns.duplicate(campaign, "копия")

    assert len(copy.messages) == MESSAGE_CAP + 3,         "refusing to copy what is already there would be refusing to copy"


# ── editing ─────────────────────────────────────────────────────────────
def test_ids_that_come_back_are_kept(services, storage, seeded):
    """Minting new ids on every save would quietly reset the rotation."""
    campaign = _made(services, storage, seeded, ["A", "B"])
    before = [m.id for m in campaign.messages]

    services.campaigns.update(campaign, {
        "messages": [{"id": before[0], "text": "A изменено"},
                     {"id": before[1], "text": "B"}]})

    assert [m.id for m in campaign.messages] == before
    assert campaign.messages[0].text == "A изменено"


def test_blank_messages_are_dropped(services, storage, seeded):
    campaign = _made(services, storage, seeded, ["A"])
    services.campaigns.update(campaign, {
        "messages": [{"text": "A"}, {"text": "   "}, {"text": ""}]})
    assert [m.text for m in campaign.messages] == ["A"]


def test_a_campaign_emptied_of_texts_stops_and_says_so(services, storage,
                                                       seeded):
    """Deleting every message used to leave the old ones in place, and the
    campaign went on sending texts the user had deleted."""
    campaign = _made(services, storage, seeded, ["A", "B"])
    services.campaigns.start(campaign)

    services.campaigns.update(campaign, {"messages": []})

    fresh = storage.campaigns.get(campaign.id)
    assert fresh.messages == []
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.last_error["code"] == "stop.no_text"
    assert any(i.code == "campaign.no_text"
               for i in services.state.campaign_issues(fresh))


def test_removing_the_last_used_message_clears_the_pointer(services, storage,
                                                           seeded):
    campaign = _made(services, storage, seeded, ["A", "B"])
    campaign.pick_message()
    keep = campaign.messages[1]

    services.campaigns.update(campaign, {
        "messages": [{"id": keep.id, "text": keep.text}]})

    assert campaign.last_message_id is None


def test_operator_is_checked_in_every_message(services, seeded):
    """Any of the texts may be the one a cycle picks, so any of them naming
    an operator that is not bound has to refuse the save."""
    with pytest.raises(CampaignError, match="err.operator.required"):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id],
            "messages": [{"text": "чисто"}, {"text": "пиши на @operator"}],
        })


# ── 12-13. the rest of the loop is untouched ────────────────────────────
def test_sent_total_keeps_climbing_across_cycles(services, storage, telegram,
                                                 seeded):
    campaign = _made(services, storage, seeded, ["A", "B"], targets=2)
    campaign.schedule = Schedule(mode=ScheduleMode.LOOP)
    storage.campaigns.upsert(campaign)

    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_total == 2

    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_total == 4


def test_a_failed_target_is_retried_on_the_next_cycle(services, storage,
                                                      telegram, seeded):
    campaign = _made(services, storage, seeded, ["A", "B"], targets=3)
    failing = storage.targets.get(campaign.target_ids[1])
    telegram.fail_on.add(failing.username)

    asyncio.run(services.scheduler._run(campaign))
    assert campaign.result_for(failing.id).status == TargetResultStatus.FAILED

    telegram.fail_on.clear()
    telegram.sent.clear()
    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1, "only the failure is retried"
    assert campaign.result_for(failing.id).status == TargetResultStatus.SENT
