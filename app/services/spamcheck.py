"""Antispam check through Telegram's own bot.

There is no API that answers "is this account spam-limited". The limit only
shows up when you try to message a stranger and get PeerFloodError - which is
exactly the action we do not want to perform just to find out. Telegram's
@SpamBot answers the same question directly: send it /start and it replies with
the account's current standing.

The reply is plain prose, localised to the account's language, so classifying
it means matching phrases. Two rules keep that honest:

  * an unrecognised reply is UNKNOWN, never CLEAN - we must never report an
    account as fine because we failed to understand the answer;
  * the whole reply is stored, so the tooltip shows Telegram's own wording
    (including any end date) instead of our paraphrase of it.

The client asks Telegram for English (lang_code is pinned in
telegram/service.py) but does not get to decide: Telethon must send an empty
lang_pack, which official clients use to make the choice stick, so the reply
language is Telegram's to pick. In practice the same account answers in
English on one run and Russian on the next, so both have to be understood.

Russian needs care rather than avoidance. The clean Russian sentence contains
the word for "limits" - "свободен от каких-либо ограничений" - so a loose
marker there reads a healthy account as a restricted one. That is why the
limited phrases are whole clauses, never single words, and why a clean match
only counts when no limited phrase matches as well.
"""
from __future__ import annotations

import asyncio

from ..logging import LOG
from ..models import Operator
from ..models.enums import SpamState
from ..spamphrases import DEFAULT_CLEAN, DEFAULT_LIMITED
from ..util import now_iso

MOD = "spamcheck"

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


# The bot ends the exchange with a single "understood" button. Pressing it
# leaves the chat closed instead of waiting on us, so the next check starts
# from a clean slate. Nothing else is ever clicked: the limited-account reply
# offers buttons that open an appeal, and those are the user's decision.
ACK_BUTTONS = ("Cool, thanks", "Хорошо, спасибо")

# What the bot says when the previous exchange was never closed: it is waiting
# for a button, so it refuses plain text - including our /start. Seeing this
# means the dialog is stuck, not that the account is fine or limited.
STUCK_MARKERS = (
    "use buttons to communicate",
    "please use buttons",
    "используйте кнопки",
    "пользуйтесь кнопками",
)


def is_stuck(reply: str) -> bool:
    text = _normalise(reply)
    return any(m in text for m in STUCK_MARKERS)


def classify(reply: str, clean_phrases=None, limited_phrases=None) -> str:
    """Turn the bot's answer into a SpamState.

    A limited phrase beats a clean one, so widening the clean side can never
    turn a restricted account green - only the other way round, which is the
    safe direction to be wrong in.
    """
    text = _normalise(reply)
    if not text:
        return SpamState.UNKNOWN
    clean = any(_normalise(p) in text for p in (clean_phrases or CLEAN_MARKERS))
    limited = any(_normalise(p) in text
                  for p in (limited_phrases or LIMITED_MARKERS))
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
        return max(0, int(self.storage.settings.get("spamcheck.delay_sec", 5)))

    @property
    def timeout_sec(self) -> float:
        return max(5, int(self.storage.settings.get("spamcheck.timeout_sec", 25)))

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

    # ── one account or operator ─────────────────────────────────────────
    async def check(self, account) -> str:
        if not account.key:
            return self._record(account, SpamState.FAILED,
                                "Аккаунт не авторизован")
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
            detail = f"{type(exc).__name__}: {exc}"
            LOG.warning(f"{account.handle}: spam check failed - {detail}",
                        module=MOD)
            return self._record(account, SpamState.FAILED, detail)

        if is_stuck(reply):
            LOG.warning(f"{account.handle}: {self.bot} still wants a button press",
                        module=MOD)
            return self._record(
                account, SpamState.FAILED,
                "Бот ждёт нажатия кнопки и не принимает команды. "
                f"Откройте {self.bot} вручную и закройте диалог.")

        verdict = classify(reply, self.clean_phrases, self.limited_phrases)
        # Only a sending account is switched off by a limit. An operator does
        # not send campaigns, so stopping it would take away the replies the
        # limit never touched - the warning on its dot is the whole point.
        if (verdict == SpamState.LIMITED and not isinstance(account, Operator)
                and not account.disabled):
            # Stop it sending. The account's own on/off switch is the lever, so
            # turning it back on is one move once the limit has been dealt with.
            account.disabled = True
            LOG.warning(f"{account.handle}: switched off by the antispam check. "
                        f"Sort the limit out, then turn it back on in the "
                        f"account settings.", module=MOD)
            self.bus.publish("account.auto_disabled", account_id=account.id)
        if verdict == SpamState.LIMITED:
            LOG.warning(f"{account.handle}: limited by Telegram antispam",
                        module=MOD)
        elif verdict == SpamState.UNKNOWN:
            LOG.warning(f"{account.handle}: unrecognised reply from {self.bot}",
                        module=MOD)
        else:
            LOG.info(f"{account.handle}: no antispam limits", module=MOD)
        return self._record(account, verdict, tidy(reply))

    async def _ask(self, account) -> str:
        return await self.service.ask_bot(
            account.key, self.bot, "/start", self.timeout_sec,
            ack_buttons=ACK_BUTTONS)

    def _record(self, account, verdict: str, detail: str) -> str:
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
