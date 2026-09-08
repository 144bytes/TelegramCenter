"""Auto-reply: editing rules, and answering private messages after a delay.

A rule saying @operator is never switched off by the app: the page warns
while the account has no operator, and such a reply is checked right
before sending and not sent. The literal «@operator» never goes out.

People who wrote for the first time while nobody was listening - the app
closed, the account or its auto-reply off - are answered when listening
starts: an unread private chat, a person who never wrote before, the
newest message no older than a day.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from ..logging import LOG
from ..messages import AppError, msg
from ..models import AutoReplyConfig, AutoReplyRule, ConversationState
from ..models.enums import AutoReplyKind, EffectiveState, GLOBAL_OWNER
from ..state.manager import OPERATOR_TOKEN, mentions_operator
from ..telegram.errors import Fault, classify, wait_seconds
from ..util import now_iso, parse_iso
from .pacing import spread_connect
from .probing import hold_for_flood

MOD = "autoreply"

# Telegram's own accounts: 777000 sends login notices and codes,
# 42777 and 333000 are used the same way. They are not bots as far as
# the API is concerned, so nothing else filters them out.
SERVICE_SENDERS = frozenset({777000, 42777, 333000})

# Name this service listens under. The chat uses its own.
OWNER = "autoreply"

# How old a first message may be and still get its answer late.
CATCH_UP_SEC = 24 * 3600


class ValidationError(AppError):
    """Why an auto-reply cannot be saved."""


# ── editing ─────────────────────────────────────────────────────────────
class AutoReplyService:
    def __init__(self, storage, bus, state):
        self.storage = storage
        self.bus = bus
        self.state = state

    def validate(self, owner_id: str, cfg: AutoReplyConfig) -> None:
        """Reject anything that must never reach the runtime."""
        if cfg.delay_min_sec < 0 or cfg.delay_max_sec < 0:
            raise ValidationError("err.delay.negative")
        if cfg.delay_min_sec > cfg.delay_max_sec:
            raise ValidationError("err.delay.inverted")

        limit = self.storage.settings.faq_limit()
        faq = [r for r in cfg.rules if r.kind == AutoReplyKind.FAQ]
        if len(faq) > limit:
            raise ValidationError("err.autoreply.faq_limit", count=len(faq),
                                  limit=limit)

        for kind in AutoReplyKind.SINGLETON:
            same = [r for r in cfg.rules if r.kind == kind]
            if len(same) > 1:
                raise ValidationError("err.autoreply.one_only",
                                      kind=msg(f"autoreply.kind.{kind}"))


    def save_config(self, owner_id: str, payload: dict) -> AutoReplyConfig:
        rules = [
            AutoReplyRule.from_dict(r) if isinstance(r, dict) else r
            for r in payload.get("rules", [])
        ]
        # a blank trailing row is how the UI offers "add another FAQ" — drop it
        rules = [r for r in rules if not r.is_blank]

        # An account whose fields are all empty has nothing that sets it
        # apart from the shared default, so it goes back to inheriting.
        if owner_id != GLOBAL_OWNER and not rules:
            self.reset_to_global(owner_id)
            return self.storage.global_autoreply()

        existing = self.storage.auto_reply.get(owner_id)
        lo, hi = self.storage.settings.default_delay_range()
        # the first rule set an account gets arrives switched on: it was added
        # to be used. After that the user's own choice is what carries over.
        default_enabled = existing.enabled if existing is not None else True
        cfg = AutoReplyConfig(
            owner_id=owner_id,
            enabled=bool(payload.get("enabled", default_enabled)),
            delay_min_sec=int(payload.get("delay_min_sec", lo)),
            delay_max_sec=int(payload.get("delay_max_sec", hi)),
            rules=rules)

        self.validate(owner_id, cfg)
        self.storage.auto_reply.upsert(cfg)
        self.bus.publish("entity.changed", entity="auto_reply", id=owner_id)
        return cfg

    def reset_to_global(self, account_id: str) -> None:
        """Drop an account's own config so it inherits the global default again."""
        if self.storage.auto_reply.delete(account_id):
            self.bus.publish("entity.changed", entity="auto_reply", id=account_id)

    def set_enabled(self, owner_id: str, enabled: bool) -> AutoReplyConfig:
        cfg = self.storage.auto_reply.get(owner_id)
        if cfg is None:
            raise ValidationError("err.autoreply.not_set_up")
        was, cfg.enabled = cfg.enabled, bool(enabled)
        try:
            self.validate(owner_id, cfg)
        except ValidationError:
            cfg.enabled = was
            raise
        self.storage.auto_reply.upsert(cfg)
        self.bus.publish("entity.changed", entity="auto_reply", id=owner_id)
        return cfg


