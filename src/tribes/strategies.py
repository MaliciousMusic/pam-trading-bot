"""Strategy function registry.

Each tribe's `strategy.family` names a function here. The function
signature is `(tribe, ts, state) -> Vote`. Add a new strategy by
decorating with `@register("name")`.

Design notes:
- Strategies are the ONLY place hardware/software limits get enforced
  via numeric constants (reaction delays, window sizes). If a tribe's
  profile says "phone + 10min eye-on-price", its strategy reflects that
  — not the spec's `attention` block, which is documentation until we
  graduate it to code.
- Strategies must be pure functions of (tribe.spec.strategy.params,
  ts, state). No hidden globals. This keeps backtests and the chess
  projector deterministic.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.types import Side, Vote
from .market_state import MarketState

if TYPE_CHECKING:
    from .tribe import Tribe


StrategyFn = Callable[["Tribe", datetime, MarketState], Vote]

_REGISTRY: dict[str, StrategyFn] = {}


def register(name: str) -> Callable[[StrategyFn], StrategyFn]:
    def wrap(fn: StrategyFn) -> StrategyFn:
        if name in _REGISTRY:
            raise ValueError(f"strategy already registered: {name}")
        _REGISTRY[name] = fn
        return fn
    return wrap


def get(name: str) -> StrategyFn:
    try:
        return _REGISTRY[name]
    except KeyError as e:
        raise KeyError(f"unknown strategy family: {name!r}") from e


def available() -> list[str]:
    return sorted(_REGISTRY)


# -- helpers --------------------------------------------------------------


def _abstain(tribe: "Tribe", reason: str = "") -> Vote:
    return Vote(
        tribe_id=tribe.spec.id,
        side=Side.ABSTAIN,
        conviction=0.0,
        horizon_seconds=0,
        weight=tribe.weight,
        reason=reason,
    )


def _vote(tribe: "Tribe", side: Side, conviction: float, horizon_seconds: int, reason: str = "") -> Vote:
    return Vote(
        tribe_id=tribe.spec.id,
        side=side,
        conviction=max(0.0, min(1.0, conviction)),
        horizon_seconds=horizon_seconds,
        weight=tribe.weight,
        reason=reason,
    )


# -- strategies -----------------------------------------------------------


@register("app_fomo")
def app_fomo(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Phone-app newcomers who watch price number go up/down and chase.
    Long window, late reaction, modest conviction.
    """
    p = tribe.spec.strategy.params
    win = float(p.get("price_window_seconds", 600))
    min_bps = float(p.get("min_move_bps", 60))
    move = state.price_change_bps(win)
    if abs(move) < min_bps:
        return _abstain(tribe, "no clear move")
    side = Side.BUY if move > 0 else Side.SELL
    conviction = min(1.0, abs(move) / (min_bps * 3.0)) * float(p.get("conviction_scale", 0.6))
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 3600)),
                 reason=f"fomo move {move:.0f}bps")


