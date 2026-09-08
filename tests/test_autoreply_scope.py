"""What an auto-reply is allowed to answer: private messages, and only those.

Written after this: switching the shared auto-reply on scheduled a reply for
dozens of "conversations" at once, across every account, within three seconds.
Every one of them was a group. `peer_id` in a group is the group rather than
the author, so the answer would have been posted into the chat for every
member to read - and the log said "reply to <whoever spoke last>", which is
why it looked like a flood of private messages that were nowhere to be found.

A mention or a reply inside a group arrives exactly the same way and is
treated exactly the same way: not a private message, not answered.

Somebody who wrote for the first time while nobody listened is answered
once listening starts: an unread private chat of theirs alone, the newest
message no older than a day.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


FIRST = {"kind": "FIRST_MESSAGE", "enabled": True, "response": "Здравствуйте!"}
PERIODIC = {"kind": "PERIODIC", "enabled": True, "response": "Я на связи"}
FAQ = {"kind": "FAQ", "enabled": True, "match": "цена", "response": "100"}


def _armed(services, account, rules=(FIRST,)):
    services.autoreply.save_config(account.id, {
        "enabled": True, "delay_min_sec": 0, "delay_max_sec": 0,
        "rules": list(rules)})
    services.responder._running = True


def _message(account, *, private=True, peer=5, when=None, text="привет",
             name="Client"):
    return {"account_key": account.key, "peer_id": peer, "sender_id": peer,
            "sender_name": name, "sender_username": "client",
            "sender_bot": False, "message_id": 1, "text": text,
            "media": None, "reply_to": None, "out": False,
            "private": private,
            "date": (when or now_utc()).isoformat()}


# ── private chats only ──────────────────────────────────────────────────
def test_a_private_message_is_answered(services, telegram, seeded):
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(_message(account)))

    assert len(telegram.sent) == 1


def test_a_group_message_is_ignored(services, telegram, seeded):
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(
        _message(account, private=False, peer=-100123, name="Аня")))

    assert telegram.sent == []


def test_being_mentioned_in_a_group_is_ignored_too(services, telegram, seeded):
    """A mention reaches the account the same way any other group message
    does, and an answer to it lands in the group all the same."""
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(_message(
        account, private=False, peer=-100123, name="Аня",
        text=f"@{account.username} привет, ты тут?")))

    assert telegram.sent == []


def test_a_reply_to_us_inside_a_group_is_ignored_too(services, telegram, seeded):
    account = seeded["account"]
    _armed(services, account)
    payload = _message(account, private=False, peer=-100123)
    payload["reply_to"] = 4242

    asyncio.run(services.responder._handle(payload))

    assert telegram.sent == []


def test_every_kind_of_rule_is_covered(services, telegram, seeded):
    """The check sits before a rule is even chosen, so FIRST_MESSAGE,
    PERIODIC and FAQ are all equally confined to private chats."""
    account = seeded["account"]
    _armed(services, account, rules=(FIRST, PERIODIC, FAQ))

    async def run():
        for text in ("привет", "сколько цена?", "ещё раз"):
            await services.responder._handle(_message(
                account, private=False, peer=-100123, text=text))

    asyncio.run(run())
    assert telegram.sent == []


def test_a_group_leaves_no_conversation_behind(services, storage, telegram,
                                               seeded):
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(
        _message(account, private=False, peer=-100123)))

    assert storage.conversations.all() == [], \
        "a group is not a conversation with this account"


def test_several_people_in_one_group_are_all_ignored(services, telegram, seeded):
    """The log that started this: one scheduled reply and a run of "already
    pending" lines naming whoever spoke next - all of it one group."""
    account = seeded["account"]
    _armed(services, account)

    async def run():
        for name in ("Аня", "оля", "Соня", "Кристина"):
            await services.responder._handle(
                _message(account, private=False, peer=-100123, name=name))

    asyncio.run(run())
    assert telegram.sent == []


def test_a_group_never_blocks_the_private_chat_of_the_same_person(
        services, telegram, seeded):
    account = seeded["account"]
    _armed(services, account)

    async def run():
        await services.responder._handle(
            _message(account, private=False, peer=-100123, name="Аня"))
        await services.responder._handle(
            _message(account, peer=777, name="Аня"))

    asyncio.run(run())

    assert len(telegram.sent) == 1
    assert telegram.sent[0][1] == 777, "answered in private, not in the group"


# ── history in a private chat is answered ───────────────────────────────
def test_a_first_message_from_while_nobody_listened_is_answered(
        services, telegram, seeded):
    account = seeded["account"]
    telegram.waiting[account.key] = [_message(account, peer=41)]
    _armed(services, account)

    async def start_listening():
        await services.responder.refresh()
        await asyncio.gather(*services.responder._pending)

    asyncio.run(start_listening())

    assert [peer for _key, peer, _text in telegram.sent] == [41]


