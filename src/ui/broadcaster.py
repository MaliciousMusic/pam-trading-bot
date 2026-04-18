"""Fan-out broadcaster for the UI.

The engine publishes tick/event dicts; each WebSocket client gets its
own bounded queue. Ticks are buffered so a newly-connected client can
replay the most recent history and hydrate its charts.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class UIBroadcaster:
    def __init__(self, tick_buffer: int = 120) -> None:
        self._subs: set[asyncio.Queue[dict]] = set()
        self._tick_buffer: deque[dict] = deque(maxlen=tick_buffer)

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._subs.discard(q)

    def recent_ticks(self) -> list[dict]:
        return list(self._tick_buffer)

    async def publish(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "tick":
            self._tick_buffer.append(msg)
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
