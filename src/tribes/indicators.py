"""Indicator library — pure MarketState → signed signal functions.

Each indicator returns a scalar in [-1, +1] (bull-positive) computed from
the current MarketState. Tribes declare, in their YAML, which indicators
they watch, with what windows, and with what action (follow / fade) —
producing a blended signal that captures the archetype's "lens" on the
market without duplicating this math inside every strategy.

Contract:
    fn(state, **params) -> float      # signed signal in [-1, +1]

Missing inputs (too-short series, zero variance, no data) return 0.0 —
the indicator is silent, not offline. Strategies decide what to do with
silence (fall back to idle, or weight indicator-blend to zero).

Adding an indicator: register a pure function with @register("name") and
add the name to the per-tribe YAML `indicators` block. No other code needs
to change.
"""
from __future__ import annotations

from collections.abc import Callable
from math import sqrt
from typing import Any

from .market_state import MarketState

IndicatorFn = Callable[..., float]

_REGISTRY: dict[str, IndicatorFn] = {}


def register(name: str) -> Callable[[IndicatorFn], IndicatorFn]:
    def wrap(fn: IndicatorFn) -> IndicatorFn:
        if name in _REGISTRY:
            raise ValueError(f"indicator already registered: {name}")
        _REGISTRY[name] = fn
        return fn
    return wrap


def get(name: str) -> IndicatorFn:
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _prices(state: MarketState, window_s: float) -> list[float]:
    """Pull price samples within `window_s`. Empty if no data."""
    latest = state.prices._data[-1][0] if state.prices._data else 0.0
    cutoff = latest - window_s
    return [v for t, v in state.prices._data if t >= cutoff]


# ── Price-derived indicators ──────────────────────────────────────────────


@register("rsi")
def rsi(state: MarketState, window_s: float = 300.0,
        overbought: float = 70.0, oversold: float = 30.0,
        action: str = "fade", **_: Any) -> float:
    """Relative Strength Index over `window_s`. Computed as 100 - 100/(1+RS)
    where RS = avg_gain / avg_loss across consecutive price samples.

    Returns signed signal:
      - action="fade"   : +1 at oversold (buy the dip), -1 at overbought.
      - action="follow" : opposite sign (breakout traders follow momentum).

    Silent when fewer than 4 samples or flat tape (avg_loss == 0)."""
    xs = _prices(state, window_s)
    if len(xs) < 4:
        return 0.0
    gains, losses = 0.0, 0.0
    for a, b in zip(xs[:-1], xs[1:]):
        d = b - a
        if d > 0:
            gains += d
        elif d < 0:
            losses -= d
    if gains + losses <= 0:
        return 0.0
    avg_loss = losses / max(len(xs) - 1, 1)
    avg_gain = gains / max(len(xs) - 1, 1)
    if avg_loss == 0:
        rsi_val = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_val = 100.0 - 100.0 / (1.0 + rs)
    # Normalize [oversold..overbought] into [-1..+1] signed by action.
    mid = (overbought + oversold) / 2.0
    half = max((overbought - oversold) / 2.0, 1.0)
    raw = _clip((rsi_val - mid) / half)              # >0 = overbought, <0 = oversold
    # Default (fade): overbought → sell (negative), oversold → buy (positive).
    return _clip(-raw if action == "fade" else raw)


@register("price_zscore")
def price_zscore(state: MarketState, window_s: float = 600.0,
                 extreme: float = 2.0, action: str = "fade", **_: Any) -> float:
    """Z-score of current price vs window mean. `extreme` caps magnitude.
    action=fade returns +1 when price is extreme-low (mean-revert up)."""
    xs = _prices(state, window_s)
    if len(xs) < 5 or state.last_price is None:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    sd = sqrt(var)
    if sd <= 0:
        return 0.0
    z = (state.last_price - m) / sd
    normalized = _clip(z / max(extreme, 0.1))         # >0 = above mean
    return _clip(-normalized if action == "fade" else normalized)


@register("price_momentum")
def price_momentum(state: MarketState, window_s: float = 60.0,
                   ref_bps: float = 15.0, action: str = "follow", **_: Any) -> float:
    """Price change over window, normalized by `ref_bps`. Positive = up-move.
    action=follow returns sign-of-move; fade inverts."""
    change_bps = state.price_change_bps(window_s)
    raw = _clip(change_bps / max(ref_bps, 1.0))
    return _clip(-raw if action == "fade" else raw)


