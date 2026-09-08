"""Where a campaign message goes, and how it gets there.

One place answers "given this target, what do I send to and what do I
reply to": the scheduler sends through it and the channel check asks it
the same question, so they cannot disagree.

A user or a group is written to directly; a channel is commented under
its latest post - Telegram copies every post into the linked discussion
chat, and a reply to that copy is what a comment is.

Which one a target is, is asked of Telegram at the moment of sending,
never read from `target.type`: the stored type was wrong for every
supergroup, which is where 66 PeerIdInvalidError came from.

Every failure leaves here as a DeliveryError carrying its fault
(telegram/errors.py `classify`); what a fault means for a campaign is the
scheduler's business. `campaign.auto_join` and `auto_join_delay_sec` are
read here only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..logging import LOG
from ..models import Target
from ..models.enums import TargetType
from ..messages import msg, text_of
from ..telegram.errors import Fault, classify, tg_error
from ..telegram.service import entity_kind as entity_kind_of
from ..telegram.service import is_member

MOD = "delivery"

# The longest restriction in a chat worth waiting out. A mute or a
# temporary ban up to a week is waited; anything longer - «for ever»
# included, which Telegram writes as no date or a date years away - is a
# ban, and the chat is given up for this account.
LONGEST_WAIT = timedelta(days=7)


class DeliveryError(Exception):
    """Why a message could not go: the message for the user and the fault
    that decides what happens next."""

    def __init__(self, message: dict, fault: str,
                 cause: BaseException | None = None):
        super().__init__(text_of(message))
        self.message = message
        self.fault = fault
        self.cause = cause

    @classmethod
    def of(cls, exc: BaseException) -> "DeliveryError":
        return cls(tg_error(exc), classify(exc), exc)


@dataclass
class Destination:
    """Resolved once, used by both the send and the check."""
    entity: Any                  # what the message is sent to
    reply_to: int | None = None  # the anchor post, for a channel comment
    comment: bool = False        # True when this is a comment under a post
    channel: Any = None          # for a comment: the channel of the post
    # Whether the account is in `entity`, as Telegram reported it - and
    # True from the moment a join succeeds. None: the question does not
    # apply (a person).
    member: bool | None = None
    joined: bool = False         # this send joined it: let the join settle


@dataclass
class Restriction:
    """What a chat forbids this account: a ban, or a wait until a moment
    (muted or thrown out for a week at most)."""
    banned: bool
    until: datetime | None = None   # local time, for a wait


class DeliveryService:
    def __init__(self, storage, service, presence=None):
        self.storage = storage
        self.service = service
        # How the account behaves around the send. Campaigns and auto-replies
        # share it, so they cannot look like different clients.
        self.presence = presence

    @property
    def auto_join(self) -> bool:
        """Whether the account subscribes before writing. One setting, read
        in one place, for every kind of target."""
        return bool(self.storage.settings.get("campaign.auto_join", False))

    @property
    def join_delay(self) -> int:
        """Seconds to wait between joining a chat and writing into it."""
        return self.storage.settings.number("campaign.auto_join_delay_sec")

    # ── the rule ────────────────────────────────────────────────────────
    @staticmethod
    def refs(target: Target) -> list:
        """Every way this target can be addressed, best first.

        The username keeps working when a chat is recreated; the numeric id is
        the fallback for one that has dropped it. Two ways round is what turns
        «no user has that username» into a delivery.
        """
        return [r for r in (target.username, target.telegram_id) if r]

    async def _peer(self, key: str, target: Target, joining: bool):
        """The live entity for this target, its kind, and whether reaching it
        just joined it.

        Asked of Telegram every time. `target.type` is not consulted: acting on
        it sent two thirds of the messages looking for a discussion chat that
        does not exist.
        """
        if target.invite:
            # A chat behind an invitation has no username and no id to look
            # up; it can only be asked for by its hash - and only joining it
            # shows it.
            try:
                chat, joined = await self.service.invite_chat(
                    key, target.invite, join=joining and self.auto_join)
            except Exception as exc:  # noqa: BLE001
                raise DeliveryError.of(exc) from exc
            if chat is None:
                raise DeliveryError(msg("excluded.not_member"), Fault.NOT_MEMBER)
            return chat, entity_kind_of(chat), joined

        candidates = self.refs(target)
        if not candidates:
            raise DeliveryError(msg("delivery.no_address"), Fault.CHAT)
        failures: list[Exception] = []
        for ref in candidates:
            try:
                entity, kind = await self.service.peer_kind(key, ref)
                return entity, kind, False
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
                LOG.debug(f"{key}: {ref} did not resolve - "
                          f"{type(exc).__name__}: {exc}", module=MOD)
        # The username's answer is the one about the chat itself; the id's
        # is often only about what this account has seen.
        verdict = next((e for e in failures if classify(e) == Fault.NOT_FOUND),
                       failures[-1])
        raise DeliveryError.of(verdict) from verdict

    async def resolve(self, key: str, target: Target,
                      joining: bool = True) -> Destination:
        """Where to send and what to answer. Raises DeliveryError.

        `joining` False never joins, not even the chat behind an invitation.
        """
        entity, kind, joined = await self._peer(key, target, joining)

        if kind != TargetType.CHANNEL:
            # a person, a small group or a supergroup: written to directly
            return Destination(entity=entity, joined=joined,
                               member=True if joined else is_member(entity))

        try:
            anchor = await self.service.discussion_anchor(key, entity)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError.of(exc) from exc
        if anchor is None:
            # Telegram gives the same empty answer for «comments are off» and
            # «nothing to comment under», so the channel is asked which it is.
            raise DeliveryError(msg("delivery.no_comments"), Fault.CHAT)
        chat, message_id = anchor
        # The discussion chat is what gets joined and written to, so its own
        # membership is the one that matters, not the channel's.
        return Destination(entity=chat, reply_to=message_id, comment=True,
                           channel=entity, member=is_member(chat))

    # ── using it ────────────────────────────────────────────────────────
    async def enter(self, key: str, destination: Destination,
                    target: Target) -> None:
        """Be in the chat before writing - or say why the message cannot go.

        Joins only where Telegram has just said the account is not in: it used
        to join before every message, 34 join requests in an evening for 22
        channels, which Telegram counts and throttles. A comment may be tried
        without joining the discussion - Telegram says so if it disagrees.
        """
        if destination.member is not False:
            return
        if not self.auto_join:
            if destination.comment:
                return
            raise DeliveryError(msg("excluded.not_member"), Fault.NOT_MEMBER)
        try:
            await self.service.join(key, destination.entity)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "UserAlreadyParticipantError":
                raise DeliveryError.of(exc) from exc
            destination.member = True        # nothing was joined, nothing to wait for
            return
        destination.member = True
        destination.joined = True
        LOG.info(f"{key} joined {target.title or target.username}", module=MOD)

    async def send(self, key: str, destination: Destination, text: str,
                   file: str | None = None, react: bool = False) -> None:
        """Send into a resolved destination. Raises DeliveryError."""
        try:
            if self.presence is not None:
                await self.presence.deliver(key, destination.entity, text,
                                            file=file,
                                            reply_to=destination.reply_to,
                                            react=react)
            else:
                await self.service.send_message(key, destination.entity, text,
                                                file=file,
                                                reply_to=destination.reply_to)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError.of(exc) from exc

    async def restriction(self, key: str,
                          destination: Destination) -> Restriction | None:
        """What the chat itself forbids this account, in one request.

        Asked after Telegram refused with «banned»: that refusal is the same
        for a ban in this chat and for an account barred from every public
        group. None means the chat forbids nothing - it is the account. For a
        comment the discussion chat and the channel are both asked.
        """
        rows = await self.service.own_restrictions(key, self._chats(destination))
        now = datetime.now(timezone.utc)
        verdict = None
        for row in rows:
            if not (row["kicked"] or row["send"]):
                continue
            until = row["until"]
            if until is None or until.timestamp() <= 0 or until - now > LONGEST_WAIT:
                return Restriction(banned=True)
            # muted, or thrown out, for a week at most: waited out
            ends = until.astimezone().replace(tzinfo=None)
            if verdict is None or ends > verdict.until:
                verdict = Restriction(banned=False, until=ends)
        return verdict

    async def leave(self, key: str, destination: Destination,
                    target: Target) -> None:
        """Leave the chat written to - and for a comment the channel as well.

        The one place the app walks out of a chat, used for a ban there and
        nothing else. Not being in one of them already is not an error.
        """
        for chat in self._chats(destination):
            try:
                await self.service.leave(key, chat)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "UserNotParticipantError":
                    LOG.debug(f"{key}: could not leave {target.title or target.username}: "
                              f"{type(exc).__name__}: {exc}", module=MOD)
                continue
            LOG.info(f"{key} left {target.title or target.username}", module=MOD)

    @staticmethod
    def _chats(destination: Destination) -> list:
        chats = [destination.entity]
        if destination.comment and destination.channel is not None \
                and destination.channel != destination.entity:
            chats.append(destination.channel)
        return chats

    async def probe(self, key: str, target: Target) -> DeliveryError | None:
        """Can this target be written to? None when it can, the reason when
        it cannot. Nothing is sent and nothing is joined."""
        try:
            await self.resolve(key, target, joining=False)
        except DeliveryError as exc:
            return exc
        return None
