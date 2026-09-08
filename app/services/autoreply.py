"""Auto-reply: editing rules, and answering incoming messages after a delay.

`@operator` is validated at five points, per the spec:
  1. creating a rule            -> AutoReplyService.save_config
  2. editing a rule             -> AutoReplyService.save_config
  3. enabling a rule            -> AutoReplyService.set_enabled / save_config
  4. changing the account's operator -> AccountService.set_operator
  5. immediately before sending -> AutoResponder._deliver

The last one is the one that actually protects the end user: however a rule
got into a bad state, the literal text "@operator" is never delivered.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime

from ..logging import LOG
from ..models import AutoReplyConfig, AutoReplyRule, ConversationState
from ..models.enums import AutoReplyKind, EffectiveState, GLOBAL_OWNER
from ..state.manager import OPERATOR_TOKEN, mentions_operator
from ..util import now_iso, parse_iso

MOD = "autoreply"

# Name this service listens under. The chat uses its own.
OWNER = "autoreply"


class ValidationError(ValueError):
    """Raised with a message meant to be shown to the user as-is."""


def disarm_operator_rules(storage, bus, account) -> int:
    """Validation point 4: the account lost its operator.

    Any enabled rule whose reply still says @operator is switched off, because
    an armed rule with no operator is exactly the state §11 forbids.
    """
    cfg = storage.auto_reply.get(account.id)
    if cfg is None:
        return 0
    touched = 0
    for rule in cfg.rules:
        if rule.enabled and mentions_operator(rule.response):
            rule.enabled = False
            touched += 1
            LOG.warning(f"{account.handle}: rule {rule.id} disabled - operator "
                        f"unbound while the reply still says @operator", module=MOD)
    if touched:
        storage.auto_reply.upsert(cfg)
        bus.publish("entity.changed", entity="auto_reply", id=cfg.owner_id)
    return touched


# ── editing ─────────────────────────────────────────────────────────────
class AutoReplyService:
    def __init__(self, storage, bus, state):
        self.storage = storage
        self.bus = bus
        self.state = state

    def _operator_for(self, owner_id: str):
        if owner_id == GLOBAL_OWNER:
            return None
        account = self.storage.accounts.get(owner_id)
        return self.storage.operator_for(account) if account else None

    def validate(self, owner_id: str, cfg: AutoReplyConfig) -> None:
        """Reject anything that must never reach the runtime."""
        if cfg.delay_min_sec < 0 or cfg.delay_max_sec < 0:
            raise ValidationError("Задержка не может быть отрицательной")
        if cfg.delay_min_sec > cfg.delay_max_sec:
            raise ValidationError("Минимальная задержка больше максимальной")

        limit = self.storage.settings.faq_limit()
        faq = [r for r in cfg.rules if r.kind == AutoReplyKind.FAQ]
        if len(faq) > limit:
            raise ValidationError(
                f"FAQ-правил {len(faq)}, а лимит {limit}. "
                f"Лимит меняется в общих настройках.")

        for kind in AutoReplyKind.SINGLETON:
            same = [r for r in cfg.rules if r.kind == kind]
            if len(same) > 1:
                label = ("«Первое сообщение»" if kind == AutoReplyKind.FIRST_MESSAGE
                         else "«Периодический ответ»")
                raise ValidationError(f"{label} может быть только одно правило")

        operator = self._operator_for(owner_id)
        for rule in cfg.rules:
            if not rule.enabled:
                continue
            if mentions_operator(rule.response) and operator is None:
                where = ("общего автоответа" if owner_id == GLOBAL_OWNER
                         else "автоответа аккаунта")
                raise ValidationError(
                    f"В тексте {where} есть {OPERATOR_TOKEN}, но оператор не привязан. "
                    f"Привяжите оператора или уберите {OPERATOR_TOKEN}.")
            if (rule.delay_min_sec is not None and rule.delay_max_sec is not None
                    and rule.delay_min_sec > rule.delay_max_sec):
                raise ValidationError("У правила минимальная задержка больше максимальной")

    def save_config(self, owner_id: str, payload: dict) -> AutoReplyConfig:
        rules = [
            AutoReplyRule.from_dict(r) if isinstance(r, dict) else r
            for r in payload.get("rules", [])
        ]
        # a blank trailing row is how the UI offers "add another FAQ" — drop it
        rules = [r for r in rules if not r.is_blank]

        # An account whose fields are all empty has nothing that distinguishes
        # it from the shared default, so it goes back to inheriting. Keeping an
        # empty config would leave it answering nobody while looking configured.
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
            raise ValidationError("Автоответ ещё не настроен")
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

    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
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
            if self.state.account_effective(account) != EffectiveState.READY:
                continue
            cfg = self.storage.autoreply_for(account.id)
            if cfg.enabled and any(r.enabled for r in cfg.rules):
                wanted.add(account.key)

        # Only this owner's listeners are ours to add and remove: the
        # operator chat attaches its own on the same accounts.
        attached = self.service.attached_keys(OWNER)
        for key in attached - wanted:
            await self.service.detach_incoming(key, OWNER)
        for key in wanted - attached:
            await self.service.attach_incoming(key, self._on_message, OWNER)
        self._running = True

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
        key = payload["account_key"]
        account = self.storage.accounts.find(lambda a: a.key == key)
        if account is None:
            return
        if self.state.account_effective(account) != EffectiveState.READY:
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

        delay = self._delay_for(cfg, rule)
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
            self.storage.conversations.add(conv)
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

    def _delay_for(self, cfg: AutoReplyConfig, rule: AutoReplyRule) -> int:
        lo = rule.delay_min_sec if rule.delay_min_sec is not None else cfg.delay_min_sec
        hi = rule.delay_max_sec if rule.delay_max_sec is not None else cfg.delay_max_sec
        lo, hi = max(0, int(lo)), max(0, int(hi))
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)

    # -- delivery ---------------------------------------------------------
    async def _deliver(self, account, rule: AutoReplyRule, payload: dict,
                       conv: ConversationState) -> None:
        text = rule.response

        # Validation point 5. Everything may have changed during the delay:
        # the operator could have been unbound, the account disabled, the
        # dependency broken. Re-check all of it now, not what was true before.
        if self.state.account_effective(account) != EffectiveState.READY:
            LOG.warning(f"{account.handle}: auto-reply cancelled, account not ready",
                        module=MOD)
            return

        if mentions_operator(text):
            operator = self.storage.operator_for(account)
            if operator is None or not operator.uname:
                LOG.error(f"{account.handle}: auto-reply BLOCKED - {OPERATOR_TOKEN} "
                          f"with no operator bound; nothing sent", module=MOD)
                self.bus.publish("autoreply.blocked", account_id=account.id,
                                 rule_id=rule.id, reason="operator_required")
                return
            text = _substitute_operator(text, operator.uname)

        if OPERATOR_TOKEN in text.lower():
            # belt and braces: never let the literal token out
            LOG.error(f"{account.handle}: auto-reply BLOCKED - literal "
                      f"{OPERATOR_TOKEN} survived substitution", module=MOD)
            return

        try:
            await self.service.send_message(account.key, payload["peer_id"], text)
        except Exception as exc:  # noqa: BLE001
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


def _substitute_operator(text: str, username: str) -> str:
    import re
    return re.sub(r"@operator\b", f"@{username}", text, flags=re.IGNORECASE)
