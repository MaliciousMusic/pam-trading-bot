"""In-process async event bus.

Minimal pub/sub for Events. One process, many producers, many consumers.
Consumers subscribe with an optional tag filter; producers just publish.

This is deliberately simple — no persistence, no network. When/if we
split ingestion from the brain across processes we can replace this
with Redis streams without touching tribe code.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from .types import Event


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._filters: list[Callable[[Event], bool] | None] = []

    def subscribe(
        self,
        tag_any: set[str] | None = None,
        kinds: set[str] | None = None,
    ) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10_000)

        if tag_any is None and kinds is None:
            flt: Callable[[Event], bool] | None = None
        else:
            def flt(ev: Event) -> bool:
                if kinds is not None and ev.kind not in kinds:
                    return False
                if tag_any is not None and not (ev.tags & tag_any):
                    return False
                return True

        self._subscribers.append(queue)
        self._filters.append(flt)
        return queue

    async def publish(self, ev: Event) -> None:
        for q, flt in zip(self._subscribers, self._filters):
            if flt is not None and not flt(ev):
                continue
            if q.full():
                # Drop oldest to keep the stream live. Slow consumers are
                # a visibility problem, not a reason to stall ingestion.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await q.put(ev)

    @staticmethod
    async def drain(q: asyncio.Queue[Event]) -> AsyncIterator[Event]:
        while True:
            ev = await q.get()
            yield ev
