"""A campaign message may carry a picture.

An advert that is always one bare block of text is a recognisable shape in
itself. The file is the app's own copy, so moving the original out of
Downloads cannot break a campaign that has been running for weeks.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

from app import config
from app.models import CampaignMessage
from app.services.campaigns import clean_messages
from app.web import WebServer


def _campaign(services, seeded, messages):
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": messages,
        "interval_min_sec": 0, "interval_max_sec": 0})


# ── the message keeps it ────────────────────────────────────────────────
def test_a_picture_travels_with_its_text(services, storage, telegram, seeded):
    picture = config.MEDIA_DIR / "img_1.png"
    picture.parent.mkdir(parents=True, exist_ok=True)
    picture.write_bytes(b"not really a png")
    campaign = _campaign(services, seeded,
                         [{"text": "Смотрите", "file": str(picture)}])

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.files, "the send was given the file"
    _key, _entity, path, caption = telegram.files[0]
    assert path == str(picture)
    assert caption == "Смотрите"


def test_a_picture_with_no_caption_is_still_a_message():
    """An empty box with nothing at all is the editor's spare row; a picture
    without words is a perfectly ordinary message."""
    kept = clean_messages([{"text": "", "file": "C:/media/img_1.png"},
                           {"text": "", "file": ""},
                           {"text": "  ", "file": ""}])
    assert len(kept) == 1
    assert kept[0].file.endswith("img_1.png")


def test_a_copy_of_a_campaign_carries_the_picture(services, storage, seeded):
    campaign = _campaign(services, seeded,
                         [{"text": "A", "file": "C:/media/img_1.png"}])

    copy = services.campaigns.duplicate(campaign, "копия")

    assert copy.messages[0].file == "C:/media/img_1.png"
    assert copy.messages[0].id != campaign.messages[0].id, "its own rotation"


def test_an_older_campaign_without_the_field_still_loads():
    message = CampaignMessage.from_dict({"id": "msg_1", "text": "старое"})
    assert message.file == ""


# ── uploading ───────────────────────────────────────────────────────────
@pytest.fixture
def server(services, storage, telegram, seeded):  # noqa: ARG001
    web = WebServer(services, storage, telegram)
    web.start()
    yield web
    web.stop()


def _upload(web, name, data=b"pretend this is a picture"):
    req = urllib.request.Request(
        web.origin + "/api/campaigns/upload", data=data, method="POST")
    req.add_header("X-TC-Token", web.guard.token)
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("X-TC-Name", name)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_an_uploaded_picture_is_kept_in_the_app_folder(server):
    status, body = _upload(server, "advert.png")

    assert status == 200
    stored = config.MEDIA_DIR / body["path"].split("\\")[-1].split("/")[-1]
    assert stored.is_file(), "the temporary copy is deleted, so this is ours"
    assert stored.suffix == ".png"
    assert body["name"] == "advert.png"


def test_two_uploads_of_one_name_do_not_collide(server):
    _status, first = _upload(server, "advert.png")
    _status, second = _upload(server, "advert.png")

    assert first["path"] != second["path"]


def test_only_pictures_are_accepted(server):
    status, body = _upload(server, "prices.pdf")

    assert status == 400
    assert body["error"]["code"] == "err.upload.pictures_only"


def test_a_name_that_is_all_dots_cannot_escape_the_folder(server):
    status, body = _upload(server, "../../evil.png")

    assert status == 200
    assert config.MEDIA_DIR in (config.MEDIA_DIR / body["path"]).parents \
        or str(config.MEDIA_DIR) in body["path"]


# ── deleting takes the picture with it ──────────────────────────────────
def _stored(name: str, age_sec: float = 7200.0):
    """A picture already in the media folder, old enough to be swept.

    The grace period exists because a picture is stored the moment it is
    chosen and only becomes referenced when the campaign is saved; a file
    made just now belongs to a form that may still be open.
    """
    import os
    import time
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MEDIA_DIR / name
    path.write_bytes(b"picture")
    old = time.time() - age_sec
    os.utime(path, (old, old))
    return path


def test_deleting_a_campaign_deletes_its_picture(services, storage, seeded):
    picture = _stored("img_gone.png")
    campaign = _campaign(services, seeded, [{"text": "A", "file": str(picture)}])

    services.campaigns.delete(campaign.id)

    assert not picture.exists()


def test_taking_a_picture_off_a_message_deletes_it(services, storage, seeded):
    picture = _stored("img_dropped.png")
    campaign = _campaign(services, seeded, [{"text": "A", "file": str(picture)}])

    services.campaigns.update(campaign, {"messages": [{"text": "A"}]})

    assert not picture.exists()


def test_a_picture_another_campaign_still_uses_is_kept(services, storage,
                                                       seeded):
    """Duplicating a campaign copies the picture reference, not the file."""
    picture = _stored("img_shared.png")
    first = _campaign(services, seeded, [{"text": "A", "file": str(picture)}])
    services.campaigns.duplicate(first, "копия")

    services.campaigns.delete(first.id)

    assert picture.exists()


def test_a_freshly_uploaded_picture_survives_a_sweep(services, storage, seeded):
    """It belongs to a form that is still open and has not been saved yet."""
    picture = _stored("img_fresh.png", age_sec=0)
    campaign = _campaign(services, seeded, [{"text": "A"}])

    services.campaigns.delete(campaign.id)

    assert picture.exists()


def test_emptying_a_running_campaign_deletes_its_picture_too(services, storage,
                                                             seeded):
    """Stopping the campaign must not skip the tidying up."""
    picture = _stored("img_running.png")
    campaign = _campaign(services, seeded, [{"text": "A", "file": str(picture)}])
    services.campaigns.start(campaign)

    services.campaigns.update(campaign, {"messages": []})

    assert not picture.exists()
    assert storage.campaigns.get(campaign.id).raw_state == "PAUSED"
