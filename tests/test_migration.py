"""The one-time v1 -> v2 data migration.

The fixtures below reproduce the exact shapes found in the user's real
%APPDATA% folder, including its three defects: an orphan binding, campaigns
pointing at a deleted account, an operator claiming a session that is not on
disk, and an enabled rule containing @operator with no operator bound.
"""
from __future__ import annotations

import json

import pytest

from app.models.enums import AutoReplyKind, GLOBAL_OWNER, ScheduleMode


V1_ACCOUNTS = {"accounts": [{
    "id": "acc_7b6ecc5bea84", "key": "session_5269011324",
    "session_file": "session_5269011324.session", "telegram_id": 5269011324,
    "username": "e4byte", "phone": "+3800953401043", "first_name": "Illia",
    "last_name": "Balieiev", "api_profile_id": "api_c6e0ddba42b2",
    "network_profile_id": None, "operator_id": None,
    "warm_state": "WARM", "operational_state": "READY", "warmup_hours": 0.1,
    "disabled": False, "created_at": "2026-09-08T10:16:13",
    "warm_started_at": "2026-09-08T10:16:13", "warm_deadline": None,
    "last_check_at": None, "last_error": None, "dep_error": None,
}]}

V1_TEMPLATES = {"templates": [{
    "id": "tpl_61653ad78c18", "name": "Main", "text": "Привет, как твои дела?",
}]}

V1_CAMPAIGNS = {"campaigns": [{
    "id": "camp_2afca7298bbd", "name": "Кампания",
    "account_id": "acc_8697f5cf76ac",          # deleted account
    "target_id": "target_0747d7b77824", "template_id": "tpl_61653ad78c18",
    "status": "RUNNING", "system_hold": None, "cyclic": True,
    "interval_min_sec": 15, "interval_max_sec": 40, "sent": 3, "total": 4,
    "current_index": 3, "created_at": "2026-09-07T19:28:19",
}]}

V1_AUTO_REPLY = {"rules": [
    {"id": "rule_d78e5c83e2c4", "name": "", "enabled": True, "trigger": "FAQ",
     "match": "хай", "response": "Привет, напиши мне на @operator",
     "then_transfer_to_operator": True},
    {"id": "rule_f6bff086d562", "enabled": True, "trigger": "FAQ",
     "match": "ыф", "response": "ыф"},
]}

V1_OPERATORS = {"operators": [{
    "id": "op_0e05b3ec6bcf", "username": "e4byte", "display_name": "e4byte",
    "key": "op_5269011324", "session_file": "op_5269011324.session",
    "telegram_id": 5269011324, "capacity": 3, "notes": "",
}]}

V1_TARGETS = {"targets": [{
    "id": "target_0747d7b77824", "title": "@e4byte", "username": "e4byte",
    "type": "CHANNEL", "active": True, "advertising_allowed": True,
    "last_check_status": "AVAILABLE",
}]}

V1_API = {"api_profiles": [{
    "id": "api_c6e0ddba42b2", "name": "Main API", "api_id": 32658546,
    "api_hash": "0790f0b5dd5382a0eddeb780b5250ec8", "enabled": True,
    "last_check": "2026-09-08T10:16:54", "last_check_status": "ONLINE",
}]}

V1_BINDINGS = {"bindings": [{
    "id": "bind_1", "account_id": "acc_8697f5cf76ac",   # orphan
    "operator_id": "op_0e05b3ec6bcf", "active": True,
}]}


@pytest.fixture
def v1_data(app_dir):
    data = app_dir / "data"
    files = {
        "accounts.json": V1_ACCOUNTS, "templates.json": V1_TEMPLATES,
        "campaigns.json": V1_CAMPAIGNS, "auto_reply.json": V1_AUTO_REPLY,
        "operators.json": V1_OPERATORS, "targets.json": V1_TARGETS,
        "api_profiles.json": V1_API, "bindings.json": V1_BINDINGS,
        "health.json": {"health": []},
        "settings.json": {"schema_version": 1, "general": {"language": "ru"},
                          "warmup": {"default_hours": 336}},
    }
    for name, payload in files.items():
        (data / name).write_text(json.dumps(payload, ensure_ascii=False),
                                 encoding="utf-8")
    # the two real campaign sessions
    (app_dir / "sessions" / "campaign" / "session_5269011324.session").write_text("s")
    return app_dir


def _run(v1_data):
    from app.storage import migrate
    ran = migrate.run()
    from app.storage import Storage
    return ran, Storage()


def test_migration_runs_once(v1_data):
    from app.storage import migrate
    assert migrate.run() is True
    assert migrate.current_schema() == migrate.TARGET_SCHEMA
    assert migrate.run() is False, "a second run must be a no-op"


def test_sessions_are_never_touched(v1_data):
    session = v1_data / "sessions" / "campaign" / "session_5269011324.session"
    before = session.read_bytes()
    _run(v1_data)
    assert session.exists()
    assert session.read_bytes() == before


def test_a_snapshot_is_taken_before_migrating(v1_data):
    _run(v1_data)
    backup = v1_data / "data.v1.bak"
    assert backup.is_dir()
    original = json.loads((backup / "campaigns.json").read_text(encoding="utf-8"))
    assert original["campaigns"][0]["target_id"] == "target_0747d7b77824"


