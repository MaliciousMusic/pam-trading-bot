"""Bybit USDT-perp linear public WebSocket — PRIMARY ingest path.

Drop-in substitute for :class:`~src.ingress.binance_ws.BinancePerpsWS`
when Binance futures WS is unreachable (see PLAN.md §5.1.2,
2026-04-24 venue swap). Subscribes to Bybit V5 public linear streams
for BTCUSDT and emits Events with the same ``kind`` taxonomy the
engine pipeline already speaks:

  - ``publicTrade.BTCUSDT``        → ``kind="trade"``
  - ``tickers.BTCUSDT``            → ``kind="funding"`` (mark/index/funding)
  - ``orderbook.50.BTCUSDT``       → ``kind="book"`` (top-of-book + 5-bp depth)
  - ``allLiquidation.BTCUSDT``     → ``kind="liquidation"``

Event shape matches Binance normalizers line-for-line where the field
exists; payload keys are kept identical so MarketState, tribes, and
the projector see no venue-specific schema.

Liquidation side convention:
  - Binance ``forceOrder.S = "SELL"`` → long got rekt → tag ``liq_long``.
  - Bybit   ``allLiquidation.S = "Buy"`` → position was long → tag ``liq_long``.
  The two venues use opposite field semantics; the normalizer below
  translates so downstream tags stay canonical.

Topic notes:
  - Bybit V5 deprecated the legacy ``liquidation.<sym>`` topic in
    Aug-2024; the current topic is ``allLiquidation.<sym>`` which
    aggregates all liquidations in the preceding 500ms window into a
    single list frame. Attempting to subscribe to the legacy topic
    makes the V5 endpoint reject the WHOLE subscription batch with
    ``error:handler not found`` — confirmed 2026-04-24 during venue
    swap. Payload field names differ: ``T`` (ts), ``s`` (symbol),
    ``S`` (side), ``v`` (qty), ``p`` (price).
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import websockets
from loguru import logger

from ..core import serde
from ..core.bus import EventBus
from ..core.types import Event


URL = "wss://stream.bybit.com/v5/public/linear"


def _ts_from_ms(ms: int | None) -> datetime:
    if ms is None:
        return Event.now_utc()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _severity_from_notional(notional_usd: float, scale: float = 10.0) -> float:
    if notional_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(notional_usd + 1) / scale))


def _to_float(x, default: float = 0.0) -> float:
    """Bybit sends most numeric fields as strings; empty string → default."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _normalize_public_trade(item: dict, symbol: str) -> Event | None:
    price = _to_float(item.get("p"))
    qty = _to_float(item.get("v"))
    if price <= 0 or qty <= 0:
        return None
    notional = price * qty
    is_sell_aggressor = str(item.get("S", "")).lower() == "sell"
    tag_side = "taker_sell" if is_sell_aggressor else "taker_buy"
    return Event(
        ts=_ts_from_ms(item.get("T")),
        source="bybit.publicTrade",
        kind="trade",
        symbol=item.get("s") or symbol,
        payload={
            "price": price,
            "qty": qty,
            "notional_usd": notional,
            "is_sell_aggressor": is_sell_aggressor,
            "agg_id": item.get("i"),
        },
        tags={"price", "trade", tag_side},
        severity=_severity_from_notional(notional, scale=8.0),
    )


