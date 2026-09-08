"""Antispam check through Telegram's own bot.

No API answers «is this account spam-limited»: the limit shows up only
when you message a stranger and get PeerFloodError, which is the one
thing we do not want to do to find out. @SpamBot answers directly.

The reply is prose, so classifying means matching phrases. Two rules
keep it honest: an unrecognised reply is UNKNOWN and never CLEAN, and
the whole reply is stored so the tooltip shows Telegram's own wording.

The language is Telegram's to pick - the same account answers in
English on one run and Russian on the next - so both are understood.
The clean Russian sentence contains the word «ограничений», which is
why limited phrases are whole clauses and a clean match only counts
when no limited phrase matches too.
"""
from __future__ import annotations

import asyncio

from ..logging import LOG
from ..models import Operator
from ..models.enums import SpamState
from ..spamphrases import (DEFAULT_BLOCKED, DEFAULT_CLEAN, DEFAULT_FROZEN,
                           DEFAULT_LIMITED)
from ..messages import msg, raw
from ..telegram.errors import is_dead_session, is_frozen, tg_error
from ..util import now_iso
from .probing import clear_frozen, mark_dead, mark_frozen, switch_off

MOD = "spamcheck"

# What the card says while the check has the account switched off.
LIMIT_NOTE = msg("note.spam_limit")

DEFAULT_BOT = "@SpamBot"
MAX_DETAIL = 400

# Telegram writes with typographic punctuation - "You’re", not "You're" -
# so everything is flattened to plain ASCII quotes before matching.
_PUNCTUATION = str.maketrans({
    "’": "'", "‘": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", " ": " ",
})


def _normalise(text: str) -> str:
    return " ".join((text or "").translate(_PUNCTUATION).lower().split())


# The built-in wording. Settings override it; these are what a fresh install
# starts with and what an emptied setting falls back to.
CLEAN_MARKERS = DEFAULT_CLEAN
LIMITED_MARKERS = DEFAULT_LIMITED
BLOCKED_MARKERS = DEFAULT_BLOCKED
FROZEN_MARKERS = DEFAULT_FROZEN


# The bot ends the exchange with one «understood» button. Pressing it
# leaves the chat closed, so the next check starts clean. Nothing
# else is ever clicked: the other buttons open an appeal.
ACK_BUTTONS = ("Cool, thanks", "Хорошо, спасибо")

# What the bot says when the previous exchange was never closed: it
# waits for a button and refuses plain text, including our /start.
STUCK_MARKERS = (
    "use buttons to communicate",
    "please use buttons",
    "используйте кнопки",
    "пользуйтесь кнопками",
)


def is_stuck(reply: str) -> bool:
    text = _normalise(reply)
    return any(m in text for m in STUCK_MARKERS)


def _mentions(text: str, phrases) -> bool:
    return any(_normalise(p) in text for p in phrases)


def mentions_frozen(reply: str) -> bool:
    """Did the bot say the account is frozen?

    The dependable detector is FrozenMethodInvalidError from a method that
    does something; this is the second route, for prose.
    """
    return _mentions(_normalise(reply), FROZEN_MARKERS)


def classify(reply: str, clean_phrases=None, limited_phrases=None,
             blocked_phrases=None) -> str:
    """Turn the bot's answer into a SpamState.

    Worst first: blocked beats limited beats clean, so widening the clean
    side can never turn a restricted account green. Blocked is looked for
    separately - folded together, a temporary limit was painted red while a
    permanent block came out yellow and left the account running.
    """
    text = _normalise(reply)
    if not text:
        return SpamState.UNKNOWN
    if _mentions(text, blocked_phrases or BLOCKED_MARKERS):
        return SpamState.BLOCKED
    clean = _mentions(text, clean_phrases or CLEAN_MARKERS)
    limited = _mentions(text, limited_phrases or LIMITED_MARKERS)
    if clean and not limited:
        return SpamState.CLEAN
    if limited:
        return SpamState.LIMITED
    return SpamState.UNKNOWN


def tidy(reply: str) -> str:
    """One-line version of the reply, short enough for a tooltip."""
    text = " ".join((reply or "").split())
    return text[:MAX_DETAIL - 1] + "…" if len(text) > MAX_DETAIL else text


