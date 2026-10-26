"""Server-sent events: one hub, every open browser tab subscribed.

    ctx.events.publish("job", job.summary())

Each subscriber has its own bounded queue; a tab that stops reading is dropped
rather than allowed to hold memory. Topics (see the build spec, 4.1): hello,
status, shop, job, job-event, notification.

Every published frame carries an `id:` (a number that only grows). A browser keeps
one stream for all its etsyotomasyon tabs (a leader tab relays the events to the others,
see static/js/events.js); while the leader changes, two streams overlap for a moment
and the id lets a tab drop an event it has already seen.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from .router import json_default

_CLOSE = object()


class Subscription:
    def __init__(self, hub: EventHub, maxsize: int = 1000) -> None:
        self.hub = hub
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self.closed = False

    def get(self, timeout: float) -> str | None:
        """The next encoded SSE frame; None on timeout; raises EOFError once closed."""
        if self.closed and self.queue.empty():
            raise EOFError
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _CLOSE:
            self.closed = True
            raise EOFError
        return item

    def put(self, frame: Any) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(frame)
            return True
        except queue.Full:
            self.closed = True
            return False

    def close(self) -> None:
        self.hub.unsubscribe(self)


def encode(topic: str, data: Any, event_id: int | None = None) -> str:
    """One SSE frame. JSON never contains a raw newline, so one data line suffices."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=json_default)
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {topic}\n{id_line}data: {payload}\n\n"


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self.closed = False
        # When the last subscriber left (or the hub was made), for the idle watchdog.
        self.idle_since = time.monotonic()
        self.ever_connected = False
        self._seq = 0

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            if self.closed:
                sub.closed = True
            else:
                self._subs.append(sub)
                self.ever_connected = True
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
                if not self._subs:
                    self.idle_since = time.monotonic()
        sub.closed = True

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, topic: str, data: Any = None) -> None:
        """Send `data` (anything JSON-able) to every open tab under `topic`."""
        payload = {} if data is None else data
        with self._lock:  # numbered and queued in one go: every tab sees the same order
            self._seq += 1
            frame = encode(topic, payload, self._seq)
            dropped = [sub for sub in self._subs if not sub.put(frame)]
        for sub in dropped:
            self.unsubscribe(sub)

    def close(self) -> None:
        """End every stream (server shutdown)."""
        with self._lock:
            self.closed = True
            subs, self._subs = list(self._subs), []
        for sub in subs:
            try:
                sub.queue.put_nowait(_CLOSE)
            except queue.Full:
                sub.closed = True
