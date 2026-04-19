"""Convert 1m OHLCV bars → synthetic Event streams for the live pipeline.

A 1m kline carries OHLC, total volume, and taker-buy volume. We synthesize
`synth_rate` trade Events per bar that preserve three first-moment invariants:

  1. Total synthesized notional_usd ≈ bar quote_volume
  2. Total synthesized taker-buy notional ≈ bar taker_buy_quote_volume
  3. All prices ∈ [low, high]; all timestamps ∈ [open_time, close_time)

Price path is a piecewise-linear OHLC polyline:
  - Up-bar  (close ≥ open):  open → low(t=0.25) → high(t=0.75) → close
  - Down-bar (close <  open):  open → high(t=0.25) → low(t=0.75) → close

Side allocation is deterministic (carry method): cumulative buys stay on
target across the bar, so flow signature is preserved at every intra-bar
time — not just the aggregate.

Higher `synth_rate` better approximates real aggTrade granularity at more
compute. The multi-variant sprint runs 6/60/600 side-by-side to measure
how sensitive tribe votes actually are to this knob.

Fidelity caveat: Binance 1m klines carry no book / liquidation / news
data. Tribes that key on those channels receive nothing from replay and
abstain more often than live. The summary report lists per-tribe
activity rate so silent tribes are surfaced, not hidden.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ..core.types import Event


def _severity_from_notional(notional_usd: float, scale: float = 8.0) -> float:
    """Matches the live normalizer in src/ingress/binance_ws.py so synthetic
    events have comparable severity distributions."""
    if notional_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(notional_usd + 1) / scale))


def _ohlc_price_at(bar: dict, frac: float) -> float:
    """Piecewise-linear OHLC polyline at fractional time frac ∈ [0, 1].

    The direction heuristic (up-bar dips first then rallies; down-bar
    rallies first then dips) is a modeling choice — we can't recover the
    true intrabar path from OHLC alone. What matters: H and L are both
    touched, and start/end anchor on O/C."""
    o = bar["open"]
    h = bar["high"]
    l = bar["low"]
    c = bar["close"]
    if c >= o:
        mid1_t, mid1_v = 0.25, l
        mid2_t, mid2_v = 0.75, h
    else:
        mid1_t, mid1_v = 0.25, h
        mid2_t, mid2_v = 0.75, l
    if frac <= mid1_t:
        return o + (mid1_v - o) * (frac / mid1_t)
    if frac <= mid2_t:
        return mid1_v + (mid2_v - mid1_v) * ((frac - mid1_t) / (mid2_t - mid1_t))
    return mid2_v + (c - mid2_v) * ((frac - mid2_t) / (1 - mid2_t))


def _trade_event(
    ts: datetime,
    price: float,
    qty: float,
    *,
    is_sell: bool,
    symbol: str,
    agg_id: str,
    notional: float,
) -> Event:
    tag_side = "taker_sell" if is_sell else "taker_buy"
    return Event(
        ts=ts,
        source="replay.aggTrade",
        kind="trade",
        symbol=symbol,
        payload={
            "price": price,
            "qty": qty,
            "notional_usd": notional,
            "is_sell_aggressor": is_sell,
            "agg_id": agg_id,
        },
        tags={"price", "trade", tag_side},
        severity=_severity_from_notional(notional, scale=8.0),
    )


def bar_to_trade_events(
    bar: dict,
    synth_rate: int,
    *,
    symbol: str = "BTCUSDT",
) -> list[Event]:
    """Emit `synth_rate` trade Events spread across the bar's minute.

    Zero-volume bars emit a single zero-notional close-price print so
    `MarketState.last_price` still advances; severity is 0 so tribes
    ignore it as a stimulus."""
    open_ms = int(bar["open_time_ms"])
    close_ms = int(bar["close_time_ms"])
    span_ms = max(1, close_ms - open_ms + 1)

    total_quote = float(bar["quote_volume"])
    buy_quote = float(bar["taker_buy_quote_volume"])
    sell_quote = max(0.0, total_quote - buy_quote)

    if total_quote <= 0 or synth_rate < 1:
        ts = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
        return [_trade_event(
            ts, float(bar["close"]), 0.0,
            is_sell=False, symbol=symbol,
            agg_id=f"replay_{open_ms}_zero", notional=0.0,
        )]

    n = int(synth_rate)
    buy_frac = buy_quote / total_quote
    target_buys = round(n * buy_frac)
    sell_count = n - target_buys
    buy_quote_per = buy_quote / target_buys if target_buys > 0 else 0.0
    sell_quote_per = sell_quote / sell_count if sell_count > 0 else 0.0

    buys_done = 0
    events: list[Event] = []
    for k in range(n):
        expected_by_now = (k + 1) * target_buys / n
        if buys_done + 1e-9 < expected_by_now and buys_done < target_buys:
            is_sell = False
            buys_done += 1
        else:
            is_sell = True
        frac = (k + 0.5) / n
        t_ms = open_ms + int(frac * span_ms)
        ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
        price = _ohlc_price_at(bar, frac)
        notional = sell_quote_per if is_sell else buy_quote_per
        qty = notional / price if price > 0 else 0.0
        events.append(_trade_event(
            ts, price, qty,
            is_sell=is_sell, symbol=symbol,
            agg_id=f"replay_{open_ms}_{k}", notional=notional,
        ))
    return events


def funding_to_event(
    funding_row: dict,
    *,
    symbol: str = "BTCUSDT",
    mark_price_hint: float | None = None,
) -> Event:
    """Historical 8h funding-rate row → `kind="funding"` Event.

    The /fundingRate response carries only the rate + timestamp (no mark).
    Caller passes the most-recent trade price as `mark_price_hint` so
    `MarketState.last_mark_price` updates coherently with the funding event.
    Pass None to leave `last_mark_price` untouched (ingest skips the
    update when `mark_price` is falsy)."""
    ts = datetime.fromtimestamp(funding_row["funding_time_ms"] / 1000, tz=timezone.utc)
    rate = float(funding_row["funding_rate"])
    mp = float(mark_price_hint) if mark_price_hint else 0.0
    return Event(
        ts=ts,
        source="replay.fundingRate",
        kind="funding",
        symbol=symbol,
        payload={
            "mark_price": mp,
            "index_price": mp,
            "funding_rate": rate,
            "next_funding_ts_ms": None,
        },
        tags={"funding", "mark_price"},
        severity=min(1.0, abs(rate) * 2000.0),
    )
