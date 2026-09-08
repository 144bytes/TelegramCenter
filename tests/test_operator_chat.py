"""The operator chat: live updates, history paging, attachments.

The bug behind most of this: the chat had no way to learn about a new message
at all, and the one slot for a message listener was owned outright by the
auto-responder, which detached anything it had not attached itself.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from app.models import Operator
from app.models.enums import AccountState
from app.telegram.service import describe_message, media_info


def _operator(storage, seeded, key="op_100"):
    op = Operator(username="support", display_name="Support", key=key,
                  session_file=f"{key}.session", telegram_id=100,
                  api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.operators.add(op)
    return op


class _Msg:
    """Enough of a Telethon message for the describing code."""

    def __init__(self, text="", out=False, sender_id=1, mid=7, action=None,
                 **media):
        self.id = mid
        self.raw_text = text
        self.out = out
        self.sender_id = sender_id
        self.date = dt.datetime(2026, 1, 2, 3, 4, 5)
        self.action = action
        self.file = media.pop("file", None)
        for name in ("photo", "video", "voice", "video_note", "audio", "gif",
                     "sticker", "document"):
            setattr(self, name, media.get(name))


class _File:
    def __init__(self, name="", size=0):
        self.name = name
        self.size = size


# ── the two listeners coexist ───────────────────────────────────────────
def test_the_responder_no_longer_silences_the_chat(services, storage, telegram,
                                                    seeded):
    """The whole point of naming owners: a refresh of the auto-responder used
    to detach every handler it did not recognise, the chat's included."""
    op = _operator(storage, seeded)

    asyncio.run(services.operators.watch(op))
    assert telegram.attached_keys("chat") == {op.key}

    asyncio.run(services.responder.refresh())

    assert telegram.attached_keys("chat") == {op.key}, "the chat still listens"


def test_the_chat_does_not_disturb_the_responder(services, storage, telegram,
                                                  seeded):
    op = _operator(storage, seeded)
    asyncio.run(telegram.attach_incoming(op.key, lambda _p: None, "autoreply"))

    asyncio.run(services.operators.watch(op))
    asyncio.run(services.operators.unwatch(op))

    assert telegram.attached_keys("autoreply") == {op.key}


def test_reopening_the_chat_does_not_stack_listeners(services, storage,
                                                     telegram, seeded):
    """Open, close, open again: one slot, one handler. Stacking them would
    deliver the same message twice into the same chat."""
    op = _operator(storage, seeded)

    for _ in range(3):
        asyncio.run(services.operators.watch(op))
        asyncio.run(services.operators.unwatch(op))
    asyncio.run(services.operators.watch(op))

    assert telegram.attached_keys("chat") == {op.key}
    assert len([k for k in telegram.attached if k[1] == op.key]) == 1


def test_watching_twice_without_closing_replaces_the_handler(services, storage,
                                                              telegram, seeded):
    """A remount must not leave the previous handler behind."""
    op = _operator(storage, seeded)

    asyncio.run(services.operators.watch(op))
    first = telegram.attached[("chat", op.key)]
    asyncio.run(services.operators.watch(op))

    assert len([k for k in telegram.attached if k[1] == op.key]) == 1
    assert telegram.attached[("chat", op.key)] is not None
    assert first is not None


def test_closing_the_chat_leaves_the_responder_listening(services, storage,
                                                          telegram, seeded):
    op = _operator(storage, seeded)
    asyncio.run(telegram.attach_incoming(op.key, lambda _p: None, "autoreply"))
    asyncio.run(services.operators.watch(op))

    asyncio.run(services.operators.unwatch(op))

    assert telegram.attached_keys("chat") == set()
    assert telegram.attached_keys("autoreply") == {op.key}, "only ours was removed"


def test_dropping_the_session_removes_every_listener(services, storage,
                                                      telegram, seeded):
    op = _operator(storage, seeded)
    asyncio.run(telegram.attach_incoming(op.key, lambda _p: None, "autoreply"))
    asyncio.run(services.operators.watch(op))

    asyncio.run(telegram.remove_session(op.key))

    assert telegram.attached_keys("chat") == set()
    assert telegram.attached_keys("autoreply") == set()


def test_a_handle_only_operator_cannot_be_watched(services, storage):
    from app.services.operators import OperatorError

    op = Operator(username="helper")
    storage.operators.add(op)

    try:
        asyncio.run(services.operators.watch(op))
    except OperatorError:
        pass
    else:                                    # pragma: no cover - failure path
        raise AssertionError("watching a sessionless operator must be refused")


# ── incoming reaches the bus ────────────────────────────────────────────
def _published(services, kind: str) -> list[dict]:
    """Collect what the bus is told, without racing a background reader."""
    seen: list[dict] = []
    original = services.bus.publish

    def recording(type_, **payload):
        if type_ == kind:
            seen.append(payload)
        original(type_, **payload)

    services.bus.publish = recording
    return seen


def test_an_incoming_message_is_published_whole(services, storage, telegram,
                                                 seeded):
    """The event carries the message itself. Publishing a bare "something
    changed" would force the chat to reload the history and lose the reader's
    position - the very thing this replaces."""
    op = _operator(storage, seeded)
    asyncio.run(services.operators.watch(op))
    seen = _published(services, "operator.message_in")

    telegram.attached[("chat", op.key)]({
        "account_key": op.key, "peer_id": 55, "message_id": 9,
        "text": "line one\nline two", "date": "2026-01-02T03:04:05",
        "out": False, "media": None,
    })

    assert len(seen) == 1
    assert seen[0]["operator_id"] == op.id
    assert seen[0]["peer_id"] == 55
    assert seen[0]["message"]["text"] == "line one\nline two"


