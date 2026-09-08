from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model


@dataclass
class ConversationState(Model):
    """What the auto-responder remembers about one peer.

    Persisted so that restarting the app does not make it greet the same
    person a second time.
    """
    id: str = field(default_factory=lambda: gen_id("conv"))
    account_id: str = ""
    peer_id: int = 0
    peer_name: str = ""
    peer_username: str | None = None
    first_seen_at: str | None = None
    first_replied_at: str | None = None
    last_periodic_at: str | None = None
    last_message_at: str | None = None
    replies_sent: int = 0
