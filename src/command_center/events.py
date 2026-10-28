from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any


class EventBus:
    """In-process pub/sub with a bounded replay buffer for SSE clients."""

    def __init__(self, history: int = 100):
        self._subscribers: set[queue.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._next_id = 0
        self._lock = threading.Lock()

    def subscribe(self, after_id: int | None = None) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            if after_id is not None:
                for event in self._history:
                    if event["id"] > after_id:
                        try:
                            subscriber.put_nowait(event)
                        except queue.Full:
                            break
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            item = {"id": self._next_id, "event": event, "data": data}
            self._history.append(item)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                pass
        return item