def test_a_message_for_an_unknown_session_is_ignored(services, storage,
                                                      telegram, seeded):
    op = _operator(storage, seeded)
    asyncio.run(services.operators.watch(op))
    seen = _published(services, "operator.message_in")

    telegram.attached[("chat", op.key)]({
        "account_key": "someone_else", "peer_id": 1, "message_id": 1,
        "text": "x", "date": "", "out": False, "media": None,
    })

    assert seen == []


# ── formatting survives the trip ────────────────────────────────────────
def test_line_breaks_and_spacing_are_kept():
    """The old code ran the text through " ".join(text.split()), which turned
    every multi-line message into one line and could not be undone later."""
    text = "Hello,\n\n  indented line\nlast"
    assert describe_message(_Msg(text=text))["text"] == text


def test_an_empty_message_stays_empty():
    assert describe_message(_Msg(text=""))["text"] == ""


def test_a_service_message_is_flagged():
    assert describe_message(_Msg(action=object()))["service"] is True


def test_the_sender_side_comes_from_the_message():
    assert describe_message(_Msg(out=True))["sender"] == "operator"
    assert describe_message(_Msg(out=False))["sender"] == "client"


def test_the_date_leaves_as_a_string():
    assert describe_message(_Msg())["date"] == "2026-01-02T03:04:05"


# ── media is described, never downloaded ────────────────────────────────
def test_media_is_named_by_its_kind():
    assert media_info(_Msg(photo=object()))["kind"] == "photo"
    assert media_info(_Msg(video=object()))["kind"] == "video"


def test_the_most_specific_kind_wins():
    """A sticker is also a document and a voice note is also audio, so a
    loose test would label both of them wrongly."""
    assert media_info(_Msg(sticker=object(), document=object()))["kind"] == "sticker"
    assert media_info(_Msg(voice=object(), audio=object()))["kind"] == "voice"
    assert media_info(_Msg(video_note=object(), video=object()))["kind"] == "video_note"


def test_a_document_keeps_its_filename():
    info = media_info(_Msg(document=object(), file=_File("report.pdf", 2048)))
    assert info == {"kind": "document", "name": "report.pdf", "size": 2048}


def test_plain_text_has_no_media():
    assert media_info(_Msg(text="hi")) is None
    assert describe_message(_Msg(text="hi"))["media"] is None


# ── history paging ──────────────────────────────────────────────────────
def test_history_is_paged_from_the_oldest_message_on_screen(services, storage,
                                                             telegram, seeded):
    op = _operator(storage, seeded)

    first = asyncio.run(services.operators.messages(op, 55))
    older = asyncio.run(services.operators.messages(op, 55, offset_id=2))

    assert [m["id"] for m in first] == [2]
    assert [m["id"] for m in older] == [1], "what came before, not the same page"


def test_the_top_of_the_history_returns_nothing(services, storage, telegram,
                                                 seeded):
    op = _operator(storage, seeded)
    assert asyncio.run(services.operators.messages(op, 55, offset_id=1)) == []


# ── sending ─────────────────────────────────────────────────────────────
def test_sending_returns_the_message_it_sent(services, storage, telegram,
                                              seeded):
    """So the chat appends one bubble instead of reloading the history."""
    op = _operator(storage, seeded)

    sent = asyncio.run(services.operators.send(op, 55, "hi there"))

    assert sent["out"] is True
    assert sent["text"] == "hi there"
    assert sent["id"]


def test_an_empty_message_is_refused(services, storage, seeded):
    from app.services.operators import OperatorError

    op = _operator(storage, seeded)
    try:
        asyncio.run(services.operators.send(op, 55, "   "))
    except OperatorError:
        pass
    else:                                    # pragma: no cover - failure path
        raise AssertionError("an empty message must not be sent")


def test_a_file_is_sent_with_its_caption(services, storage, telegram, seeded):
    op = _operator(storage, seeded)

    sent = asyncio.run(services.operators.send_file(op, 55, r"C:\tmp\pic.jpg",
                                                    "look"))

    assert telegram.files == [(op.key, 55, r"C:\tmp\pic.jpg", "look")]
    assert sent["media"] is not None


def test_a_file_needs_no_caption(services, storage, telegram, seeded):
    """Telegram sends a picture on its own; demanding a caption would be ours
    to invent."""
    op = _operator(storage, seeded)

    asyncio.run(services.operators.send_file(op, 55, r"C:\tmp\pic.jpg"))

    assert telegram.files[0][3] == ""


# ── answering a message ─────────────────────────────────────────────────
def test_a_text_reply_carries_the_message_it_answers(services, storage,
                                                      telegram, seeded):
    op = _operator(storage, seeded)

    sent = asyncio.run(services.operators.send(op, 55, "конечно", reply_to=42))

    assert telegram.replied[-1] == 42
    assert sent["reply_to"] == 42


def test_an_attachment_replies_the_same_way(services, storage, telegram, seeded):
    """One path for every kind: a picture answers a message exactly as text
    does, and so will a voice note when it arrives."""
    op = _operator(storage, seeded)

    asyncio.run(services.operators.send_file(op, 55, r"C:	mp\pic.jpg",
                                             "вот", reply_to=42))

    assert telegram.replied[-1] == 42


def test_sending_without_answering_anything_stays_plain(services, storage,
                                                         telegram, seeded):
    op = _operator(storage, seeded)

    asyncio.run(services.operators.send(op, 55, "просто так"))

    assert telegram.replied[-1] is None


def test_a_message_says_what_it_answers():
    """So the chat can quote it, the way Telegram shows the original above
    the reply."""
    msg = _Msg(text="конечно")
    msg.reply_to_msg_id = 42
    assert describe_message(msg)["reply_to"] == 42


def test_a_plain_message_answers_nothing():
    assert describe_message(_Msg(text="hi"))["reply_to"] is None
