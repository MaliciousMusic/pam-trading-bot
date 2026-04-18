"""Smoke-test: connect to Binance perps and print normalized Events.

Run from the project root:
    python -m scripts.run_ingress
"""
from __future__ import annotations

import asyncio

from loguru import logger

from src.core.bus import EventBus
from src.core.config import data_sources
from src.ingress.binance_ws import BinancePerpsWS


async def _print_events(bus: EventBus) -> None:
    q = bus.subscribe()
    async for ev in EventBus.drain(q):
        logger.info(
            "[{kind:>11}] {src:<22} sev={sev:.2f} tags={tags} payload={p}",
            kind=ev.kind,
            src=ev.source,
            sev=ev.severity,
            tags=sorted(ev.tags),
            p=ev.payload,
        )


async def main() -> None:
    cfg = data_sources()["sources"]["binance_perps_ws"]
    if not cfg.get("enabled"):
        logger.warning("binance_perps_ws is disabled in config")
        return
    bus = EventBus()
    client = BinancePerpsWS(bus, streams=cfg["streams"])
    await asyncio.gather(client.run(), _print_events(bus))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
