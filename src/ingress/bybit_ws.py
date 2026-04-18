"""Bybit public WebSocket ingester (BTCUSDT linear perp).

Subscribes to the `publicTrade.<symbol>` channel on Bybit's v5 linear
perpetual stream. Every taker trade becomes a normalized cross-venue
trade event — same shape as Binance's `trade` events but marked with
`kind="cross_trade"` and `source="bybit.publicTrade"` so MarketState
routes flow into a per-venue series (never merged with Binance's own
taker flow, which is what the execution venue actually felt).

Cross-venue flow powers the arb archetype (Phase G) and the tribes
reading `MarketState.cross_venue_flow("bybit", window_s)`.

Endpoint: wss://stream.bybit.com/v5/public/linear
Subscribe message:
    {"op":"subscribe","args":["publicTrade.BTCUSDT"]}

Message shape (per Bybit v5 docs):
    {"topic":"publicTrade.BTCUSDT","data":[{"T":ms, "s":"BTCUSDT",
     "S":"Buy"|"Sell", "v":qty_str, "p":px_str, "i":id, "BT":false}, ...]}

`S` is the taker side. "Buy" = taker bought = buy-aggression.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import orjson
import websockets
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


URL = "wss://stream.bybit.com/v5/public/linear"


def _ts_from_ms(ms: int | None) -> datetime:
    if ms is None:
        return Event.now_utc()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _severity(notional_usd: float) -> float:
    if notional_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(notional_usd + 1) / 8.0))


class BybitPublicWS:
    def __init__(self, bus: EventBus, symbol: str = "BTCUSDT") -> None:
        self.bus = bus
        self.symbol = symbol
        self._topic = f"publicTrade.{symbol}"

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                logger.info("bybit_ws: connecting to {}", URL)
                async with websockets.connect(
                    URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    await ws.send(orjson.dumps(
                        {"op": "subscribe", "args": [self._topic]}
                    ).decode())
                    backoff = 1.0
                    # Bybit recommends app-level pings every ~20s for long-lived streams.
                    ping_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            await self._handle_raw(raw)
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "bybit_ws: disconnected ({}), reconnect in {:.1f}s", e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(orjson.dumps({"op": "ping"}).decode())
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        if msg.get("topic") != self._topic:
            return
        for item in msg.get("data") or ():
            try:
                price = float(item["p"])
                qty = float(item["v"])
            except (KeyError, ValueError, TypeError):
                continue
            notional = price * qty
            is_sell = str(item.get("S", "")).lower() == "sell"
            tag_side = "taker_sell" if is_sell else "taker_buy"
            ev = Event(
                ts=_ts_from_ms(item.get("T")),
                source="bybit.publicTrade",
                kind="cross_trade",
                symbol=item.get("s") or self.symbol,
                payload={
                    "venue": "bybit",
                    "price": price,
                    "qty": qty,
                    "notional_usd": notional,
                    "is_sell_aggressor": is_sell,
                },
                tags={"cross_trade", "bybit", tag_side},
                severity=_severity(notional),
            )
            await self.bus.publish(ev)