def _normalize_ticker(data: dict, symbol: str) -> Event | None:
    """Bybit ``tickers.<sym>`` funding/mark payload → ``kind="funding"``.

    Snapshot frames carry the full field set; delta frames carry only
    changed fields. We only emit an Event when mark/index/funding or
    their timestamps show up — pure top-of-book delta ticks don't need
    to fire a funding event (MarketState already tracks last_price from
    publicTrade, which is more granular).
    """
    mark = data.get("markPrice")
    index = data.get("indexPrice")
    rate = data.get("fundingRate")
    nxt = data.get("nextFundingTime")
    if mark is None and index is None and rate is None:
        return None
    ts_ms = data.get("updatedTime") or data.get("deliveryTime")
    try:
        ts_ms = int(ts_ms) if ts_ms is not None else None
    except (TypeError, ValueError):
        ts_ms = None
    return Event(
        ts=_ts_from_ms(ts_ms),
        source="bybit.tickers",
        kind="funding",
        symbol=data.get("symbol") or symbol,
        payload={
            "mark_price": _to_float(mark),
            "index_price": _to_float(index),
            "funding_rate": _to_float(rate),
            "next_funding_ts_ms": int(nxt) if nxt not in (None, "") else None,
        },
        tags={"funding", "mark_price"},
        severity=min(1.0, abs(_to_float(rate)) * 2000.0),
    )


def _depth_within_bps(
    levels: list[tuple[float, float]], mid: float, bps: float, side: str,
) -> float:
    """Sum of notional (USD) on levels within ``bps`` of mid on the given side."""
    if mid <= 0 or not levels:
        return 0.0
    threshold = mid * (bps / 10_000.0)
    total = 0.0
    for p, q in levels:
        if side == "bid" and (mid - p) > threshold:
            continue
        if side == "ask" and (p - mid) > threshold:
            continue
        total += p * q
    return total