class SpamCheckService:
    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state

    # ── settings ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.storage.settings.get("spamcheck.enabled", False))

    @property
    def bot(self) -> str:
        return (self.storage.settings.get("spamcheck.bot") or DEFAULT_BOT).strip()

    @property
    def include_operators(self) -> bool:
        return bool(self.storage.settings.get("spamcheck.include_operators", False))

    @property
    def delay_sec(self) -> int:
        return self.storage.settings.number("spamcheck.delay_sec")

    @property
    def timeout_sec(self) -> float:
        return self.storage.settings.number("spamcheck.timeout_sec", low=5)

    def _phrases(self, key: str, fallback) -> tuple:
        raw = self.storage.settings.get(f"spamcheck.{key}")
        values = ([str(item).strip() for item in raw]
                  if isinstance(raw, list) else [])
        values = [v for v in values if v]
        # an emptied list means "use what we shipped", never "match nothing"
        return tuple(values) or tuple(fallback)

    @property
    def clean_phrases(self) -> tuple:
        return self._phrases("clean_phrases", DEFAULT_CLEAN)

    @property
    def limited_phrases(self) -> tuple:
        return self._phrases("limited_phrases", DEFAULT_LIMITED)

    @property
    def blocked_phrases(self) -> tuple:
        return self._phrases("blocked_phrases", DEFAULT_BLOCKED)

    # ── one account or operator ─────────────────────────────────────────
    async def check(self, account) -> str:
        if getattr(account, "disabled", False):
            return account.spam_state        # off means no traffic at all
        if not account.key:
            return self._record(account, SpamState.FAILED,
                                msg("spam.not_authorized"))
        try:
            reply = await self._ask(account)
            if is_stuck(reply):
                # The first call pressed the pending button, so the dialog is
                # closed now. One more try gets the actual status.
                LOG.info(f"{account.handle}: the bot was waiting for a button, "
                         f"asking again", module=MOD)
                reply = await self._ask(account)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            detail = tg_error(exc)
            if is_dead_session(detail):
                # The session is gone - nothing to do with spam. Recording it here is
                # what stops the account reporting READY for ever.
                mark_dead(account, self.storage, self.bus, detail)
                LOG.warning(f"{account.handle}: the session is no longer valid",
                            module=MOD)
                return self._record(account, SpamState.FAILED, detail)
            if is_frozen(detail):
                # Messaging the bot is the cheapest thing that actually does
                # something, so a freeze usually shows here first. A health check
                # never will: `get_me` answers normally.
                mark_frozen(account, self.storage, self.bus, detail)
                LOG.warning(f"{account.handle}: Telegram has frozen the account",
                            module=MOD)
                return self._record(account, SpamState.FAILED, detail)
            LOG.warning(f"{account.handle}: spam check failed - "
                        f"{type(exc).__name__}: {exc}", module=MOD)
            return self._record(account, SpamState.FAILED, detail)

        # The bot's own words are shown as it wrote them.
        said = raw(tidy(reply))
        if mentions_frozen(reply):
            mark_frozen(account, self.storage, self.bus, said)
            LOG.warning(f"{account.handle}: {self.bot} says the account is frozen",
                        module=MOD)
            return self._record(account, SpamState.FAILED, said)
        # The bot answered, so the account can send. Anything we concluded
        # earlier from a failed call is out of date.
        clear_frozen(account, self.storage, self.bus)

        if is_stuck(reply):
            LOG.warning(f"{account.handle}: {self.bot} still wants a button press",
                        module=MOD)
            return self._record(account, SpamState.FAILED,
                                msg("spam.stuck", bot=self.bot))

        verdict = classify(reply, self.clean_phrases, self.limited_phrases,
                           self.blocked_phrases)
        # Only a sending account is switched off by a bad verdict. An
        # operator does not send campaigns; the warning on its dot is enough.
        if verdict in SpamState.BAD and not isinstance(account, Operator):
            # The on/off switch is the only lever: deal with the limit,
            # then switch it back on.
            if switch_off(account, self.storage, self.service, self.bus,
                          LIMIT_NOTE):
                LOG.warning(f"{account.handle}: switched off by the spam check",
                            module=MOD)
        if verdict == SpamState.BLOCKED:
            LOG.error(f"{account.handle}: blocked by Telegram for good - "
                      f"only a different account helps", module=MOD)
        elif verdict == SpamState.LIMITED:
            LOG.warning(f"{account.handle}: limited by Telegram antispam",
                        module=MOD)
        elif verdict == SpamState.UNKNOWN:
            LOG.warning(f"{account.handle}: unrecognised reply from {self.bot}",
                        module=MOD)
        else:
            LOG.info(f"{account.handle}: no antispam limits", module=MOD)
        return self._record(account, verdict, said)

    async def _ask(self, account) -> str:
        return await self.service.ask_bot(
            account.key, self.bot, "/start", self.timeout_sec,
            ack_buttons=ACK_BUTTONS)

    def _record(self, account, verdict: str, detail: dict) -> str:
        account.spam_state = verdict
        account.spam_detail = detail
        account.spam_checked_at = now_iso()
        operator = isinstance(account, Operator)
        repo = self.storage.operators if operator else self.storage.accounts
        repo.upsert(account)
        self.bus.publish("entity.changed",
                         entity="operator" if operator else "account",
                         id=account.id)
        return verdict
