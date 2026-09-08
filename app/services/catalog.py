"""Targets and templates.

Templates are a library used to fill in a new campaign; they are deliberately
not a live dependency of campaigns that already exist.
"""
from __future__ import annotations

import re

from ..logging import LOG
from ..models import Target, Template
from ..models.enums import EffectiveState, TargetType
from ..util import now_iso

MOD = "catalog"
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/)?(@?[\w+_-]+)",
                      re.IGNORECASE)


def parse_ref(raw: str) -> str:
    """Accept a @name, a t.me link or a bare id and return the bare handle."""
    text = (raw or "").strip()
    m = _LINK_RE.search(text)
    if m:
        text = m.group(1)
    return text.lstrip("@").strip()


class CatalogService:
    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state

    # ── targets ─────────────────────────────────────────────────────────
    def create_target(self, raw: str, title: str = "",
                      type_: str = TargetType.CHANNEL) -> Target:
        ref = parse_ref(raw)
        if not ref:
            raise ValueError("Пустая ссылка на канал")
        if ref.lstrip("-").isdigit():
            target = Target(telegram_id=int(ref), title=title or ref, type=type_)
        else:
            existing = self.storage.targets.find(
                lambda t: t.username.lower() == ref.lower())
            if existing is not None:
                raise ValueError(f"@{ref} уже в списке")
            target = Target(username=ref, title=title or f"@{ref}", type=type_)
        self.storage.targets.add(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

    def add_many(self, blob: str) -> dict:
        """Paste a list — one link per line. Duplicates are skipped, not errors."""
        added, skipped = 0, 0
        for line in (blob or "").splitlines():
            if not line.strip():
                continue
            try:
                self.create_target(line)
                added += 1
            except ValueError:
                skipped += 1
        return {"added": added, "skipped": skipped}

    def update_target(self, target: Target, **fields) -> Target:
        for k, v in fields.items():
            if hasattr(target, k):
                setattr(target, k, v)
        self.storage.targets.upsert(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

    def delete_target(self, target_id: str) -> bool:
        ok = self.storage.targets.delete(target_id)
        if ok:
            # campaigns keep the id and report "канал не найден" rather than
            # quietly shrinking their target list
            self.bus.publish("entity.changed", entity="target", id=target_id)
        return ok

    def _probe_key(self) -> str | None:
        for account in self.storage.accounts.all():
            if (account.key
                    and self.state.account_effective(account) == EffectiveState.READY):
                return account.key
        return None

    async def check_target(self, target: Target) -> Target:
        key = self._probe_key()
        if key is None:
            target.last_check_status = "ERROR: нет готового аккаунта для проверки"
            target.last_check = now_iso()
            self.storage.targets.upsert(target)
            self.bus.publish("entity.changed", entity="target", id=target.id)
            return target

        ref = target.username or str(target.telegram_id or "")
        result = await self.service.check_target(ref, key=key)
        if result.get("ok"):
            target.last_check_status = "AVAILABLE"
            target.title = result.get("title") or target.title
            target.username = result.get("username") or target.username
            target.telegram_id = result.get("id") or target.telegram_id
            target.type = TargetType.CHANNEL if result.get("is_channel") else TargetType.GROUP
        else:
            target.last_check_status = f"ERROR: {result.get('detail', 'недоступен')}"
            LOG.warning(f"target {ref}: {target.last_check_status}", module=MOD)
        target.last_check = now_iso()
        self.storage.targets.upsert(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

    async def check_all(self) -> None:
        for target in self.storage.targets.all():
            await self.check_target(target)

    # ── templates ───────────────────────────────────────────────────────
    def create_template(self, name: str, text: str) -> Template:
        if not name.strip():
            raise ValueError("Не задано название шаблона")
        tpl = Template(name=name.strip(), text=text)
        self.storage.templates.add(tpl)
        self.bus.publish("entity.changed", entity="template", id=tpl.id)
        return tpl

    def update_template(self, tpl: Template, **fields) -> Template:
        for k, v in fields.items():
            if hasattr(tpl, k):
                setattr(tpl, k, v)
        self.storage.templates.upsert(tpl)
        self.bus.publish("entity.changed", entity="template", id=tpl.id)
        return tpl

    def delete_template(self, template_id: str) -> bool:
        ok = self.storage.templates.delete(template_id)
        if ok:
            # existing campaigns already hold their own snapshot of the text,
            # so deleting a template cannot change what they send
            self.bus.publish("entity.changed", entity="template", id=template_id)
        return ok