def test_somebody_the_app_already_knows_is_not_new(services, storage, telegram,
                                                   seeded):
    from app.models import ConversationState
    account = seeded["account"]
    storage.conversations.add(ConversationState(account_id=account.id,
                                                peer_id=41))
    telegram.waiting[account.key] = [_message(account, peer=41)]
    _armed(services, account)

    async def start_listening():
        await services.responder.refresh()
        await asyncio.gather(*services.responder._pending)

    asyncio.run(start_listening())

    assert telegram.sent == []


def test_a_backlog_of_private_chats_is_answered_one_per_person(
        services, storage, telegram, seeded):
    account = seeded["account"]
    _armed(services, account)
    old = now_utc() - timedelta(hours=2)

    async def run():
        for peer in range(10, 20):
            # two messages from each person, as a real backlog would carry
            await services.responder._handle(_message(account, peer=peer, when=old))
            await services.responder._handle(_message(account, peer=peer, when=old))

    asyncio.run(run())

    assert len(telegram.sent) == 10, "one answer per person, not per message"
    assert len(storage.conversations.all()) == 10


def test_an_old_group_message_is_still_ignored(services, telegram, seeded):
    """Age is not the rule. Where it was written is."""
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(_message(
        account, private=False, peer=-100123, when=now_utc() - timedelta(days=3))))

    assert telegram.sent == []


def test_someone_answered_once_is_not_answered_again(services, storage,
                                                     telegram, seeded):
    """The backlog may hold several messages from a person who was already
    answered in an earlier run. FIRST_MESSAGE fires once per conversation."""
    account = seeded["account"]
    _armed(services, account)

    asyncio.run(services.responder._handle(_message(account, peer=42)))
    assert len(telegram.sent) == 1

    asyncio.run(services.responder._handle(_message(account, peer=42)))
    assert len(telegram.sent) == 1, "the first message is answered only once"


# ── which chats count as a first message nobody answered ────────────────
class _Msg:
    def __init__(self, mid, when, out=False, text="привет"):
        self.id, self.date, self.out, self.raw_text = mid, when, out, text


class _User:
    def __init__(self, uid, bot=False):
        self.id, self.bot, self.is_self = uid, bot, False
        self.username, self.first_name, self.last_name = f"u{uid}", "Имя", ""
        self.title = None


class _Dialog:
    def __init__(self, uid, history, unread, bot=False, user=True, pinned=False):
        self.id = uid
        self.entity = _User(uid, bot)
        self.is_user = user
        self.unread_count = unread
        self.pinned = pinned
        self.history = history
        self.message = history[0] if history else None


class _Client:
    def __init__(self, dialogs):
        self.dialogs = dialogs

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def get_messages(self, entity, limit):
        d = next(x for x in self.dialogs if x.entity is entity)
        return d.history[:limit]


def _first_contacts(dialogs):
    from app.telegram.service import TelegramService
    service = TelegramService()

    async def client_for(key):  # noqa: ARG001
        return _Client(dialogs)

    service._client_for = client_for
    since = now_utc() - timedelta(hours=24)
    return [row["peer_id"] for row in asyncio.run(
        service.first_contacts("session_1", since))]


def test_only_a_fresh_unread_chat_of_theirs_alone_counts():
    now = now_utc()
    dialogs = [
        _Dialog(1, [_Msg(2, now), _Msg(1, now)], unread=2),        # counts
        _Dialog(2, [_Msg(3, now), _Msg(2, now, out=True)], unread=1),  # we wrote
        _Dialog(3, [_Msg(5, now), _Msg(4, now)], unread=1),        # wrote before
        _Dialog(4, [_Msg(6, now)], unread=0),                      # read
        _Dialog(5, [_Msg(7, now)], unread=1, bot=True),            # a bot
        _Dialog(6, [_Msg(8, now)], unread=1, user=False),          # a group
        _Dialog(7, [_Msg(9, now - timedelta(days=2))], unread=1),  # too old
    ]
    assert _first_contacts(dialogs) == [1]


def test_a_pinned_old_chat_does_not_end_the_search():
    now = now_utc()
    dialogs = [
        _Dialog(1, [_Msg(1, now - timedelta(days=9))], unread=0, pinned=True),
        _Dialog(2, [_Msg(2, now)], unread=1),
    ]
    assert _first_contacts(dialogs) == [2]


def test_a_new_person_is_written_down_once(services, storage, telegram, seeded,
                                           monkeypatch):
    """One incoming message, one write of conversations.json."""
    account = seeded["account"]
    _armed(services, account, rules=(FAQ,))   # «привет» asks nothing
    writes = []
    real = storage.conversations.save
    monkeypatch.setattr(storage.conversations, "save",
                        lambda: (writes.append(1), real())[1])

    asyncio.run(services.responder._handle(_message(account, peer=77)))

    assert len(writes) == 1
