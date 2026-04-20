"""Stablecoin supply poller — free CoinGecko public endpoints, no auth.

Tracks circulating supply of USDT + USDC. The delta over a rolling window
is our free-tier proxy for "stablecoin bridge flow": when supply expands
(Tether minting, USDC mints), the typical destination is exchange deposits,
which empirically precedes BTC bids. When supply contracts (redemptions),
fresh buying pressure drains.

We cannot see exchange-resident stablecoin balances directly on the free
tier (that requires Glassnode paid or Dune queries with API keys). But
the aggregate supply-change signal is strongly correlated in the literature
(Kaiko/Glassnode research 2022-2024) with 2-4h forward BTC returns.

Event shape:
    kind="stablecoin_supply"
    source="coingecko.stablecoin"
    payload={usdt_supply, usdc_supply, combined_supply, timestamp}
    severity=0.0  (state variable)

Poll cadence: 1800s (30min). CoinGecko updates its supply figure a few
times per hour; polling faster wastes rate-limit budget. Free tier allows
~30 req/min — we use 2 per 30min = trivial. Two concurrent requests per
poll (USDT + USDC).
"""
from __future__ import annotations

import asyncio

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


USER_AGENT = "pam-council/0.2 (research bot)"
REQUEST_TIMEOUT_S = 20
API_BASE = "https://api.coingecko.com/api/v3"


class StablecoinSupplyPoller:
    """Polls CoinGecko market_data.circulating_supply for USDT + USDC and
    emits a combined supply event per poll."""

    COINS = {
        "usdt": "tether",
        "usdc": "usd-coin",
    }

    def __init__(
        self,
        bus: EventBus,
        *,
        poll_seconds: int = 1800,
        retry_seconds: int = 300,
    ) -> None:
        self.bus = bus
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as s:
            while True:
                try:
                    await self._poll(s)
                    await asyncio.sleep(self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[stablecoin] poll failed: {}", e)
                    await asyncio.sleep(self.retry_seconds)

    async def _fetch_supply(self, s: aiohttp.ClientSession, cg_id: str) -> float | None:
        """Fetch circulating supply for a CoinGecko coin id. Returns None
        on any error — caller decides whether to emit a partial event."""
        try:
            async with s.get(
                f"{API_BASE}/coins/{cg_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "true",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                data = await r.json()
        except Exception as e:
            logger.warning("[stablecoin] fetch {} failed: {}", cg_id, e)
            return None
        md = (data or {}).get("market_data") or {}
        supply = md.get("circulating_supply")
        if supply is None:
            return None
        try:
            return float(supply)
        except (TypeError, ValueError):
            return None

    async def _poll(self, s: aiohttp.ClientSession) -> None:
        # Fire the two coin requests concurrently — still well under the
        # free-tier rate limit (30 req/min) even if we polled every minute.
        usdt, usdc = await asyncio.gather(
            self._fetch_supply(s, self.COINS["usdt"]),
            self._fetch_supply(s, self.COINS["usdc"]),
            return_exceptions=False,
        )
        if usdt is None and usdc is None:
            logger.warning("[stablecoin] both fetches failed, skipping emit")
            return
        combined = (usdt or 0.0) + (usdc or 0.0)
        ev = Event(
            ts=Event.now_utc(),
            source="coingecko.stablecoin",
            kind="stablecoin_supply",
            symbol="STABLE",
            payload={
                "usdt_supply": usdt or 0.0,
                "usdc_supply": usdc or 0.0,
                "combined_supply": combined,
            },
            tags={"stablecoin", "coingecko"},
            severity=0.0,
        )
        await self.bus.publish(ev)
