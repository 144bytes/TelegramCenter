"""Persistence: nested dataclasses, atomic writes, session routing."""
from __future__ import annotations

import json

from app.models import Campaign, CampaignTargetResult, Schedule
from app.models.enums import ScheduleMode, TargetResultStatus
from app.storage import Storage


def test_nested_dataclasses_survive_a_round_trip(storage, app_dir):  # noqa: ARG001
    campaign = Campaign(
        name="nested", account_id="acc", target_ids=["t1", "t2"],
        message_text="hi",
        schedule=Schedule(mode=ScheduleMode.DAILY, times=["09:00", "21:00"]),
        results=[CampaignTargetResult(target_id="t1",
                                      status=TargetResultStatus.SENT, attempts=2)])
    storage.campaigns.add(campaign)

    reloaded = Storage().campaigns.get(campaign.id)
    assert isinstance(reloaded.schedule, Schedule)
    assert reloaded.schedule.times == ["09:00", "21:00"]
    assert isinstance(reloaded.results[0], CampaignTargetResult)
    assert reloaded.results[0].status == TargetResultStatus.SENT
    assert reloaded.results[0].attempts == 2


def test_unknown_fields_are_ignored_not_fatal(storage, app_dir):  # noqa: ARG001
    payload = {"campaigns": [{"id": "camp_x", "name": "old", "legacy_field": 1,
                              "target_ids": ["a"]}]}
    storage.campaigns.path.write_text(json.dumps(payload), encoding="utf-8")
    reloaded = Storage().campaigns.get("camp_x")
    assert reloaded is not None
    assert reloaded.name == "old"
    assert not hasattr(reloaded, "legacy_field")


def test_a_corrupt_file_does_not_crash_startup(storage, app_dir):  # noqa: ARG001
    storage.campaigns.path.write_text("{ this is not json", encoding="utf-8")
    assert Storage().campaigns.all() == []


def test_writes_are_atomic_and_leave_no_temp_file(storage, seeded):  # noqa: ARG001
    storage.accounts.save()
    leftovers = list(storage.accounts.path.parent.glob("*.tmp"))
    assert leftovers == []


def test_no_backup_files_are_produced(storage, seeded, app_dir):  # noqa: ARG001
    storage.accounts.save()
    storage.campaigns.save()
    for pattern in ("*.bak", "*.backup", "*~", "*.old"):
        assert list(app_dir.rglob(pattern)) == []


def test_global_autoreply_is_created_on_demand(storage):
    cfg = storage.global_autoreply()
    assert cfg.owner_id == "global"
    # and is not duplicated on a second call
    assert storage.global_autoreply().owner_id == "global"
    assert len(storage.auto_reply.all()) == 1


def test_session_routing_by_key_prefix(app_dir):  # noqa: ARG001
    from app import config
    from app.telegram.service import session_dir_for

    assert session_dir_for("session_123") == config.CAMPAIGN_SESSIONS_DIR
    assert session_dir_for("pending_abc") == config.CAMPAIGN_SESSIONS_DIR
    assert session_dir_for("op_123") == config.OPERATOR_SESSIONS_DIR
    assert session_dir_for("op_pending_abc") == config.OPERATOR_SESSIONS_DIR


def test_discovery_picks_up_new_session_files(storage, app_dir):
    from app.services.discovery import discover
    from app.models import ApiProfile

    storage.api_profiles.add(ApiProfile(name="Main", api_id=1, api_hash="h"))
    (app_dir / "sessions" / "campaign" / "session_5001.session").write_text("x")
    (app_dir / "sessions" / "operators" / "op_6001.session").write_text("x")

    report = discover(storage)

    assert report == {"accounts": 1, "operators": 1}
    assert storage.accounts.find(lambda a: a.key == "session_5001").telegram_id == 5001
    assert storage.operators.find(lambda o: o.key == "op_6001") is not None

    # running it again must not duplicate anything
    assert discover(storage) == {"accounts": 0, "operators": 0}


def test_discovery_skips_pending_sessions(storage, app_dir):
    from app.services.discovery import discover
    (app_dir / "sessions" / "campaign" / "pending_abc.session").write_text("x")
    assert discover(storage)["accounts"] == 0


def test_missing_session_file_clears_the_claim_but_keeps_the_record(
        storage, app_dir):  # noqa: ARG001
    from app.models import Operator
    from app.services.discovery import prune_missing_sessions

    op = Operator(username="helper", key="op_999", session_file="op_999.session")
    storage.operators.add(op)

    changed = prune_missing_sessions(storage)

    assert changed == 1
    reloaded = storage.operators.get(op.id)
    assert reloaded is not None, "the record must survive — the handle is still useful"
    assert reloaded.key == ""
    assert reloaded.logged_in is False
    assert reloaded.username == "helper"


def test_settings_helpers_clamp_and_order(storage):
    storage.settings.set("autoreply.faq_limit", 99)
    assert storage.settings.faq_limit() == 10
    storage.settings.set("autoreply.faq_limit", -3)
    assert storage.settings.faq_limit() == 0

    storage.settings.update({"autoreply.default_delay_min_sec": 900,
                             "autoreply.default_delay_max_sec": 300})
    assert storage.settings.default_delay_range() == (300, 900)


def test_deleting_an_account_removes_only_its_own_autoreply(
        services, storage, seeded):
    account = seeded["account"]
    services.autoreply.save_config("global", {
        "enabled": True, "rules": [{"kind": "FIRST_MESSAGE", "response": "shared"}]})
    services.autoreply.save_config(account.id, {
        "enabled": True, "rules": [{"kind": "FIRST_MESSAGE", "response": "mine"}]})

    services.accounts.delete(account.id)

    assert storage.auto_reply.get(account.id) is None
    assert storage.auto_reply.get("global") is not None


def test_deleting_an_account_keeps_its_campaigns_visible(services, storage, seeded):
    account = seeded["account"]
    campaign = services.campaigns.create({
        "name": "c", "account_id": account.id,
        "target_ids": [seeded["target"].id], "message_text": "hi"})

    services.accounts.delete(account.id)

    # kept on purpose: a silent disappearance is worse than a visible error
    kept = storage.campaigns.get(campaign.id)
    assert kept is not None
    view = services.state.campaign_view(kept)
    assert "campaign.account_missing" in [i["code"] for i in view["issues"]]


def test_deleting_an_account_does_not_delete_the_session_by_default(
        services, storage, app_dir, seeded):  # noqa: ARG001
    session = app_dir / "sessions" / "campaign" / "session_777.session"
    session.write_text("data")

    services.accounts.delete(seeded["account"].id)

    assert session.exists(), "sessions are never removed unless asked for"
