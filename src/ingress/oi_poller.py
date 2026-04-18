"""Multi-venue open-interest poller (free public endpoints — no auth).

Pulls OI from Binance, Bybit, OKX every `poll_seconds` and emits one
`kind="oi"` event per venue. Payload carries the venue label, notional
in USD, and the contract count so MarketState can track per-venue
trajectories and the engine can reason about leverage build-up.

Why not CoinGlass directly? Their free tier still requires an API key
for the aggregated endpoint and sandboxes daily requests. The three
largest perp venues individually serve OI without auth — summing them
client-side reproduces ~85% of CoinGlass's "total BTC perp OI" signal
for zero cost and zero rate-limit surprises.

Event shape:
  kind="oi"
  source="binance.oi" | "bybit.oi" | "okx.oi"
  payload={venue, oi_contracts, oi_usd, mark_price}
  tags={"oi", venue}
  severity=0 (OI is a state variable, not a shock)

Rising OI with rising price = leverage piling on in the trend direction.
Falling OI in a drawdown = deleveraging cascade. Tribes consume these
via MarketState.oi_total_usd() / oi_delta_pct(window_s).
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


USER_AGENT = "pam-council/0.2 (research bot)"
REQUEST_TIMEOUT_S = 15


class OIPoller:
    """Polls three venues concurrently and emits per-venue OI events.

    Design: every poll fires three requests in parallel; each completes
    or fails independently. One venue being down does not starve the
    others. The mark-price-to-USD conversion is done here (not on the
    strategy side) so OI events carry USD notional directly — the
    engine never sees raw contracts.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        symbol: str = "BTCUSDT",
        poll_seconds: int = 60,
        retry_seconds: int = 30,
    ) -> None:
        self.bus = bus
        self.symbol = symbol
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as s:
            while True:
                try:
                    await asyncio.gather(
                        self._poll_binance(s),
                        self._poll_bybit(s),
                        self._poll_okx(s),
                        return_exceptions=False,
                    )
                    await asyncio.sleep(self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[oi] sweep failed: {}", e)
                    await asyncio.sleep(self.retry_seconds)

    async def _emit(
        self,
        venue: str,
        oi_contracts: float,
        oi_usd: float,
        mark_price: float,
    ) -> None:
        ev = Event(
            ts=Event.now_utc(),
            source=f"{venue}.oi",
            kind="oi",
            symbol=self.symbol,
            payload={
                "venue": venue,
                "oi_contracts": oi_contracts,
                "oi_usd": oi_usd,
                "mark_price": mark_price,
            },
            tags={"oi", venue},
            severity=0.0,
        )
        await self.bus.publish(ev)

    # -- Binance -------------------------------------------------------
    async def _poll_binance(self, s: aiohttp.ClientSession) -> None:
        """Binance reports `openInterest` in contracts (= coins for coin-M,
        = coins for USDT-M too since contract size is 1 BTC). The paired
        mark price endpoint gives USD conversion."""
        try:
            async with s.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": self.symbol},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                oi_data = await r.json()
            async with s.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": self.symbol},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                mark = await r.json()
        except Exception as e:
            logger.warning("[oi] binance poll failed: {}", e)
            return
        contracts = float(oi_data.get("openInterest") or 0.0)
        price = float(mark.get("markPrice") or 0.0)
        if contracts <= 0 or price <= 0:
            return
        await self._emit("binance", contracts, contracts * price, price)

    # -- Bybit ---------------------------------------------------------
    async def _poll_bybit(self, s: aiohttp.ClientSession) -> None:
        """Bybit v5: GET /v5/market/open-interest?category=linear&intervalTime=5min
        Returns a list of recent snapshots; we take the most recent."""
        try:
            async with s.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={
                    "category": "linear",
                    "symbol": self.symbol,
                    "intervalTime": "5min",
                    "limit": 1,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                payload: dict[str, Any] = await r.json()
            async with s.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": self.symbol},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                tick_payload: dict[str, Any] = await r.json()
        except Exception as e:
            logger.warning("[oi] bybit poll failed: {}", e)
            return
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            return
        # Bybit returns openInterest as coin count (BTC), not USD.
        contracts = float(rows[0].get("openInterest") or 0.0)
        tick_rows = ((tick_payload.get("result") or {}).get("list") or [])
        price = 0.0
        if tick_rows:
            price = float(tick_rows[0].get("markPrice") or tick_rows[0].get("lastPrice") or 0.0)
        if contracts <= 0 or price <= 0:
            return
        await self._emit("bybit", contracts, contracts * price, price)

    # -- OKX -----------------------------------------------------------
    async def _poll_okx(self, s: aiohttp.ClientSession) -> None:
        """OKX: GET /api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP
        Returns `oi` in contracts (1 contract = 0.01 BTC for BTC-USDT-SWAP),
        and `oiCcy` in the base currency (BTC). We use `oiCcy` directly as
        the coin count."""
        try:
            async with s.get(
                "https://www.okx.com/api/v5/public/open-interest",
                params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                oi_payload: dict[str, Any] = await r.json()
            async with s.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": "BTC-USDT-SWAP"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                tick_payload: dict[str, Any] = await r.json()
        except Exception as e:
            logger.warning("[oi] okx poll failed: {}", e)
            return
        rows = oi_payload.get("data") or []
        if not rows:
            return
        contracts = float(rows[0].get("oiCcy") or 0.0)   # base-currency units (BTC)
        tick_rows = tick_payload.get("data") or []
        price = 0.0
        if tick_rows:
            price = float(tick_rows[0].get("last") or tick_rows[0].get("markPx") or 0.0)
        if contracts <= 0 or price <= 0:
            return
        await self._emit("okx", contracts, contracts * price, price)
