from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any


class EventBroker:
    def __init__(self, site_id: str, history_limit: int = 250):
        self.site_id = site_id
        self.history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, request_id: str, event_type: str, message: str, **details: Any) -> None:
        event = {
            "site_id": self.site_id,
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "event_type": event_type,
            "message": message,
            **details,
        }
        self.history.append(event)
        for queue in tuple(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def stream(self):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self.subscribers.add(queue)
        try:
            for event in list(self.history):
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield event
                except asyncio.TimeoutError:
                    yield {"event_type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
        finally:
            self.subscribers.discard(queue)