def _normalize_book_snapshot(data: dict, symbol: str) -> Event | None:
    """Bybit ``orderbook.<depth>.<sym>`` snapshot → ``kind="book"`` event.

    Snapshot frames carry the full book (depth 50 for this subscription).
    Delta frames are skipped in v1 — they'd require maintaining a local
    order book and reconciling with the snapshot/seq counter. For the
    decomposition measurement we only need top-of-book + 5-bp depth on
    each side, and the snapshot-only approach samples that cleanly at
    the snapshot cadence (several per second on BTCUSDT).
    """
    bids_raw = data.get("b") or []
    asks_raw = data.get("a") or []
    if not bids_raw or not asks_raw:
        return None
    try:
        bids = [(float(px), float(qty)) for px, qty in bids_raw if float(qty) > 0]
        asks = [(float(px), float(qty)) for px, qty in asks_raw if float(qty) > 0]
    except (TypeError, ValueError):
        return None
    if not bids or not asks:
        return None
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    best_bid, _ = bids[0]
    best_ask, _ = asks[0]
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return None
    mid = 0.5 * (best_bid + best_ask)
    spread_bps = (best_ask - best_bid) / mid * 10_000.0
    bid_depth_5 = _depth_within_bps(bids, mid, 5.0, "bid")
    ask_depth_5 = _depth_within_bps(asks, mid, 5.0, "ask")
    return Event(
        ts=Event.now_utc(),
        source="bybit.orderbook50",
        kind="book",
        symbol=data.get("s") or symbol,
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


def _normalize_liquidation(data: dict, symbol: str) -> Event | None:
    """Bybit V5 ``allLiquidation.<sym>`` item → ``kind="liquidation"``.

    Field shape in V5 allLiquidation (aggregated 500ms windows):
      - ``T``  liq timestamp (ms)
      - ``s``  symbol
      - ``S``  side of the LIQUIDATED position ("Buy" = long liqd)
      - ``v``  qty (string)
      - ``p``  price (string)
    """
    price = _to_float(data.get("p") or data.get("price"))
    qty = _to_float(data.get("v") or data.get("size") or data.get("qty"))
    if price <= 0 or qty <= 0:
        return None
    notional = price * qty
    # Bybit `S` = the side of the liquidated position.
    # "Buy" = long position liquidated, "Sell" = short position liquidated.
    # Canonical tag convention: liq_long = a long got rekt.
    side_raw = str(data.get("S") or data.get("side", "")).lower()
    liq_tag = "liq_long" if side_raw == "buy" else "liq_short"
    ts_ms = data.get("T") or data.get("updatedTime")
    try:
        ts_ms = int(ts_ms) if ts_ms is not None else None
    except (TypeError, ValueError):
        ts_ms = None
    return Event(
        ts=_ts_from_ms(ts_ms),
        source="bybit.allLiquidation",
        kind="liquidation",
        symbol=data.get("s") or data.get("symbol") or symbol,
        payload={
            "price": price,
            "qty": qty,
            "notional_usd": notional,
            "liquidated_side": side_raw.upper(),
            "order_type": None,
            "status": None,
        },
        tags={"liquidation", liq_tag},
        severity=_severity_from_notional(notional, scale=7.0),
    )


class BybitPerpsWS:
    """Primary-venue Bybit V5 public linear WS client for BTCUSDT.

    Not to be confused with :class:`BybitPublicWS` in ``bybit_ws.py``,
    which emits ``kind="cross_trade"`` events as a secondary cross-venue
    signal alongside Binance. This class emits primary-venue events
    and is intended to be the only market-data client running in
    ``run_bot`` when ``exchange == "bybit_perps"``.
    """

    def __init__(self, bus: EventBus, symbol: str = "BTCUSDT") -> None:
        self.bus = bus
        self.symbol = symbol
        self._topics = [
            f"publicTrade.{symbol}",
            f"tickers.{symbol}",
            f"orderbook.50.{symbol}",
            f"allLiquidation.{symbol}",
        ]

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                logger.info("bybit_perps_ws: connecting to {}", URL)
                async with websockets.connect(
                    URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2 ** 20,
                ) as ws:
                    sub = {"op": "subscribe", "args": self._topics}
                    await ws.send(serde.dumps(sub).decode())
                    logger.info(
                        "bybit_perps_ws: subscribed to {}", ", ".join(self._topics),
                    )
                    backoff = 1.0
                    # Bybit recommends application-level pings every ~20 s on
                    # long-lived public streams. The `ping_interval` arg covers
                    # protocol-level pings; this heartbeat covers the app ping.
                    ping_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            await self._handle_raw(raw)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except (asyncio.CancelledError, Exception):
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "bybit_perps_ws: disconnected ({}), reconnect in {:.1f}s",
                    e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(serde.dumps({"op": "ping"}).decode())
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = serde.loads(raw)
        except serde.JSONDecodeError:
            logger.error("bybit_perps_ws: bad json {!r}", raw[:200])
            return
        # Bybit control frames (subscription ack, pong, etc.) carry no topic.
        if "topic" not in msg:
            if msg.get("op") in ("subscribe", "pong"):
                return
            if msg.get("success") is False:
                logger.warning("bybit_perps_ws: control error {!r}", msg)
            return
        topic = msg["topic"]
        mtype = msg.get("type", "")
        data = msg.get("data")
        if data is None:
            return
        # publicTrade carries a list; others carry a single dict.
        if topic.startswith("publicTrade."):
            for item in data:
                ev = _normalize_public_trade(item, self.symbol)
                if ev is not None:
                    await self.bus.publish(ev)
        elif topic.startswith("tickers."):
            if not isinstance(data, dict):
                return
            ev = _normalize_ticker(data, self.symbol)
            if ev is not None:
                await self.bus.publish(ev)
        elif topic.startswith("orderbook."):
            # Snapshot-only. Delta frames (type=="delta") are discarded in v1 —
            # we rely on the snapshot cadence (several per second on BTCUSDT)
            # for top-of-book + 5-bp depth sampling.
            if mtype != "snapshot" or not isinstance(data, dict):
                return
            ev = _normalize_book_snapshot(data, self.symbol)
            if ev is not None:
                await self.bus.publish(ev)
        elif topic.startswith("allLiquidation."):
            # V5 allLiquidation frames carry a list of aggregated liqs.
            if isinstance(data, list):
                for item in data:
                    ev = _normalize_liquidation(item, self.symbol)
                    if ev is not None:
                        await self.bus.publish(ev)
            elif isinstance(data, dict):
                ev = _normalize_liquidation(data, self.symbol)
                if ev is not None:
                    await self.bus.publish(ev)