@register("volatility_regime")
def volatility_regime(state: MarketState, window_s: float = 600.0,
                      low_bps: float = 15.0, high_bps: float = 80.0,
                      prefer: str = "low", use_realized: bool = False,
                      **_: Any) -> float:
    """Volatility regime signal. Two measurement modes:

      use_realized=False (default): range bps = (high-low)/mid × 10_000.
          Fast but spike-dominated — a single wick inflates the reading.
      use_realized=True: log-return stddev × sqrt(N) × 10_000.
          Noise-robust — averages over the whole sample path, not just
          the extremes. Better proxy for "how nervous is this tape."

    Returns +1 when the tribe is in its preferred regime, -1 when not.
      prefer="low"  → mean-reverters, grid bots: they want calm.
      prefer="high" → breakout, liq-hunter: they want violence.
    Silent when no data."""
    vol_bps: float | None = None
    if use_realized:
        vol_bps = state.realized_vol_bps(window_s)
    if vol_bps is None:
        rng = state.price_range(window_s)
        if rng is None or rng[0] <= 0:
            return 0.0
        lo, hi = rng
        vol_bps = (hi - lo) / ((hi + lo) / 2.0) * 10_000.0
    mid = (low_bps + high_bps) / 2.0
    half = max((high_bps - low_bps) / 2.0, 1.0)
    raw = _clip((vol_bps - mid) / half)               # +1 high vol, -1 low vol
    return _clip(raw if prefer == "high" else -raw)


# ── Flow-derived indicators ───────────────────────────────────────────────


@register("taker_imbalance")
def taker_imbalance(state: MarketState, window_s: float = 60.0,
                    reference_usd: float = 500_000.0,
                    action: str = "follow", **_: Any) -> float:
    """Net signed taker notional over window, normalized by `reference_usd`.
    Follow → sign-of-flow (trend-chasers). Fade → opposite (MMs, grid bots)."""
    net = state.taker_flow_usd(window_s)
    raw = _clip(net / max(reference_usd, 1.0))
    return _clip(-raw if action == "fade" else raw)


@register("liq_imbalance")
def liq_imbalance(state: MarketState, window_s: float = 60.0,
                  reference_usd: float = 500_000.0,
                  action: str = "follow", **_: Any) -> float:
    """(longs liq'd − shorts liq'd). Follow = continuation (long liqs → bearish
    follow-through → negative signal). Fade = catch-the-falling-knife."""
    longs = state.long_liq_usd(window_s)
    shorts = state.short_liq_usd(window_s)
    net = longs - shorts
    # "Follow" a cascade means TRADE WITH the flow direction that was forced
    # to close. Long liqs = forced sells = price down = short. So follow=-raw.
    raw = _clip(net / max(reference_usd, 1.0))
    return _clip(-raw if action == "follow" else raw)


@register("funding_extreme")
def funding_extreme(state: MarketState, extreme: float = 0.0005,
                    action: str = "fade", **_: Any) -> float:
    """Current funding rate, normalized by `extreme`. Fade (positive funding
    → short) is the contrarian reading; follow aligns with paid side."""
    f = float(state.last_funding_rate or 0.0)
    raw = _clip(f / max(extreme, 1e-9))
    return _clip(-raw if action == "fade" else raw)


# ── Sentiment / news indicators ───────────────────────────────────────────


@register("news_pulse")
def news_pulse(state: MarketState, window_s: float = 1800.0,
               tag: str | None = None, reference: float = 0.8,
               action: str = "follow", **_: Any) -> float:
    """Signed, severity-weighted news score over the window, optionally
    filtered to items carrying `tag` (e.g. 'regulation', 'influencer').
    Follow = ride the headline; fade = contrarian."""
    score = state.news_score(window_s, tag=tag) if tag else state.news_score(window_s)
    raw = _clip(score / max(reference, 0.1))
    return _clip(-raw if action == "fade" else raw)


@register("social_pulse")
def social_pulse(state: MarketState, window_s: float = 900.0,
                 reference: float = 2.0, action: str = "fade", **_: Any) -> float:
    """Reddit social score, normalized. Default fade (retail extremes reverse)."""
    raw = _clip(state.social_score(window_s) / max(reference, 0.1))
    return _clip(-raw if action == "fade" else raw)


@register("fgi_bias")
def fgi_bias(state: MarketState, action: str = "fade", **_: Any) -> float:
    """Fear & Greed Index as bias in [-1,+1]. Default fade (peak greed → top)."""
    raw = _clip(state.fgi_bias())
    return _clip(-raw if action == "fade" else raw)


# ── Blending ──────────────────────────────────────────────────────────────


def evaluate(state: MarketState, indicator_specs: dict[str, dict]) -> tuple[float, dict[str, float]]:
    """Compute the blended signal for a set of indicators.

    `indicator_specs` is `{key: params}` where `params` may include a
    `weight` key (default 1.0) used for weighted averaging. The key is
    normally an indicator name from the registry, but aliases are allowed
    by appending `:<suffix>` — e.g. `news_pulse:macro` and
    `news_pulse:regulation` both resolve to `news_pulse` with different
    params. This lets a tribe watch the same indicator twice with
    different windows or tags.

    Unknown indicator names are skipped silently (forward-compat).
    Returns (blended_signal in [-1,+1], per-indicator values).
    """
    if not indicator_specs:
        return 0.0, {}
    values: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for key, params in indicator_specs.items():
        resolved = key.split(":", 1)[0]
        fn = _REGISTRY.get(resolved)
        if fn is None:
            continue
        kwargs = {k: v for k, v in params.items() if k != "weight"}
        val = fn(state, **kwargs)
        w = float(params.get("weight", 1.0))
        values[key] = val
        weighted_sum += val * w
        weight_total += abs(w)
    if weight_total <= 0:
        return 0.0, values
    return _clip(weighted_sum / weight_total), values
