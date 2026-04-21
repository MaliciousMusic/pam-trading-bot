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

Phase G3 — proxy book events
----------------------------
`bar_to_book_events()` synthesizes coarse `kind="book"` events from the
same OHLCV input so liquidity-sensitive tribes (microstructure_prober,
and the depth-aware projector kernel) have something to train on in
replay. It is a proxy, not live depth — depth and spread are derived
from bar volatility and volume:

  spread_bps ≈ clamp((high - low) / close × 10_000 × alpha, 0.5, 20.0)
  depth_5bps ≈ beta × quote_volume / bar_minutes
  bid/ask split is balanced with a small tilt toward taker-buy direction

This is strictly a fidelity fallback. Whenever real L2 data is
available (live Binance, recorded tapes), the proxy should be OFF.
`build_event_stream(..., synth_book=False)` stays the default so
existing replay baselines remain byte-identical; flip it on explicitly
for enriched calibration runs.
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


# ---------------------------------------------------------------------------
# Phase G3 — proxy book-depth synthesis (opt-in)
# ---------------------------------------------------------------------------

# Tunables exposed as module constants so tests / config can override without
# threading a dozen args through every call-site. These defaults are calibrated
# against a 2026-04-21 live 5-min snapshot (spread ≈ 0.01 bps, bid_depth_5bps
# ≈ $400k, ask_depth_5bps ≈ $100k on BTCUSDT perps). Proxy values will be
# noisier, but they put the tribes in the right regime.

# Fraction of bar HL range we interpret as an instantaneous spread. Real
# intra-bar spread is << HL range; 0.01 maps a 10-bps HL into a 0.1-bps
# spread, which sits in the live-observed band.
PROXY_SPREAD_ALPHA = 0.01
# Spread clamp (bps). Floor = hardware-lane tightness during calm periods;
# ceil = stress-regime; wider would look artificial and distort the projector
# kernel's `depth * pressure / spread` weighting.
PROXY_SPREAD_MIN_BPS = 0.5
PROXY_SPREAD_MAX_BPS = 20.0
# Depth @5bps as a fraction of per-minute quote volume. Live observation:
# ~$400k depth vs ~$200M/min notional on busy periods → ~0.002. Use
# 0.003 to match the calmer end of the band where proxy is most useful.
PROXY_DEPTH_BETA = 0.003
PROXY_DEPTH_MIN_USD = 10_000.0
PROXY_DEPTH_MAX_USD = 5_000_000.0
# Flow asymmetry — how much the bid/ask depth imbalance follows the
# taker-buy fraction. 0 = symmetric, 1.0 = fully lopsided. 0.35 matches
# the live distribution where the bid/ask split typically sits around
# 60/40 to 75/25 on the dominant side.
PROXY_DEPTH_FLOW_TILT = 0.35
# Emit N book events per bar, spaced uniformly across the minute. Each
# event uses the OHLC polyline price at its fractional position as the
# mid, so bid/ask straddle the synthetic path. 6 per minute ≈ 100ms-ish
# cadence collapsed to 10s intervals — enough for the microstructure
# tribe to see a depth-tape, not so many that replay storage balloons.
PROXY_BOOK_EVENTS_PER_BAR = 6


def bar_to_book_events(
    bar: dict,
    *,
    symbol: str = "BTCUSDT",
    events_per_bar: int = PROXY_BOOK_EVENTS_PER_BAR,
) -> list[Event]:
    """Emit proxy `kind="book"` events from an OHLCV bar.

    The events mimic the shape of live Binance `@depth20` events
    (`best_bid`, `best_ask`, `mid`, `spread_bps`, `bid_depth_5bps_usd`,
    `ask_depth_5bps_usd`) but the numbers are derived from the bar's
    volatility + volume rather than a real L2 snapshot.

    Zero-volume / degenerate bars return an empty list so ingest stays
    quiet rather than polluting MarketState with placeholder depth.
    """
    open_ms = int(bar["open_time_ms"])
    close_ms = int(bar["close_time_ms"])
    span_ms = max(1, close_ms - open_ms + 1)
    bar_minutes = span_ms / 60_000.0

    total_quote = float(bar["quote_volume"])
    if total_quote <= 0 or events_per_bar < 1:
        return []

    hl_frac = max(0.0, (float(bar["high"]) - float(bar["low"])) / max(float(bar["close"]), 1e-9))
    spread_bps = max(
        PROXY_SPREAD_MIN_BPS,
        min(PROXY_SPREAD_MAX_BPS, hl_frac * 10_000.0 * PROXY_SPREAD_ALPHA),
    )

    vol_per_min = total_quote / max(bar_minutes, 1e-9)
    depth_mid_usd = max(
        PROXY_DEPTH_MIN_USD,
        min(PROXY_DEPTH_MAX_USD, PROXY_DEPTH_BETA * vol_per_min),
    )

    # Flow tilt — positive when taker-buys dominate (push ask depth down,
    # bid depth up, mimicking a market that has been hit on the ask).
    buy_quote = float(bar["taker_buy_quote_volume"])
    buy_frac = buy_quote / total_quote  # ∈ [0, 1]
    tilt = (buy_frac - 0.5) * 2.0 * PROXY_DEPTH_FLOW_TILT  # ∈ [-tilt, +tilt]
    bid_depth = depth_mid_usd * (1.0 + tilt)
    ask_depth = depth_mid_usd * (1.0 - tilt)

    events: list[Event] = []
    n = int(events_per_bar)
    for k in range(n):
        frac = (k + 0.5) / n
        t_ms = open_ms + int(frac * span_ms)
        ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
        mid = _ohlc_price_at(bar, frac)
        half = mid * spread_bps / 2.0 / 10_000.0
        best_bid = mid - half
        best_ask = mid + half
        events.append(Event(
            ts=ts,
            source="replay.depth20_proxy",
            kind="book",
            symbol=symbol,
            payload={
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread_bps": spread_bps,
                "bid_depth_5bps_usd": bid_depth,
                "ask_depth_5bps_usd": ask_depth,
            },
            tags={"orderbook_tick", "book", "proxy"},
            severity=0.0,
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