@register("momentum_chaser")
def momentum_chaser(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Leveraged degens piling onto print flow, amplified by liquidation cascades."""
    p = tribe.spec.strategy.params
    win = float(p.get("flow_window_seconds", 60))
    min_flow = float(p.get("min_net_flow_usd", 300_000))
    liq_amp = float(p.get("liq_amplifier", 1.5))
    net_flow = state.taker_flow_usd(win)
    liq_net = state.long_liq_usd(win) - state.short_liq_usd(win)   # +ve => long liqs => sell pressure
    signal = net_flow - liq_net * liq_amp
    if abs(signal) < min_flow:
        return _abstain(tribe, "flow below threshold")
    side = Side.BUY if signal > 0 else Side.SELL
    conviction = min(1.0, abs(signal) / (min_flow * 4.0))
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 120)),
                 reason=f"momentum signal {signal:.0f}")


@register("breakout_trader")
def breakout_trader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """TA day-traders waiting for a clean break of recent range."""
    p = tribe.spec.strategy.params
    win = float(p.get("range_window_seconds", 300))
    buf_bps = float(p.get("break_buffer_bps", 5))
    rng = state.price_range(win)
    if rng is None or state.last_price is None:
        return _abstain(tribe, "no price data")
    lo, hi = rng
    price = state.last_price
    up = hi * (1 + buf_bps / 10_000.0)
    dn = lo * (1 - buf_bps / 10_000.0)
    if price >= up:
        return _vote(tribe, Side.BUY, 0.7, int(p.get("hold_seconds", 180)),
                     reason=f"break above {hi:.2f}")
    if price <= dn:
        return _vote(tribe, Side.SELL, 0.7, int(p.get("hold_seconds", 180)),
                     reason=f"break below {lo:.2f}")
    return _abstain(tribe, "inside range")


@register("trend_follower")
def trend_follower(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Slow swing capital — only follows persistent multi-tick moves, passes
    when funding is already extreme against the direction."""
    p = tribe.spec.strategy.params
    win = float(p.get("trend_window_seconds", 300))
    min_bps = float(p.get("min_persistent_move_bps", 80))
    funding_guard = float(p.get("funding_guard", 0.001))
    move = state.price_change_bps(win)
    if abs(move) < min_bps:
        return _abstain(tribe, "no persistent trend")
    side = Side.BUY if move > 0 else Side.SELL
    f = state.last_funding_rate
    if (side is Side.BUY and f > funding_guard) or (side is Side.SELL and f < -funding_guard):
        return _abstain(tribe, "funding already extreme against continuation")
    conviction = min(1.0, abs(move) / (min_bps * 2.5))
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 3600)),
                 reason=f"trend {move:.0f}bps f={f:.5f}")


@register("mean_reverter_mm")
def mean_reverter_mm(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Market makers happily fade MILD imbalance; on heavily one-sided flow
    they PULL quotes (abstain) because inventory risk dominates."""
    p = tribe.spec.strategy.params
    win = float(p.get("flow_window_seconds", 30))
    max_imb = float(p.get("max_imbalance_usd", 1_500_000))
    net = state.taker_flow_usd(win)
    abs_n = abs(net)
    if abs_n < max_imb * 0.2:
        return _abstain(tribe, "balanced flow")
    if abs_n > max_imb:
        return _abstain(tribe, "flow too one-sided, pulling quotes")
    side = Side.SELL if net > 0 else Side.BUY
    conviction = min(0.5, abs_n / max_imb)
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 60)),
                 reason=f"mm fade net={net:.0f}")


@register("liquidation_hunter")
def liquidation_hunter(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Pros who position FOR continuation after large liquidation prints."""
    p = tribe.spec.strategy.params
    win = float(p.get("liq_window_seconds", 60))
    min_liq = float(p.get("min_liq_usd", 500_000))
    longs = state.long_liq_usd(win)
    shorts = state.short_liq_usd(win)
    net = longs - shorts
    if abs(net) < min_liq:
        return _abstain(tribe, "no meaningful liq cluster")
    side = Side.SELL if net > 0 else Side.BUY  # long liqs → keep shorting
    conviction = min(1.0, abs(net) / (min_liq * 5.0))
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 120)),
                 reason=f"liq cascade {net:.0f}")


@register("funding_fader")
def funding_fader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Contrarians fading extreme funding rates (crowded-side tax)."""
    p = tribe.spec.strategy.params
    extreme = float(p.get("extreme_funding_rate", 0.0005))
    f = state.last_funding_rate
    if abs(f) < extreme:
        return _abstain(tribe, "funding normal")
    side = Side.SELL if f > 0 else Side.BUY
    conviction = min(1.0, abs(f) / (extreme * 4.0))
    return _vote(tribe, side, conviction, horizon_seconds=int(p.get("hold_seconds", 1800)),
                 reason=f"fade funding {f:.5f}")


@register("news_reactor")
def news_reactor(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Placeholder: will consume `kind='news'` events once that source is wired.
    Until then the correct behavior is to abstain — the tribe's attention surface
    simply isn't receiving its input."""
    return _abstain(tribe, "news source not yet wired")
