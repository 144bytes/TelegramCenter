"""In-process event bus.

Services publish; the SSE endpoint subscribes. Nothing polls.

Every subscriber gets its own bounded queue. A subscriber that stops draining
(a browser tab that froze, a connection that died without closing) fills up and
then loses its oldest events rather than blocking the publisher — a stuck tab
must never stall the campaign scheduler.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from ..logging import LOG

MAX_QUEUE = 512


@dataclass
class Event:
    type: str                      # "entity.changed" | "state" | "log" | "login" | ...
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class Subscription:
    def __init__(self, bus: "EventBus"):
        self._bus = bus
        self._q: queue.Queue[Event | None] = queue.Queue(maxsize=MAX_QUEUE)
        self._closed = False

    def put(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            try:
                self._q.get_nowait()          # drop the oldest, keep the newest
                self._q.put_nowait(event)
            except queue.Empty:               # pragma: no cover - race, harmless
                pass

    def listen(self, heartbeat: float = 15.0) -> Iterator[Event | None]:
        """Yield events; yield None every `heartbeat` seconds of silence so the
        caller can write an SSE comment and notice a dead socket."""
        while not self._closed:
            try:
                item = self._q.get(timeout=heartbeat)
            except queue.Empty:
                yield None
                continue
            if item is None:
                return
            yield item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._bus.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.RLock()

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, type_: str, **payload: Any) -> None:
        event = Event(type=type_, payload=payload)
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.put(event)

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
        for sub in subs:
            sub._closed = True
            try:
                sub._q.put_nowait(None)
            except queue.Full:
                pass
        LOG.info("event bus closed", module="events")