def test_account_keeps_identity_and_drops_warmup(v1_data):
    _, storage = _run(v1_data)
    account = storage.accounts.get("acc_7b6ecc5bea84")
    assert account.key == "session_5269011324"
    assert account.username == "e4byte"
    assert account.api_profile_id == "api_c6e0ddba42b2"
    assert account.raw_state == "READY"
    assert not hasattr(account, "warm_state")
    assert not hasattr(account, "warmup_hours")


def test_campaign_gains_a_target_list_and_a_text_snapshot(v1_data):
    _, storage = _run(v1_data)
    campaign = storage.campaigns.get("camp_2afca7298bbd")
    assert campaign.target_ids == ["target_0747d7b77824"]
    assert campaign.message_text == "Привет, как твои дела?"
    assert campaign.source_template_id == "tpl_61653ad78c18"
    assert len(campaign.results) == 1


def test_cyclic_campaign_becomes_an_interval_schedule(v1_data):
    _, storage = _run(v1_data)
    campaign = storage.campaigns.get("camp_2afca7298bbd")
    assert campaign.schedule.mode == ScheduleMode.INTERVAL
    assert campaign.schedule.every_sec == 40
    assert campaign.gap_min_sec == 15 and campaign.gap_max_sec == 40


def test_running_campaign_is_parked_not_resumed_blindly(v1_data):
    _, storage = _run(v1_data)
    assert storage.campaigns.get("camp_2afca7298bbd").raw_state == "PAUSED"


def test_orphan_campaign_is_kept_and_flagged(v1_data):
    _, storage = _run(v1_data)
    from app.state import StateManager
    campaign = storage.campaigns.get("camp_2afca7298bbd")
    assert campaign is not None, "an orphan must not be deleted silently"
    view = StateManager(storage).campaign_view(campaign)
    assert "campaign.account_missing" in [i["code"] for i in view["issues"]]


def test_global_rules_become_the_shared_default(v1_data):
    _, storage = _run(v1_data)
    cfg = storage.auto_reply.get(GLOBAL_OWNER)
    assert cfg is not None
    assert len(cfg.rules) == 2
    assert all(r.kind == AutoReplyKind.FAQ for r in cfg.rules)


def test_operator_rule_is_disabled_when_no_operator_is_bound(v1_data):
    _, storage = _run(v1_data)
    cfg = storage.auto_reply.get(GLOBAL_OWNER)
    bad = next(r for r in cfg.rules if r.id == "rule_d78e5c83e2c4")
    good = next(r for r in cfg.rules if r.id == "rule_f6bff086d562")
    assert "@operator" in bad.response
    assert bad.enabled is False, "an armed @operator rule with no operator is forbidden"
    assert good.enabled is True


def test_operator_claiming_a_missing_session_is_downgraded(v1_data):
    _, storage = _run(v1_data)
    op = storage.operators.get("op_0e05b3ec6bcf")
    assert op is not None, "the handle alone is still useful for auto-replies"
    assert op.key == ""
    assert op.logged_in is False
    assert op.username == "e4byte"


def test_removed_entities_are_renamed_not_deleted(v1_data):
    _run(v1_data)
    data = v1_data / "data"
    assert not (data / "bindings.json").exists()
    assert (data / "bindings.json.removed").exists()
    assert (data / "health.json.removed").exists()
    kept = json.loads((data / "bindings.json.removed").read_text(encoding="utf-8"))
    assert kept["bindings"][0]["id"] == "bind_1"


def test_api_profile_status_splits_into_state_and_error(v1_data):
    _, storage = _run(v1_data)
    profile = storage.api_profiles.get("api_c6e0ddba42b2")
    assert profile.raw_state == "ONLINE"
    assert profile.last_error is None
    assert profile.api_id == 32658546


def test_target_drops_the_removed_flag(v1_data):
    _, storage = _run(v1_data)
    target = storage.targets.get("target_0747d7b77824")
    assert not hasattr(target, "advertising_allowed")
    assert target.username == "e4byte"


def test_dead_settings_sections_are_dropped_language_kept(v1_data):
    _, storage = _run(v1_data)
    assert storage.settings.get("general.language") == "ru"
    assert storage.settings.get("warmup") is None


def test_migration_on_an_empty_folder_is_harmless(app_dir):  # noqa: ARG001
    from app.storage import Storage, migrate
    assert migrate.run() is True
    assert Storage().accounts.all() == []


# ── schema 3: operators gained their own API profile ────────────────────
def test_operators_keep_working_after_gaining_a_profile_field(v1_data):
    """They used to borrow whichever profile was default. Leaving the new
    field empty would make every existing operator report a setting it never
    had to make."""
    _, storage = _run(v1_data)
    operator = storage.operators.get("op_0e05b3ec6bcf")
    assert operator.api_profile_id == "api_c6e0ddba42b2"
    assert operator.network_profile_id is None


def test_a_v2_install_only_runs_the_newer_step(v1_data):
    """Someone already on v2 must not have the whole v1 migration replayed."""
    import json
    from app.storage import migrate

    migrate.run()                       # take it to the current schema
    settings = json.loads((v1_data / "data" / "settings.json").read_text(
        encoding="utf-8"))
    settings["schema_version"] = 2
    (v1_data / "data" / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8")

    operators = json.loads((v1_data / "data" / "operators.json").read_text(
        encoding="utf-8"))
    operators["operators"][0]["api_profile_id"] = None
    (v1_data / "data" / "operators.json").write_text(
        json.dumps(operators, ensure_ascii=False), encoding="utf-8")

    assert migrate.run() is True

    from app.storage import Storage
    assert Storage().operators.get("op_0e05b3ec6bcf").api_profile_id         == "api_c6e0ddba42b2"
