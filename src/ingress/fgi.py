"""Fear-Greed Index poller (alternative.me — free, no key).

Emits a `kind="sentiment"` Event whenever the value changes. The FGI
updates once per day but we poll hourly because the "time until update"
field is not always reliable.

Payload: {value:int 0-100, classification:str, ts_source:str}
Tags include 'fgi' and one of 'fgi_extreme_fear', 'fgi_fear', 'fgi_neutral',
'fgi_greed', 'fgi_extreme_greed' — attention filters use those.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


URL = "https://api.alternative.me/fng/?limit=1"
POLL_SECONDS = 3600      # hourly; FGI changes once/day
RETRY_SECONDS = 60


def _classify(v: int) -> str:
    if v <= 24:  return "fgi_extreme_fear"
    if v <= 44:  return "fgi_fear"
    if v <= 55:  return "fgi_neutral"
    if v <= 74:  return "fgi_greed"
    return "fgi_extreme_greed"


def _severity(v: int) -> float:
    # distance from 50 mapped to 0..1
    return min(1.0, abs(v - 50) / 50.0)


class FGIPoller:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._last_value: int | None = None

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "pam-council/0.1 (research bot)"}
        ) as s:
            while True:
                try:
                    await self._poll_once(s)
                    await asyncio.sleep(POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("fgi poll failed: {}", e)
                    await asyncio.sleep(RETRY_SECONDS)

    async def _poll_once(self, s: aiohttp.ClientSession) -> None:
        async with s.get(URL, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            data = await r.json()
        rows = data.get("data") or []
        if not rows:
            return
        row = rows[0]
        value = int(row["value"])
        if value == self._last_value:
            return
        self._last_value = value
        tag = _classify(value)
        ev = Event(
            ts=Event.now_utc(),
            source="alternative.me/fng",
            kind="sentiment",
            symbol=None,
            payload={
                "value": value,
                "classification": row.get("value_classification", ""),
                "ts_source": row.get("timestamp", ""),
            },
            tags={"fgi", tag},
            severity=_severity(value),
        )
        await self.bus.publish(ev)
        logger.info("[fgi] {} → {} ({})", value, tag, row.get("value_classification"))
