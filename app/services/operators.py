"""Operators: a @username is enough; a session additionally unlocks chats.

A linked operator (made from an account) uses that account's session,
profiles and switch; only its notes are its own.
"""
from __future__ import annotations

from ..logging import LOG
from ..messages import AppError
from ..models import Operator
from .campaigns import NO_OPERATOR_LEFT
from .sessions import checked_links, connection_changed, release_session

MOD = "operators"

# Name the chat listens under, kept apart from the auto-responder's so the
# two can watch the same account without switching each other off.
OWNER = "chat"


class OperatorError(AppError):
    """Why an operator cannot be saved, or its chat cannot be used."""


class OperatorService:
    def __init__(self, storage, service, bus, state, presence, campaigns):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.presence = presence
        # A text that says @operator cannot be sent once the operator it
        # meant has gone, so those campaigns stop with the rest.
        self.campaigns = campaigns
        self.accounts = None      # set by Services: a linked operator's profiles

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="operator", id=id_)

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, username: str = "", display_name: str = "",
               notes: str = "") -> Operator:
        uname = (username or "").strip().lstrip("@")
        if not uname and not display_name.strip():
            raise OperatorError("err.operator.no_name")
        if uname and self.storage.operators.find(
                lambda o: self.storage.operator_uname(o).lower() == uname.lower()):
            raise OperatorError("err.operator.exists", name=f"@{uname}")
        op = Operator(username=uname, display_name=display_name.strip(),
                      notes=notes.strip())
        self.storage.operators.add(op)
        self._changed(op.id)
        return op

    def update(self, op: Operator, **fields) -> Operator:
        """Edit an operator. A linked one keeps only its notes: its API and
        proxy are its account's, set through the account."""
        fields = {**fields, **checked_links(self.storage, fields)}
        account = self.storage.linked_account(op)
        if account is not None:
            links = {k: fields[k] for k in ("api_profile_id", "network_profile_id")
                     if k in fields}
            if links:
                self.accounts.update(account, **links)
            if "notes" in fields:
                op.notes = fields["notes"]
            self.storage.operators.upsert(op)
            self._changed(op.id)
            return op
        if "username" in fields:
            fields["username"] = (fields["username"] or "").strip().lstrip("@")
        reconnects = connection_changed(op, fields)
        for k, v in fields.items():
            if hasattr(op, k) and k != "account_id":
                setattr(op, k, v)
        if not op.uname and not op.display_name.strip():
            raise OperatorError("err.operator.no_name")
        # The user is editing it, so they have read whatever the app had to
        # say about it.
        op.stop_note = None
        self.storage.operators.upsert(op)
        if reconnects:
            self.service.invalidate(op.key)
        self._changed(op.id)
        return op

    def delete(self, operator_id: str) -> bool:
        """Delete the operator and everything naming it.

        An independent operator's session file goes with it; a linked one
        has none - the account and its session stay.
        """
        op = self.storage.operators.get(operator_id)
        if op is None:
            return False

        # No campaign may stay scheduled to send a text naming it. Auto-reply
        # rules stay as they are: the page warns, and none of them is sent.
        for account in self.storage.accounts.filter(lambda a: a.operator_id == operator_id):
            account.operator_id = None
            self.storage.accounts.upsert(account)
            self.campaigns.stop_running(
                [c for c in self.storage.campaigns_for_account(account.id)
                 if c.needs_operator], NO_OPERATOR_LEFT)
            self.bus.publish("entity.changed", entity="account", id=account.id)

        if not op.linked:
            release_session(self.service, op.key)
        ok = self.storage.operators.delete(operator_id)
        self._changed(operator_id)
        return ok

    # ── chats ───────────────────────────────────────────────────────────
    def _require_session(self, op: Operator) -> str:
        """The session key the chat uses, or why there is none."""
        holder = self.storage.session_holder(op)
        handle = self.storage.operator_handle(op)
        if holder is None or not holder.key:
            raise OperatorError("err.operator.not_signed_in", name=handle)
        if getattr(holder, "disabled", False):
            raise OperatorError("err.operator.switched_off", name=handle)
        return holder.key

    async def dialogs(self, op: Operator) -> list[dict]:
        key = self._require_session(op)
        limit = self.storage.settings.number("accounts.dialog_limit")
        rows = await self.service.dialogs(limit, key=key)
        return [{"id": d["id"], "name": d["name"], "username": d["username"]}
                for d in rows]

    async def messages(self, op: Operator, peer_id: int,
                       offset_id: int = 0) -> list[dict]:
        """One page of history, oldest first.

        `offset_id` asks for what came before that message, which is how the
        chat loads older history on scroll-up.
        """
        key = self._require_session(op)
        limit = self.storage.settings.number("accounts.message_limit")
        return await self.service.messages(peer_id, limit, key=key,
                                           offset_id=offset_id)

    # ── presence, driven by the person rather than by the send ──────────
    # An operator is a human at a keyboard: they decide when they are
    # present, and the send is immediate because they are watching for
    # it. Same primitives as a campaign, different order.
    async def enter_chat(self, op: Operator, peer_id: int) -> None:
        """The operator opened this chat."""
        key = self._require_session(op)
        if self.presence is not None:
            await self.presence.enter(key, peer_id)

    async def typing(self, op: Operator, peer_id: int) -> None:
        """The operator is typing right now.

        Called from the chat as they type, so the other side sees it while it
        is happening.
        """
        key = self._require_session(op)
        if self.presence is not None:
            await self.presence.typing(key, peer_id)

    async def leave_chat(self, op: Operator) -> None:
        """The operator closed the chat."""
        key = self._require_session(op)
        if self.presence is not None:
            await self.presence.leave(key)

    async def send(self, op: Operator, peer_id: int, text: str,
                   reply_to: int | None = None) -> dict:
        key = self._require_session(op)
        if not text.strip():
            raise OperatorError("err.chat.empty_message")
        # No manufactured pause: a person pressed send a moment ago and is
        # waiting. The indicator already follows their keyboard (`typing`).
        sent = await self.service.send_message(key, peer_id, text,
                                               reply_to=reply_to)
        LOG.info(f"{self.storage.operator_handle(op)} -> {peer_id}: message sent", module=MOD)
        self.bus.publish("operator.message_sent", operator_id=op.id, peer_id=peer_id)
        return sent

    async def send_file(self, op: Operator, peer_id: int, path: str,
                        caption: str = "", reply_to: int | None = None) -> dict:
        """Send one attachment. The caption travels with it, the way Telegram
        itself pairs a picture with its text."""
        key = self._require_session(op)
        sent = await self.service.send_message(key, peer_id, caption, file=path,
                                               reply_to=reply_to)
        LOG.info(f"{self.storage.operator_handle(op)} -> {peer_id}: file sent", module=MOD)
        self.bus.publish("operator.message_sent", operator_id=op.id, peer_id=peer_id)
        return sent

    # ── live updates ────────────────────────────────────────────────────
    def _on_incoming(self, payload: dict) -> None:
        """Turn one Telegram update into a bus event the open chat can use.

        The event carries the whole message, so the browser appends one bubble
        instead of reloading history and losing the reader's place.
        """
        key = payload.get("account_key")
        operator = self.storage.operators.find(
            lambda o: (self.storage.session_holder(o) or o).key == key)
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
                "reply_to": payload.get("reply_to"),
                "service": False,
            },
        )

    async def watch(self, op: Operator) -> bool:
        """Start listening on this operator's session while its chat is open."""
        key = self._require_session(op)
        return await self.service.attach_incoming(key, self._on_incoming, OWNER)

    async def unwatch(self, op: Operator) -> None:
        holder = self.storage.session_holder(op)
        if holder is not None and holder.key:
            await self.service.detach_incoming(holder.key, OWNER)
