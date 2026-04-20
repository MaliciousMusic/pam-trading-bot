"""OKX public WebSocket ingester (BTC-USDT-SWAP perpetual).

Subscribes to the `trades` channel on OKX v5 public stream. Emits
`kind="cross_trade"` events so the execution venue (Binance) is not
contaminated by external flow — cross-venue series live alongside the
main `taker_flow` in MarketState.

Endpoint: wss://ws.okx.com:8443/ws/v5/public
Subscribe:
    {"op":"subscribe","args":[{"channel":"trades","instId":"BTC-USDT-SWAP"}]}

Each update is:
    {"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},
     "data":[{"instId":"BTC-USDT-SWAP","tradeId":"x","px":"p","sz":"q",
              "side":"buy"|"sell","ts":"ms"}, ...]}

`side` is the taker side. `sz` is in contracts — for BTC-USDT-SWAP one
contract is 0.01 BTC, so we multiply by 0.01 to get BTC qty.
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


URL = "wss://ws.okx.com:8443/ws/v5/public"
CONTRACT_MULTIPLIER = 0.01   # 1 contract = 0.01 BTC for BTC-USDT-SWAP
INST_ID = "BTC-USDT-SWAP"


def _ts_from_ms(ms: str | int | None) -> datetime:
    if ms is None:
        return Event.now_utc()
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return Event.now_utc()


def _severity(notional_usd: float) -> float:
    if notional_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(notional_usd + 1) / 8.0))


class OKXPublicWS:
    def __init__(self, bus: EventBus, inst_id: str = INST_ID) -> None:
        self.bus = bus
        self.inst_id = inst_id

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                logger.info("okx_ws: connecting to {}", URL)
                async with websockets.connect(
                    URL,
                    ping_interval=None,   # OKX wants app-level "ping" text
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    await ws.send(orjson.dumps({
                        "op": "subscribe",
                        "args": [{"channel": "trades", "instId": self.inst_id}],
                    }).decode())
                    backoff = 1.0
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
                    "okx_ws: disconnected ({}), reconnect in {:.1f}s", e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _heartbeat(self, ws) -> None:
        """OKX closes idle sockets after 30s — send 'ping' text every 20s."""
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send("ping")
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _handle_raw(self, raw: str | bytes) -> None:
        if raw == "pong" or raw == b"pong":
            return
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        arg = msg.get("arg") or {}
        if arg.get("channel") != "trades":
            return
        for item in msg.get("data") or ():
            try:
                price = float(item["px"])
                size_contracts = float(item["sz"])
            except (KeyError, ValueError, TypeError):
                continue
            qty = size_contracts * CONTRACT_MULTIPLIER
            notional = price * qty
            is_sell = str(item.get("side", "")).lower() == "sell"
            tag_side = "taker_sell" if is_sell else "taker_buy"
            ev = Event(
                ts=_ts_from_ms(item.get("ts")),
                source="okx.trades",
                kind="cross_trade",
                symbol=item.get("instId") or self.inst_id,
                payload={
                    "venue": "okx",
                    "price": price,
                    "qty": qty,
                    "notional_usd": notional,
                    "is_sell_aggressor": is_sell,
                },
                tags={"cross_trade", "okx", tag_side},
                severity=_severity(notional),
            )
            await self.bus.publish(ev)
