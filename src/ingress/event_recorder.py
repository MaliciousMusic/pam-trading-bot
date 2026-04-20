"""Event tape recorder.

Subscribes to the bus and writes every published Event to a per-day
JSONL tape under `data/events/YYYY-MM-DD.jsonl`. The tape is what the
variant tournament replays against each child council — without it,
variants can't be faithfully scored on the same market session the
parent saw.

Kept deliberately dumb: one coroutine, one file handle per day,
rotate at UTC midnight. No compression (that's a later problem).
Callers can narrow `skip_kinds` if disk is tight (depth@100ms is by
far the fattest feed).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import orjson
from loguru import logger

from ..core.bus import EventBus
from ..core.config import PROJECT_ROOT
from ..core.types import Event


EVENTS_DIR = PROJECT_ROOT / "data" / "events"


class EventRecorder:
    def __init__(
        self,
        bus: EventBus,
        *,
        directory: Path | None = None,
        skip_kinds: set[str] | None = None,
    ) -> None:
        self.bus = bus
        self.dir = directory or EVENTS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.skip_kinds = set(skip_kinds or ())
        self._fh = None
        self._current_day: str | None = None
        self._count = 0

    async def run(self) -> None:
        q = self.bus.subscribe()
        logger.info("[recorder] event tape → {}", self.dir)
        try:
            while True:
                ev: Event = await q.get()
                if ev.kind in self.skip_kinds:
                    continue
                self._write(ev)
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def _write(self, ev: Event) -> None:
        # Day rotation is wall-clock driven (not ev.ts) so replayed events
        # with historical timestamps don't force the tape backward into an
        # old day's file. For live feeds ev.ts ≈ now, so this is a no-op.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._current_day:
            if self._fh is not None:
                self._fh.close()
            self._fh = (self.dir / f"{day}.jsonl").open("ab")
            self._current_day = day
        payload = {
            "ts": ev.ts.astimezone(timezone.utc).isoformat(),
            "source": ev.source,
            "kind": ev.kind,
            "symbol": ev.symbol,
            "payload": ev.payload,
            "tags": sorted(ev.tags),
            "severity": ev.severity,
        }
        self._fh.write(orjson.dumps(payload) + b"\n")
        self._count += 1
        if self._count % 10_000 == 0:
            self._fh.flush()