# ── runtime ─────────────────────────────────────────────────────────────
class AutoResponder:
    """Listens on every eligible broadcast account and replies after a delay."""

    def __init__(self, storage, service, bus, state, presence=None):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        # The same manners a campaign message goes out with: an instant
        # auto-reply beside a slow campaign message would be two clients.
        self.presence = presence
        self._pending: set[asyncio.Task] = set()
        # (account_id, peer_id, kind) currently waiting out its delay. A flood
        # of "привет" must produce one answer, not one answer per message.
        self._inflight: set[tuple[str, int, str]] = set()
        self._running = False

    # -- lifecycle --------------------------------------------------------
    async def refresh(self) -> None:
        """Attach to accounts that should listen, detach from those that
        should not. Safe to call as often as you like."""
        wanted: set[str] = set()
        for account in self.storage.accounts.all():
            if not account.key or account.disabled:
                continue
            if not self.state.account_usable(account):
                continue
            cfg = self.storage.autoreply_for(account.id)
            if cfg.enabled and any(r.enabled for r in cfg.rules):
                wanted.add(account.key)

        # Only this owner's listeners are ours to add and remove: the
        # operator chat attaches its own on the same accounts.
        attached = self.service.attached_keys(OWNER)
        for key in attached - wanted:
            await self.service.detach_incoming(key, OWNER)
        self._running = True
        # Attaching is what connects the account, so a dozen answering
        # accounts used to connect together. The first goes at once -
        # switching one on must take effect - and the rest are spread out.
        for n, key in enumerate(sorted(wanted - attached)):
            if n:
                await spread_connect(self.storage)
            if await self.service.attach_incoming(key, self._on_message, OWNER):
                await self._catch_up(key)

    async def _catch_up(self, key: str) -> None:
        """Answer people who wrote for the first time while nobody listened.

        Handed to the same path a live message takes, so the rules, the delay
        and the one-answer-per-person guard are the same. Somebody the app
        already knows is not new, whatever the chat says.
        """
        account = self.storage.accounts.find(lambda a: a.key == key)
        if account is None:
            return
        since = datetime.now(timezone.utc) - timedelta(seconds=CATCH_UP_SEC)
        try:
            rows = await self.service.first_contacts(key, since)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"{account.handle}: could not look for unanswered "
                        f"chats: {type(exc).__name__}: {exc}", module=MOD)
            return
        fresh = [r for r in rows
                 if self.storage.conversation(account.id, r["peer_id"]) is None]
        if fresh:
            LOG.info(f"{account.handle}: {len(fresh)} first message(s) came "
                     f"while nobody listened", module=MOD)
        for payload in fresh:
            self._on_message(payload)

    async def stop(self) -> None:
        self._running = False
        for key in list(self.service.attached_keys(OWNER)):
            await self.service.detach_incoming(key, OWNER)
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()
        self._inflight.clear()

    # -- incoming ---------------------------------------------------------
    def _on_message(self, payload: dict) -> None:
        """Called from the Telethon handler. Never blocks: it only spawns the
        delayed delivery task."""
        if not self._running or payload.get("out"):
            return
        task = asyncio.ensure_future(self._handle(payload))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _handle(self, payload: dict) -> None:
        # Bots are not customers. Without this the antispam check answers
        # itself: @SpamBot replies, and our auto-reply replies to that.
        if payload.get("sender_bot"):
            return
        # Neither is Telegram: its «new login from …» notice arrives as an
        # ordinary private message from a sender not flagged as a bot, so
        # signing in scheduled a cheerful sales reply to Telegram itself.
        if payload.get("sender_id") in SERVICE_SENDERS:
            LOG.debug("ignoring a service message from Telegram", module=MOD)
            return
        # The one thing an auto-reply answers: somebody writing to this
        # account directly. Not a group or a channel - `peer_id` there is the
        # chat, not the author, so a busy chat produced one reply and a run
        # of «already pending» lines. History is deliberately kept: Telegram
        # re-delivers what the session missed, and in a private chat that is
        # a person still waiting.
        if not payload.get("private"):
            return
        key = payload["account_key"]
        account = self.storage.accounts.find(lambda a: a.key == key)
        if account is None:
            return
        if not self.state.account_usable(account):
            return

        cfg = self.storage.autoreply_for(account.id)
        if not cfg.enabled:
            return

        conv = self._touch_conversation(account, payload)
        rule = self.pick(cfg, payload.get("text", ""), conv)
        if rule is None:
            return

        guard = (account.id, payload["peer_id"], rule.kind)
        if guard in self._inflight:
            # an answer of this kind is already on its way to this person
            LOG.debug(f"{account.handle}: {rule.kind} reply already pending for "
                      f"{payload.get('sender_name')}, ignoring", module=MOD)
            return

        delay = self._delay_for(cfg)
        self._inflight.add(guard)
        self.bus.publish("autoreply.scheduled", account_id=account.id,
                         rule_id=rule.id, peer=payload.get("sender_name"),
                         delay_sec=delay)
        LOG.info(f"{account.handle}: reply to {payload.get('sender_name')} "
                 f"in {delay}s (rule {rule.kind})", module=MOD)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._inflight.discard(guard)
            return
        try:
            await self._deliver(account, rule, payload, conv)
        finally:
            # released only once the answer is out, so the next message can
            # earn a fresh reply but a burst cannot earn several
            self._inflight.discard(guard)

    def _touch_conversation(self, account, payload) -> ConversationState:
        conv = self.storage.conversation(account.id, payload["peer_id"])
        if conv is None:
            conv = ConversationState(
                account_id=account.id, peer_id=payload["peer_id"],
                peer_name=payload.get("sender_name", ""),
                peer_username=payload.get("sender_username"),
                first_seen_at=now_iso())
        conv.last_message_at = now_iso()
        self.storage.conversations.upsert(conv)
        return conv

    # -- rule selection ---------------------------------------------------
    def pick(self, cfg: AutoReplyConfig, text: str,
             conv: ConversationState) -> AutoReplyRule | None:
        """FAQ beats the generic replies: a specific answer is better than a
        greeting. Among equally-matching FAQ rules one is chosen at random so
        several rules on the same topic all get used."""
        lowered = (text or "").lower()

        faq = [r for r in cfg.rules
               if r.kind == AutoReplyKind.FAQ and r.enabled and r.response.strip()
               and r.match.strip() and r.match.strip().lower() in lowered]
        if faq:
            return random.choice(faq)

        if not conv.first_replied_at:
            first = next((r for r in cfg.rules
                          if r.kind == AutoReplyKind.FIRST_MESSAGE and r.enabled
                          and r.response.strip()), None)
            if first is not None:
                return first

        periodic = next((r for r in cfg.rules
                         if r.kind == AutoReplyKind.PERIODIC and r.enabled
                         and r.response.strip()), None)
        if periodic is not None and self._periodic_due(cfg, conv):
            return periodic
        return None

    def _periodic_due(self, cfg: AutoReplyConfig, conv: ConversationState) -> bool:
        """The periodic reply must not fire on every single message — it waits
        out at least one full delay window between sends."""
        last = parse_iso(conv.last_periodic_at)
        if last is None:
            return True
        return (datetime.now() - last).total_seconds() >= max(cfg.delay_max_sec, 1)

    @staticmethod
    def _delay_for(cfg: AutoReplyConfig) -> int:
        lo, hi = max(0, int(cfg.delay_min_sec)), max(0, int(cfg.delay_max_sec))
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)

    # -- delivery ---------------------------------------------------------
    async def _deliver(self, account, rule: AutoReplyRule, payload: dict,
                       conv: ConversationState) -> None:
        text = rule.response

        # Telegram told the account to wait: the answer waits with it, and
        # goes when the wait is over.
        await self._flood_pause(account)

        # Validation point 5. Everything may have changed during the delay -
        # operator unbound, account disabled, dependency broken - so all of
        # it is re-checked now.
        if self.state.account_effective(account) != EffectiveState.READY:
            LOG.warning(f"{account.handle}: auto-reply cancelled, account not ready",
                        module=MOD)
            return

        if mentions_operator(text):
            operator = self.storage.operator_for(account)
            uname = self.storage.operator_uname(operator) if operator else ""
            if not uname:
                LOG.error(f"{account.handle}: auto-reply BLOCKED - {OPERATOR_TOKEN} "
                          f"with no operator bound; nothing sent", module=MOD)
                self.bus.publish("autoreply.blocked", account_id=account.id,
                                 rule_id=rule.id, reason="operator_required")
                return
            text = _substitute_operator(text, uname)

        if OPERATOR_TOKEN in text.lower():
            # belt and braces: never let the literal token out
            LOG.error(f"{account.handle}: auto-reply BLOCKED - literal "
                      f"{OPERATOR_TOKEN} survived substitution", module=MOD)
            return

        while True:
            try:
                if self.presence is not None:
                    await self.presence.deliver(account.key, payload["peer_id"], text)
                else:
                    await self.service.send_message(account.key, payload["peer_id"],
                                                    text)
                break
            except Exception as exc:  # noqa: BLE001
                if classify(exc) == Fault.FLOOD:
                    until = hold_for_flood(account, self.storage, self.bus,
                                           wait_seconds(exc))
                    LOG.warning(f"{account.handle}: Telegram asked the account to "
                                f"wait until {until} - the auto-reply waits too",
                                module=MOD)
                    await self._flood_pause(account)
                    continue
                LOG.error(f"{account.handle}: auto-reply failed: {exc}", module=MOD)
                self.bus.publish("autoreply.failed", account_id=account.id,
                                 rule_id=rule.id, error=str(exc))
                return

        if rule.kind == AutoReplyKind.PERIODIC:
            conv.last_periodic_at = now_iso()
        if not conv.first_replied_at:
            conv.first_replied_at = now_iso()
        conv.replies_sent += 1
        self.storage.conversations.upsert(conv)
        self.bus.publish("autoreply.sent", account_id=account.id, rule_id=rule.id,
                         peer=conv.peer_name)
        LOG.info(f"{account.handle} -> {conv.peer_name}: auto-reply sent", module=MOD)

    @staticmethod
    async def _flood_pause(account) -> None:
        """Hold the answer while Telegram wants the account to wait."""
        left = account.flood_left()
        if left > 0:
            LOG.info(f"{account.handle}: auto-reply waits until "
                     f"{account.flood_until} as Telegram asked", module=MOD)
            await asyncio.sleep(left)


def _substitute_operator(text: str, username: str) -> str:
    import re
    return re.sub(r"@operator\b", f"@{username}", text, flags=re.IGNORECASE)
