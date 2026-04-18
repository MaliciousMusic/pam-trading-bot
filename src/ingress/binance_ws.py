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


def _depth_within_bps(levels: list, mid: float, bps: float, side: str) -> float:
    """Sum USD notional of all levels whose price is within `bps` of mid on the
    given side. `levels` is list of [price,qty] strings as Binance sends."""
    if mid <= 0 or not levels:
        return 0.0
    threshold = mid * (bps / 10_000.0)
    total = 0.0
    for lvl in levels:
        try:
            p = float(lvl[0]); q = float(lvl[1])
        except (IndexError, ValueError, TypeError):
            continue
        if side == "bid" and (mid - p) > threshold:
            continue
        if side == "ask" and (p - mid) > threshold:
            continue
        total += p * q
    return total


def _normalize_book_depth(data: dict, stream_name: str = "") -> Event | None:
    """Partial book depth snapshot (e.g. btcusdt@depth20@100ms).

    The combined-stream envelope's inner payload has no `e` field; it carries
    `{"lastUpdateId", "bids":[[px,qty],...], "asks":[[px,qty],...]}`. We flatten
    to a single `book` event with top-of-book + depth-at-5bps on each side,
    which is all the projector kernel and liquidity-sensitive strategies need."""
    bids = data.get("bids") or data.get("b") or []
    asks = data.get("asks") or data.get("a") or []
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0]); best_ask = float(asks[0][0])
    except (IndexError, ValueError, TypeError):
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return None
    mid = 0.5 * (best_bid + best_ask)
    spread_bps = (best_ask - best_bid) / mid * 10_000.0
    bid_depth_5 = _depth_within_bps(bids, mid, 5.0, "bid")
    ask_depth_5 = _depth_within_bps(asks, mid, 5.0, "ask")
    # Extract symbol from "btcusdt@depth20@100ms" → "btcusdt".upper()
    sym = stream_name.split("@", 1)[0].upper() if stream_name else None
    return Event(
        ts=Event.now_utc(),
        source="binance.depth20",
        kind="book",
        symbol=sym,
        payload={
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": spread_bps,
            "bid_depth_5bps_usd": bid_depth_5,
            "ask_depth_5bps_usd": ask_depth_5,
        },
        tags={"orderbook_tick", "book"},
        severity=0.0,
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
        stream_name = msg.get("stream", "")
        # Partial book-depth streams have no `e` field in the inner payload,
        # so we route on the stream name.
        if "@depth" in stream_name and not data.get("e"):
            try:
                ev = _normalize_book_depth(data, stream_name)
            except (KeyError, TypeError, ValueError) as e:
                logger.error("binance_ws: book depth normalize failed: {}", e)
                return
            if ev is None:
                return
            await self.bus.publish(ev)
            return
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
