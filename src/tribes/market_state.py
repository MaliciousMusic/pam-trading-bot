"""Rolling-window view of recent market activity.

One process-wide instance is fed every normalized Event from the bus.
Tribes read from it at vote time — they don't keep private state unless
they genuinely need to (cooldowns, held positions).

All windows store `(epoch_seconds, value)` tuples and trim on the fly
to the largest requested window. Keep this lean; tribes that need
bespoke series can carry their own.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Iterable

from ..core.types import Event


class RollingSeries:
    """Time-stamped series, trimmed to a maximum age."""

    __slots__ = ("max_age_s", "_data")

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._data: deque[tuple[float, float]] = deque()

    def add(self, ts: datetime, value: float) -> None:
        t = ts.timestamp()
        self._data.append((t, value))
        cutoff = t - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def advance_clock(self, ts: datetime) -> None:
        """Trim stale entries relative to `ts` without adding a new sample.
        Used by the chess projector when the virtual clock moves forward."""
        cutoff = ts.timestamp() - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def _window(self, window_s: float | None) -> Iterable[tuple[float, float]]:
        if not self._data:
            return ()
        latest = self._data[-1][0]
        w = window_s if window_s is not None else self.max_age_s
        cutoff = latest - w
        return ((t, v) for t, v in self._data if t >= cutoff)

    def sum(self, window_s: float | None = None) -> float:
        return sum(v for _, v in self._window(window_s))

    def first_last(self, window_s: float | None = None) -> tuple[float, float] | None:
        xs = list(self._window(window_s))
        if not xs:
            return None
        return xs[0][1], xs[-1][1]

    def min_max(self, window_s: float | None = None) -> tuple[float, float] | None:
        xs = [v for _, v in self._window(window_s)]
        if not xs:
            return None
        return min(xs), max(xs)


class MarketState:
    """Shared read model the voting tribes query."""

    MAX_AGE_S = 600.0  # biggest window any tribe will ask for; extend if needed.

    def __init__(self) -> None:
        self.taker_flow = RollingSeries(self.MAX_AGE_S)   # signed notional (+buy, -sell)
        self.long_liq = RollingSeries(self.MAX_AGE_S)      # long-position liquidation notional
        self.short_liq = RollingSeries(self.MAX_AGE_S)     # short-position liquidation notional
        self.prices = RollingSeries(self.MAX_AGE_S)        # mark/trade price samples
        # Social/sentiment series — signed score ∈ [-1,+1] per reddit post / pulse.
        # Kept longer than market windows because headlines carry across minutes.
        self.social = RollingSeries(1800.0)                # 30min social pulse history
        self.last_price: float | None = None
        self.last_mark_price: float | None = None
        self.last_funding_rate: float = 0.0
        self.fgi_value: int | None = None                  # 0..100
        self.fgi_classification: str = ""
        self.fgi_ts: datetime | None = None
        self.last_update: datetime | None = None

    # -- ingest ---------------------------------------------------------

    def ingest(self, ev: Event) -> None:
        self.last_update = ev.ts
        if ev.kind == "trade":
            p = ev.payload.get("price")
            n = ev.payload.get("notional_usd", 0.0)
            signed = -n if ev.payload.get("is_sell_aggressor") else n
            self.taker_flow.add(ev.ts, signed)
            if p is not None:
                self.last_price = p
                self.prices.add(ev.ts, p)
        elif ev.kind == "liquidation":
            n = ev.payload.get("notional_usd", 0.0)
            if "liq_long" in ev.tags:
                self.long_liq.add(ev.ts, n)
            elif "liq_short" in ev.tags:
                self.short_liq.add(ev.ts, n)
        elif ev.kind == "funding":
            mp = ev.payload.get("mark_price")
            if mp:
                self.last_mark_price = mp
                if self.last_price is None:
                    self.last_price = mp
                self.prices.add(ev.ts, mp)
            self.last_funding_rate = ev.payload.get("funding_rate", 0.0)
        elif ev.kind == "social_pulse":
            score = float(ev.payload.get("score", 0.0))
            if score != 0.0:
                self.social.add(ev.ts, score)
        elif ev.kind == "sentiment":
            # FGI update — keep the latest value; it changes at most daily.
            v = ev.payload.get("value")
            if isinstance(v, (int, float)):
                self.fgi_value = int(v)
                self.fgi_classification = str(ev.payload.get("classification", ""))
                self.fgi_ts = ev.ts

    # -- queries --------------------------------------------------------

    def taker_flow_usd(self, window_s: float) -> float:
        return self.taker_flow.sum(window_s)

    def long_liq_usd(self, window_s: float) -> float:
        return self.long_liq.sum(window_s)

    def short_liq_usd(self, window_s: float) -> float:
        return self.short_liq.sum(window_s)

    def price_change_bps(self, window_s: float) -> float:
        fl = self.prices.first_last(window_s)
        if fl is None or fl[0] == 0:
            return 0.0
        return (fl[1] - fl[0]) / fl[0] * 10_000.0

    def price_range(self, window_s: float) -> tuple[float, float] | None:
        return self.prices.min_max(window_s)

    # -- sentiment queries ----------------------------------------------

    def social_score(self, window_s: float) -> float:
        """Summed signed sentiment over `window_s`. Positive = bullish chatter."""
        return self.social.sum(window_s)

    def social_volume(self, window_s: float) -> int:
        """Raw count of scored social pulses in the window."""
        # RollingSeries exposes trimming via sum path; reuse _window().
        return sum(1 for _ in self.social._window(window_s))

    def fgi_bias(self) -> float:
        """Return FGI as a signed bias in [-1,+1] — positive = greed, negative = fear.
        Zero when unknown."""
        if self.fgi_value is None:
            return 0.0
        return (self.fgi_value - 50) / 50.0

    # -- projection support --------------------------------------------

    def snapshot(self) -> "MarketState":
        """Full copy so the chess projector can mutate freely without
        corrupting the live read model."""
        s = MarketState()
        s.taker_flow._data = deque(self.taker_flow._data)
        s.long_liq._data = deque(self.long_liq._data)
        s.short_liq._data = deque(self.short_liq._data)
        s.prices._data = deque(self.prices._data)
        s.social._data = deque(self.social._data)
        s.last_price = self.last_price
        s.last_mark_price = self.last_mark_price
        s.last_funding_rate = self.last_funding_rate
        s.fgi_value = self.fgi_value
        s.fgi_classification = self.fgi_classification
        s.fgi_ts = self.fgi_ts
        s.last_update = self.last_update
        return s

    def advance_clock(self, ts: datetime) -> None:
        self.taker_flow.advance_clock(ts)
        self.long_liq.advance_clock(ts)
        self.short_liq.advance_clock(ts)
        self.prices.advance_clock(ts)
        self.social.advance_clock(ts)
        self.last_update = ts
