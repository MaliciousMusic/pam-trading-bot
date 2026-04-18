"""End-to-end smoke test of the voting layer.

Connects to Binance perps WS, lets MarketState absorb the stream, and every
settings.clock.tick_seconds asks each tribe for a Vote. Prints a per-tick
table. No aggregator / projector / execution yet — that's the next layer.

Run from the project root:
    python -m scripts.run_voting
"""
from __future__ import annotations

import asyncio

from loguru import logger

from src.core.bus import EventBus
from src.core.config import data_sources, settings
from src.core.types import Event, Side
from src.ingress.binance_ws import BinancePerpsWS
from src.tribes.loader import load_all
from src.tribes.market_state import MarketState


async def _market_state_feeder(bus: EventBus, state: MarketState) -> None:
    q = bus.subscribe()
    while True:
        ev: Event = await q.get()
        state.ingest(ev)


async def _voting_loop(tribes, state: MarketState, tick_seconds: int) -> None:
    tick = 0
    while True:
        await asyncio.sleep(tick_seconds)
        tick += 1
        ts = Event.now_utc()
        if state.last_price is None:
            logger.info("[tick {:>4}] waiting for price data...", tick)
            continue
        votes = [t.vote(ts, state) for t in tribes]
        buys = [v for v in votes if v.side is Side.BUY]
        sells = [v for v in votes if v.side is Side.SELL]
        abst = [v for v in votes if v.side is Side.ABSTAIN]
        logger.info(
            "[tick {:>4}] price={:.2f} funding={:.5f} | BUY={} SELL={} ABSTAIN={}",
            tick, state.last_price, state.last_funding_rate,
            len(buys), len(sells), len(abst),
        )
        for v in votes:
            arrow = {"buy": "^", "sell": "v", "abstain": "-"}[v.side.value]
            logger.info(
                "   {} {:<32} conv={:.2f} w={:.3f} horizon={:>5}s  {}",
                arrow, v.tribe_id, v.conviction, v.weight, v.horizon_seconds, v.reason,
            )


async def main() -> None:
    cfg = settings()
    ds = data_sources()["sources"]["binance_perps_ws"]
    tribes = load_all()
    if not tribes:
        logger.error("no tribes loaded; aborting")
        return
    bus = EventBus()
    state = MarketState()
    client = BinancePerpsWS(bus, streams=ds["streams"])
    tick_s = cfg["clock"]["tick_seconds"]
    await asyncio.gather(
        client.run(),
        _market_state_feeder(bus, state),
        _voting_loop(tribes, state, tick_s),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
