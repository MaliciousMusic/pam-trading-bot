"""Higher-timeframe trend poller (free Binance 1h klines).

Pulls the last N closed 1h candles every `poll_seconds`, computes an EMA
fast/slow spread on closes, normalizes by the mean candle range (a
light-weight ATR), passes it through tanh so the output sits in [-1, +1],
and publishes a `kind="htf_trend"` event that MarketState ingests.

Why this exists: the council lives on a 10-minute horizon. Without a
higher-TF anchor, it happily stacks shorts into a ripping 4h uptrend. A
single scalar trend score per poll is enough to add a soft counter-trend
shrink in the decision engine — we never block entries, only shrink when
we're picking fights with the larger structure.

The score is a *bias*, not a forecast: +1 = strong up-trend, -1 = strong
down-trend, ~0 = chop / reversal in progress. The decision engine interprets
the magnitude; the poller doesn't know the council's direction.

Event shape:
  kind="htf_trend"
  source="binance.klines.<timeframe>"
  payload={timeframe, score, ema_fast, ema_slow, atr, last_close}
  tags={"htf_trend", "<timeframe>"}
  severity=0 (state variable, not a stimulus)
"""
from __future__ import annotations

import asyncio
import math

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


USER_AGENT = "pam-council/0.2 (research bot)"
REQUEST_TIMEOUT_S = 20
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


def _ema(values: list[float], period: int) -> float:
    """Standard EMA. Seeded from the first `period` SMA; stable for short
    series (period ≤ len(values))."""
    if not values or period <= 0:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = seed
    for v in values[period:]:
        out = alpha * v + (1 - alpha) * out
    return out


class HTFTrendPoller:
    def __init__(
        self,
        bus: EventBus,
        *,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        lookback_candles: int = 50,
        fast_ema: int = 8,
        slow_ema: int = 21,
        poll_seconds: int = 60,
        retry_seconds: int = 30,
    ) -> None:
        self.bus = bus
        self.symbol = symbol
        self.timeframe = timeframe
        self.lookback = max(slow_ema + 5, int(lookback_candles))
        self.fast_ema = int(fast_ema)
        self.slow_ema = int(slow_ema)
        self.poll_seconds = int(poll_seconds)
        self.retry_seconds = int(retry_seconds)

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as s:
            while True:
                try:
                    await self._poll_once(s)
                    await asyncio.sleep(self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[htf] poll failed: {}", e)
                    await asyncio.sleep(self.retry_seconds)

    async def _poll_once(self, s: aiohttp.ClientSession) -> None:
        async with s.get(
            KLINES_URL,
            params={
                "symbol": self.symbol,
                "interval": self.timeframe,
                "limit": self.lookback,
            },
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
        ) as r:
            r.raise_for_status()
            rows = await r.json()
        if not isinstance(rows, list) or len(rows) < self.slow_ema:
            return
        # Each row: [openTime, open, high, low, close, volume, closeTime, ...]
        closes = [float(row[4]) for row in rows]
        ranges = [float(row[2]) - float(row[3]) for row in rows]
        ema_fast = _ema(closes, self.fast_ema)
        ema_slow = _ema(closes, self.slow_ema)
        # Mean candle range as the normalizer. Using mean over the full
        # window keeps it insensitive to a single flash bar; tanh squashes
        # to [-1, +1] so the decision engine can use |score| directly as a
        # shrink magnitude.
        atr = sum(ranges) / len(ranges) if ranges else 0.0
        if atr <= 0:
            return
        raw = (ema_fast - ema_slow) / atr
        score = math.tanh(raw)
        last_close = closes[-1]
        ev = Event(
            ts=Event.now_utc(),
            source=f"binance.klines.{self.timeframe}",
            kind="htf_trend",
            symbol=self.symbol,
            payload={
                "timeframe": self.timeframe,
                "score": score,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr,
                "last_close": last_close,
            },
            tags={"htf_trend", self.timeframe},
            severity=0.0,
        )
        await self.bus.publish(ev)
        logger.debug(
            "[htf] {} score={:+.2f} emaF={:.2f} emaS={:.2f} atr={:.2f}",
            self.timeframe, score, ema_fast, ema_slow, atr,
        )
