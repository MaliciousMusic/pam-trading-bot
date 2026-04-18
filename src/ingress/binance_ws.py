"""Binance USDT-M perpetuals WebSocket ingester.

Subscribes to the combined stream endpoint and normalizes each message
into an Event on the bus. Reconnects with exponential backoff.

Streams enabled here (free, public):
  - <sym>@aggTrade      taker-aggressed trade prints
  - <sym>@forceOrder    liquidations
  - <sym>@markPrice@1s  mark price + funding rate, 1s cadence

The normalization rules live here because they are Binance-specific.
Tribes must never see raw exchange payloads — only Events.
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


BASE_URL = "wss://fstream.binance.com/stream"


def _ts_from_ms(ms: int | None) -> datetime:
    if ms is None:
        return Event.now_utc()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _severity_from_notional(notional_usd: float, scale: float = 10.0) -> float:
    """Map trade/liquidation notional to a 0..1 severity."""
    if notional_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(notional_usd + 1) / scale))


def _normalize_agg_trade(data: dict) -> Event:
    price = float(data["p"])
    qty = float(data["q"])
    notional = price * qty
    # m=true -> buyer was the market maker -> taker was the seller (sell aggression)
    is_sell_aggressor = bool(data.get("m"))
    tag_side = "taker_sell" if is_sell_aggressor else "taker_buy"
    return Event(
        ts=_ts_from_ms(data.get("T") or data.get("E")),
        source="binance.aggTrade",
        kind="trade",
        symbol=data.get("s"),
        payload={
            "price": price,
            "qty": qty,
            "notional_usd": notional,
            "is_sell_aggressor": is_sell_aggressor,
            "agg_id": data.get("a"),
        },
        tags={"price", "trade", tag_side},
        severity=_severity_from_notional(notional, scale=8.0),
    )


def _normalize_force_order(data: dict) -> Event:
    o = data.get("o", {})
    price = float(o.get("ap") or o.get("p") or 0.0)
    qty = float(o.get("q") or 0.0)
    notional = price * qty
    # S = side of the liquidated order. SELL liquidated = long got rekt. BUY liq = short got rekt.
    side = (o.get("S") or "").upper()
    liq_tag = "liq_long" if side == "SELL" else "liq_short"
    return Event(
        ts=_ts_from_ms(o.get("T") or data.get("E")),
        source="binance.forceOrder",
        kind="liquidation",
        symbol=o.get("s"),
        payload={
            "price": price,
            "qty": qty,
            "notional_usd": notional,
            "liquidated_side": side,
            "order_type": o.get("o"),
            "status": o.get("X"),
        },
        tags={"liquidation", liq_tag},
        severity=_severity_from_notional(notional, scale=7.0),
    )


def _normalize_mark_price(data: dict) -> Event:
    return Event(
        ts=_ts_from_ms(data.get("E")),
        source="binance.markPrice",
        kind="funding",
        symbol=data.get("s"),
        payload={
            "mark_price": float(data.get("p") or 0.0),
            "index_price": float(data.get("i") or 0.0),
            "funding_rate": float(data.get("r") or 0.0),
            "next_funding_ts_ms": data.get("T"),
        },
        tags={"funding", "mark_price"},
        severity=min(1.0, abs(float(data.get("r") or 0.0)) * 2000.0),
    )


_NORMALIZERS = {
    "aggTrade": _normalize_agg_trade,
    "forceOrder": _normalize_force_order,
    "markPriceUpdate": _normalize_mark_price,
}


class BinancePerpsWS:
    def __init__(self, bus: EventBus, streams: list[str]) -> None:
        self.bus = bus
        self.streams = streams
        self._url = f"{BASE_URL}?streams={'/'.join(streams)}"

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                logger.info("binance_ws: connecting to {}", self._url)
                async with websockets.connect(
                    self._url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("binance_ws: disconnected ({}), reconnect in {:.1f}s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            logger.error("binance_ws: bad json {!r}", raw[:200])
            return
        data = msg.get("data") or msg
        event_type = data.get("e")
        norm = _NORMALIZERS.get(event_type)
        if norm is None:
            return
        try:
            ev = norm(data)
        except (KeyError, TypeError, ValueError) as e:
            logger.error("binance_ws: normalize failed for {}: {}", event_type, e)
            return
        await self.bus.publish(ev)
