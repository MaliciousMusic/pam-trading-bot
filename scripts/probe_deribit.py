"""Quick Deribit options connectivity probe.

One-shot: instantiates DeribitOptionsPoller, runs a single _poll() and
prints the resulting Event. Proves (or disproves) that the public REST
endpoint returns data and the normalizer produces a well-formed
kind="options_stats" event before we trust the pulse-ledger verdict on
put_call_fader / options_dealer_gamma.
"""
from __future__ import annotations

import asyncio
import sys

import aiohttp
from loguru import logger

from src.core.bus import EventBus
from src.core.config import data_sources
from src.ingress.deribit_options import DeribitOptionsPoller


async def run() -> int:
    cfg = data_sources()["sources"].get("deribit_options") or {}
    if not cfg.get("enabled"):
        print("[probe-deribit] disabled in config")
        return 2

    bus = EventBus()
    q = bus.subscribe()
    poller = DeribitOptionsPoller(
        bus,
        currency=str(cfg.get("currency", "BTC")),
        poll_seconds=int(cfg.get("poll_seconds", 120)),
    )

    async with aiohttp.ClientSession(
        headers={"User-Agent": "pam-council/probe", "Accept": "application/json"},
    ) as s:
        try:
            await poller._poll(s)
        except Exception as e:
            print(f"[probe-deribit] poll raised: {e}")
            return 1

    try:
        ev = q.get_nowait()
    except asyncio.QueueEmpty:
        print("[probe-deribit] poll returned but no event published")
        return 1

    print(f"[probe-deribit] kind={ev.kind}  source={ev.source}")
    print(f"[probe-deribit] payload:")
    for k, v in ev.payload.items():
        if isinstance(v, float):
            print(f"  {k:<32} {v:>12.4f}")
        else:
            print(f"  {k:<32} {v}")
    return 0


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
