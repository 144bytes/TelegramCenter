"""Telethon exceptions as messages the interface can translate.

The basic errors a person can act on have a code of their own ("tg." and
the class name), worded in the interface's dictionaries. Anything else is
shown as Telegram put it. Every message keeps the raw text in `detail`,
which is what the fault checks below read.
"""
from __future__ import annotations

from ..messages import msg, raw, text_of


# Failures of our own, named so they get wording like Telegram's own errors.
class NoCredentialsError(RuntimeError):
    """The session has no API profile to connect with."""


class NotAuthorizedError(RuntimeError):
    """The session is not signed in."""


class SessionReplaceError(RuntimeError):
    """A finished login could not replace the old session file."""


# Sessions Telegram has invalidated for good: only signing in again helps.
DEAD_SESSION_ERRORS = (
    "AuthKeyDuplicatedError",
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
)

# Telegram has frozen the account. The health check cannot see it - `get_me`
# answers normally - so only trying to do something finds out.
FROZEN_ERRORS = (
    "FrozenMethodInvalidError",
    "FrozenAuthKeyError",
)

# Errors with wording of their own in the dictionaries ("tg.<name>").
KNOWN = frozenset({
    "PasswordHashInvalidError", "PhoneCodeInvalidError",
    "PhoneCodeExpiredError", "PhoneNumberInvalidError",
    "PhoneNumberBannedError", "ApiIdInvalidError",
    *DEAD_SESSION_ERRORS, *FROZEN_ERRORS,
    "TimeoutError", "ChannelPrivateError", "ChatWriteForbiddenError",
    "UserBannedInChannelError", "InviteRequestSentError",
    "UserNotParticipantError", "ChatAdminRequiredError",
    "UsernameNotOccupiedError", "UsernameInvalidError", "PeerIdInvalidError",
    "MsgIdInvalidError", "ChannelInvalidError", "ChatIdInvalidError",
    "InviteHashExpiredError", "InviteHashInvalidError",
    "UserAlreadyParticipantError", "ChannelsTooMuchError", "PeerFloodError",
    "NoCredentialsError", "NotAuthorizedError", "SessionReplaceError",
})

# Errors that carry «try again in N seconds» rather than a reason.
WAIT_ERRORS = ("FloodWaitError", "SlowModeWaitError", "FloodPremiumWaitError")


class Fault:
    """What a failed send, lookup or join means - one name per behaviour.

    `classify` is the only place that reads error names for this; every
    caller acts on the fault it returns.
    """
    DEAD = "dead"              # the session is finished: sign in again
    FROZEN = "frozen"          # Telegram froze the account
    BANNED = "banned"          # banned in this chat, or the account in all
                               # public groups: one more request tells which
    SLOW_MODE = "slow_mode"    # this chat's slow mode: skip it for now
    FLOOD = "flood"            # the whole account must wait
    NOT_FOUND = "not_found"    # the address itself leads nowhere
    GUEST = "guest"            # comments need the discussion group joined
    NOT_MEMBER = "not_member"  # not in the group, and joining is off
    CHAT = "chat"              # this chat refuses: counted, then switched off
    ACCOUNT = "account"        # the account or the network: the safety net


# The address leads nowhere, whichever account asks.
NOT_FOUND_ERRORS = ("UsernameNotOccupiedError", "UsernameInvalidError",
                    "InviteHashExpiredError", "InviteHashInvalidError")

# What Telethon says when a username or a link resolves to nothing. Its
# third ValueError - «Could not find the input entity» - is about the
# account never having met the chat, not about the chat.
NOT_FOUND_TEXTS = ("as username", "Cannot find any entity corresponding to")

# This chat refuses this account. Also the lookups that depend on the
# account: an id it has never met is not a chat that does not exist.
CHAT_ERRORS = (
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "ChannelPrivateError",
    "UserNotParticipantError", "ChatSendMediaForbiddenError",
    "ChatSendPlainForbiddenError", "ChatSendPhotosForbiddenError",
    "ChatSendGifsForbiddenError", "ChatSendStickersForbiddenError",
    "ChatRestrictedError", "PeerIdInvalidError", "ChannelInvalidError",
    "ChatIdInvalidError",
    "MsgIdInvalidError", "InviteRequestSentError",
)


def classify(exc: BaseException) -> str:
    """The fault behind one exception from sending, resolving or joining."""
    name = exc.__class__.__name__
    detail = f"{name}: {exc}"
    if is_dead_session(detail):
        return Fault.DEAD
    if is_frozen(detail):
        return Fault.FROZEN
    if name == "UserBannedInChannelError":
        return Fault.BANNED
    if name == "SlowModeWaitError":
        return Fault.SLOW_MODE
    if name in WAIT_ERRORS:
        return Fault.FLOOD
    if name in NOT_FOUND_ERRORS or (
            name == "ValueError" and any(t in str(exc) for t in NOT_FOUND_TEXTS)):
        return Fault.NOT_FOUND
    if name == "ChatGuestSendForbiddenError":
        return Fault.GUEST
    if name in CHAT_ERRORS or name == "ValueError":
        return Fault.CHAT
    return Fault.ACCOUNT


def wait_seconds(exc: Exception) -> int:
    """How long this error says to wait, or 0 when it does not say."""
    if exc.__class__.__name__ not in WAIT_ERRORS:
        return 0
    try:
        return max(0, int(getattr(exc, "seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0


def tg_error(exc: BaseException) -> dict:
    """The message for one Telethon (or plain) exception."""
    name = exc.__class__.__name__
    text = str(exc)
    if len(text) > 200:
        text = text[:200].rstrip() + "…"
    detail = f"{name}: {text}" if text else name
    if name in KNOWN:
        return msg(f"tg.{name}", detail=detail)
    if name in WAIT_ERRORS:
        return msg(f"tg.{name}", seconds=wait_seconds(exc), detail=detail)
    if name == "ValueError":
        # what Telethon raises for a username or id that resolves to nothing
        return msg("tg.not_found", detail=detail)
    return raw(detail)


def tg_detail(detail: str | None) -> dict:
    """The same, when only the "Name: text" line is left of the exception."""
    detail = detail or ""
    name = detail.split(":", 1)[0].strip()
    return msg(f"tg.{name}", detail=detail) if name in KNOWN else raw(detail)


def is_dead_session(detail) -> bool:
    """Does this failure mean the session itself is finished?

    Takes a message or its text, not the exception: `last_error` is all a
    later read has to go on.
    """
    text = text_of(detail)
    return any(name in text for name in DEAD_SESSION_ERRORS)


def is_frozen(detail) -> bool:
    """Does this failure mean Telegram has frozen the account?"""
    text = text_of(detail)
    return any(name in text for name in FROZEN_ERRORS)
