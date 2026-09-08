"""What an account does around sending, in the order a person does it.

The transport knows how to appear online, type and mark a chat read; it
does not know in what order, and the order is the whole difference:

  * a campaign or an auto-reply has the text ready and nobody waiting,
    so it can behave - online, look, now and then react to what is
    there, type as long as the text would take, send, go quiet.
    `deliver` is that, for both.
  * an operator is a person at a keyboard: opening the chat is what
    makes them online, their keystrokes show the indicator, closing it
    makes them quiet. `enter` / `typing` / `leave` are that.

Nothing here may break a send: these are courtesies, and a failed one
belongs in the debug log, never in the result. What `deliver` raises is
the send's own failure - with one exception: a reaction refused because
of the account itself (see REACTION_SIGNALS) is an answer the message
would get too, so it is raised before the message is sent.
"""
from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager

from ..logging import LOG
from ..telegram.errors import Fault, classify

MOD = "presence"

# A refused reaction that speaks for the message too: banned, told to
# wait, session dead, account frozen. Anything else - reactions switched
# off in the chat, an emoji it does not take - says nothing about writing.
REACTION_SIGNALS = (Fault.BANNED, Fault.FLOOD, Fault.DEAD, Fault.FROZEN)

# How fast the account types, in characters per second, and the
# bounds on how long that may take. Drawn fresh for every message.
TYPING_CPS = (9.0, 22.0)
TYPING_MIN_SEC = 1.0
TYPING_MAX_SEC = 25.0

# How long to wait before going quiet again, so that "online" and "offline"
# do not land in the same second as the message.
LINGER_SEC = (2.0, 6.0)

# Telegram shows the indicator for about five seconds; a client typing for
# longer sends it again.
TYPING_REFRESH_SEC = 4.5


def typing_seconds(text: str) -> float:
    """How long this text would take somebody to type.

    Bounded at both ends: a one-word reply still takes a moment, and a long
    advert must not leave the indicator running for two minutes.
    """
    length = len((text or "").strip())
    if not length:
        return 0.0
    seconds = length / random.uniform(*TYPING_CPS)
    return max(TYPING_MIN_SEC, min(TYPING_MAX_SEC, seconds))


class Presence:
    """The four courtesies, and the two orders they are used in."""

    def __init__(self, service):
        self.service = service

    # ── the courtesies ──────────────────────────────────────────────────
    async def _quietly(self, what: str, coro) -> bool:
        """Run a presence call, and never let it matter if it fails.

        None of them is the point of the operation, so none may stop it.
        """
        try:
            await coro
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.debug(f"{what} failed: {type(exc).__name__}: {exc}", module=MOD)
            return False

    async def online(self, key: str) -> None:
        await self._quietly("online", self.service.set_online(key, True))

    async def offline(self, key: str) -> None:
        await self._quietly("offline", self.service.set_online(key, False))

    async def read(self, key: str, entity) -> None:
        await self._quietly("mark read", self.service.mark_read(key, entity))

    async def peek(self, key: str, entity) -> int:
        """Open the chat the way a client does, and say what the last message
        is. Zero when nothing could be read."""
        try:
            return await self.service.peek_history(key, entity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.debug(f"peek failed: {type(exc).__name__}: {exc}", module=MOD)
            return 0

    @asynccontextmanager
    async def seen(self, key: str):
        """Be online for the length of this block, and quiet afterwards.

        «Come online» and «go quiet» are one act with two ends, and anything
        that separates them can drop the second half - leaving the account
        online for ever. Exiting a block is what Python guarantees.
        Not used for the operator chat: there the block is a person's session.
        """
        await self.online(key)
        try:
            yield
        finally:
            # Not in the same instant as the message: a client that goes offline
            # the moment it lands was only ever online in order to send.
            linger = random.uniform(*LINGER_SEC)
            if linger > 0:
                await asyncio.sleep(linger)
            await self.offline(key)

    async def type_for(self, key: str, entity, seconds: float,
                       action: str = "typing") -> None:
        """Show the indicator for about this long, refreshed the way a client
        does. The first refusal ends it for this message: the rest is only
        the wait. Nothing is left running when this returns."""
        left = max(0.0, seconds)
        showing = True
        while left > 0:
            if showing:
                showing = await self._quietly(
                    "typing", self.service.set_typing(key, entity, action))
            step = min(left, TYPING_REFRESH_SEC)
            await asyncio.sleep(step)
            left -= step

    # ── the scripted order: campaigns and auto-replies ──────────────────
    async def deliver(self, key: str, entity, text: str, *,
                      file: str | None = None, reply_to: int | None = None,
                      look: bool = True, react: bool = False):
        """Send the way a person would, and return what was sent.

        The send is the only part allowed to fail loudly; the rest is manners.
        `react` leaves a reaction on the newest message first - before ours
        exists, so never on our own.
        """
        async with self.seen(key):
            last = await self.peek(key, entity) if look or react else 0
            if look:
                await self.read(key, entity)
            if react and last:
                await self._react_before(key, entity, last)
            await self.type_for(
                key, entity, typing_seconds(text if not file else text or "…"),
                "upload_photo" if file else "typing")
            return await self.service.send_message(
                key, entity, text, file=file, reply_to=reply_to)

    # ── reacting ────────────────────────────────────────────────────────
    async def _react_before(self, key: str, entity, message_id: int) -> None:
        """React to this message on the way to writing.

        The first or second emoji the chat offers, at random. Paid reactions
        are excluded in the transport, where the list is read: they cost real
        Stars and are usually listed first. Whether to react at all belongs to
        the campaign. A refusal is raised only when it is one of
        REACTION_SIGNALS; any other is logged and the message goes as usual.
        """
        try:
            available = await self.service.reactions_available(key, entity)
            if not available:
                return                       # this chat allows none at all
            emoji = random.choice(available[:2])
            await self.service.react(key, entity, message_id, emoji)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if classify(exc) in REACTION_SIGNALS:
                raise
            LOG.debug(f"reaction failed: {type(exc).__name__}: {exc}", module=MOD)
            return
        LOG.debug(f"{key} reacted {emoji} before writing", module=MOD)

    # ── following a person: the operator chat ───────────────────────────
    async def enter(self, key: str, entity) -> None:
        """The operator opened this chat: online, and everything in it read."""
        await self.online(key)
        await self.read(key, entity)

    async def typing(self, key: str, entity) -> None:
        """The operator is typing. One signal; the chat sends it again while
        the keys keep moving, so the indicator follows the keyboard."""
        await self._quietly("typing", self.service.set_typing(key, entity))

    async def leave(self, key: str) -> None:
        """The operator closed the chat."""
        await self.offline(key)
