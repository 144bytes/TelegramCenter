"""Operators: a @username is enough; logging in additionally unlocks chats."""
from __future__ import annotations

from .. import config
from ..logging import LOG
from ..models import Operator

MOD = "operators"

# Name the chat listens under, kept apart from the auto-responder's so the
# two can watch the same account without switching each other off.
OWNER = "chat"


class OperatorError(ValueError):
    """Message meant to be shown to the user as-is."""


class OperatorService:
    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="operator", id=id_)

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, username: str = "", display_name: str = "",
               notes: str = "") -> Operator:
        uname = (username or "").strip().lstrip("@")
        if not uname and not display_name.strip():
            raise OperatorError("Укажите ник оператора")
        if uname and self.storage.operators.find(lambda o: o.uname.lower() == uname.lower()):
            raise OperatorError(f"Оператор @{uname} уже есть")
        op = Operator(username=uname, display_name=display_name.strip(),
                      notes=notes.strip())
        self.storage.operators.add(op)
        self._changed(op.id)
        return op

    def update(self, op: Operator, **fields) -> Operator:
        if "username" in fields:
            fields["username"] = (fields["username"] or "").strip().lstrip("@")
        for k, v in fields.items():
            if hasattr(op, k):
                setattr(op, k, v)
        if not op.uname and not op.display_name.strip():
            raise OperatorError("Укажите ник оператора")
        self.storage.operators.upsert(op)
        self._changed(op.id)
        return op

    def delete(self, operator_id: str, delete_session: bool = False) -> bool:
        op = self.storage.operators.get(operator_id)
        if op is None:
            return False

        # Unbinding an operator is a @operator validation point: any account
        # that pointed here must not keep a rule armed that says @operator.
        from .autoreply import disarm_operator_rules
        for account in self.storage.accounts.filter(lambda a: a.operator_id == operator_id):
            account.operator_id = None
            self.storage.accounts.upsert(account)
            disarm_operator_rules(self.storage, self.bus, account)
            self.bus.publish("entity.changed", entity="account", id=account.id)

        if op.key:
            try:
                self.service.run(self.service.remove_session(op.key), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            if delete_session:
                for p in config.OPERATOR_SESSIONS_DIR.glob(f"{op.key}.session*"):
                    try:
                        p.unlink()
                    except OSError as exc:
                        LOG.warning(f"could not remove {p.name}: {exc}", module=MOD)
        ok = self.storage.operators.delete(operator_id)
        self._changed(operator_id)
        return ok

    # ── chats ───────────────────────────────────────────────────────────
    def _require_session(self, op: Operator) -> str:
        if not op.logged_in:
            raise OperatorError(f"{op.handle} не авторизован — чаты недоступны")
        return op.key

    async def dialogs(self, op: Operator) -> list[dict]:
        key = self._require_session(op)
        limit = int(self.storage.settings.get("accounts.dialog_limit", 30))
        rows = await self.service.dialogs(limit, key=key)
        return [{"id": d["id"], "name": d["name"], "username": d["username"]}
                for d in rows]

    async def messages(self, op: Operator, peer_id: int,
                       offset_id: int = 0) -> list[dict]:
        """One page of history, oldest first.

        `offset_id` asks for what came before that message, which is how the
        chat loads older history when the user scrolls to the top.
        """
        key = self._require_session(op)
        limit = int(self.storage.settings.get("accounts.message_limit", 30))
        return await self.service.messages(peer_id, limit, key=key,
                                           offset_id=offset_id)

    async def send(self, op: Operator, peer_id: int, text: str,
                   reply_to: int | None = None) -> dict:
        key = self._require_session(op)
        if not text.strip():
            raise OperatorError("Пустое сообщение")
        sent = await self.service.send_message(key, peer_id, text,
                                               reply_to=reply_to)
        LOG.info(f"{op.handle} -> {peer_id}: message sent", module=MOD)
        self.bus.publish("operator.message_sent", operator_id=op.id, peer_id=peer_id)
        return sent

    async def send_file(self, op: Operator, peer_id: int, path: str,
                        caption: str = "", reply_to: int | None = None) -> dict:
        """Send one attachment. The caption travels with it, the way Telegram
        itself pairs a picture with its text."""
        key = self._require_session(op)
        sent = await self.service.send_message(key, peer_id, caption, file=path,
                                               reply_to=reply_to)
        LOG.info(f"{op.handle} -> {peer_id}: file sent", module=MOD)
        self.bus.publish("operator.message_sent", operator_id=op.id, peer_id=peer_id)
        return sent

    # ── live updates ────────────────────────────────────────────────────
    def _on_incoming(self, payload: dict) -> None:
        """Turn one Telegram update into a bus event the open chat can use.

        The event carries the whole message, so the browser appends a single
        bubble instead of reloading the history - which is what used to make
        the view jump and lose the reader's place.
        """
        operator = self.storage.operators.find(
            lambda o: o.key == payload.get("account_key"))
        if operator is None:
            return
        self.bus.publish(
            "operator.message_in",
            operator_id=operator.id,
            peer_id=payload.get("peer_id"),
            message={
                "id": payload.get("message_id"),
                "out": bool(payload.get("out")),
                "sender": "client",
                "text": payload.get("text") or "",
                "date": payload.get("date") or "",
                "media": payload.get("media"),
                "service": False,
            },
        )

    async def watch(self, op: Operator) -> bool:
        """Start listening on this operator's session while its chat is open."""
        key = self._require_session(op)
        return await self.service.attach_incoming(key, self._on_incoming, OWNER)

    async def unwatch(self, op: Operator) -> None:
        if op.key:
            await self.service.detach_incoming(op.key, OWNER)
