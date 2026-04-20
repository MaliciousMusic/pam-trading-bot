"""Strategy function registry.

Each tribe's `strategy.family` names a function here. The function
signature is `(tribe, ts, state) -> Vote`. Add a new strategy by
decorating with `@register("name")`.

### Always-leaning contract (v2)

Every archetype has a posture at every instant T, even when their
signal is quiet. Strategies therefore always return a signed lean
(Side.BUY or Side.SELL with conviction ∈ (0, 1]) except when their
**input channel is genuinely offline** (e.g. no price yet, no news
feed wired). Only then is `Side.ABSTAIN` correct.

- Strong signal → return a high-conviction BUY/SELL with a `reason`.
- Weak/neutral data → fall back to the tribe's idle posture (either
  the spec's `disposition.idle_bias/idle_conviction`, or a
  data-dependent idle computed by the strategy — e.g. trend-follower
  idles in the direction of the longer-term trend).

Design notes:
- Strategies are the ONLY place hardware/software limits get enforced
  via numeric constants (reaction delays, window sizes).
- Strategies must be pure functions of (tribe.spec.strategy.params,
  ts, state). No hidden globals. This keeps backtests and the chess
  projector deterministic.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone
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


def _offline(tribe: "Tribe", reason: str = "") -> Vote:
    """True abstention — the tribe's data surface is unavailable."""
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


def _lean(tribe: "Tribe", signed: float, *, reason: str,
          horizon_seconds: int | None = None, cap: float = 1.0) -> Vote:
    """Convert a signed scalar [-1,+1] into a Vote. Near-zero signed collapses
    to the tribe's idle disposition (static). `cap` lets callers constrain
    conviction."""
    signed = max(-1.0, min(1.0, signed))
    if abs(signed) < 1e-6:
        d = tribe.spec.disposition
        signed = float(d.idle_bias)
        conv = float(d.idle_conviction) * abs(signed) if signed else 0.0
        if signed == 0.0:
            return _offline(tribe, reason + " (flat idle)")
        side = Side.BUY if signed > 0 else Side.SELL
        return _vote(tribe, side, min(cap, conv),
                     horizon_seconds or int(tribe.spec.strategy.params.get("hold_seconds", 300)),
                     reason=f"idle {reason}")
    side = Side.BUY if signed > 0 else Side.SELL
    return _vote(tribe, side, min(cap, abs(signed)),
                 horizon_seconds or int(tribe.spec.strategy.params.get("hold_seconds", 300)),
                 reason=reason)


# -- strategies -----------------------------------------------------------


@register("app_fomo")
def app_fomo(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Phone-app newcomers. Chase big moves when they see one; default
    to a HODLer long lean when nothing's happening."""
    p = tribe.spec.strategy.params
    win = float(p.get("price_window_seconds", 600))
    min_bps = float(p.get("min_move_bps", 60))
    move = state.price_change_bps(win)

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    if abs(move) >= min_bps:
        signed = move / (min_bps * 3.0) * float(p.get("conviction_scale", 0.6))
        return _lean(tribe, signed, reason=f"fomo move {move:.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 3600)))

    # Idle posture — HODLer lean (spec sets bias; most are slight long).
    return _lean(tribe, 0.0, reason="no clear move",
                 horizon_seconds=int(p.get("hold_seconds", 3600)))


@register("momentum_chaser")
def momentum_chaser(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Leveraged degens piling onto print flow, amplified by liquidations.
    Idle: weak lean in the direction of the tiny residual flow."""
    p = tribe.spec.strategy.params
    win = float(p.get("flow_window_seconds", 60))
    min_flow = float(p.get("min_net_flow_usd", 300_000))
    liq_amp = float(p.get("liq_amplifier", 1.5))
    net_flow = state.taker_flow_usd(win)
    liq_net = state.long_liq_usd(win) - state.short_liq_usd(win)
    signal = net_flow - liq_net * liq_amp

    if abs(signal) >= min_flow:
        signed = signal / (min_flow * 4.0)
        return _lean(tribe, signed, reason=f"momentum signal {signal:.0f}",
                     horizon_seconds=int(p.get("hold_seconds", 120)))

    # Idle: whisper in the direction of whatever residual flow exists.
    whisper = (signal / min_flow) * 0.3 if min_flow > 0 else 0.0
    return _lean(tribe, whisper, reason=f"flow quiet {signal:.0f}",
                 horizon_seconds=int(p.get("hold_seconds", 120)))


@register("breakout_trader")
def breakout_trader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """TA day-traders. Strong break → high conviction; inside-range →
    weak lean toward whichever edge the price is nearer (buyers vs sellers
    about to break)."""
    p = tribe.spec.strategy.params
    win = float(p.get("range_window_seconds", 300))
    buf_bps = float(p.get("break_buffer_bps", 5))
    rng = state.price_range(win)
    price = state.last_price
    if rng is None or price is None:
        return _offline(tribe, "no price data")

    lo, hi = rng
    up = hi * (1 + buf_bps / 10_000.0)
    dn = lo * (1 - buf_bps / 10_000.0)
    if price >= up:
        return _vote(tribe, Side.BUY, 0.75, int(p.get("hold_seconds", 180)),
                     reason=f"break above {hi:.2f}")
    if price <= dn:
        return _vote(tribe, Side.SELL, 0.75, int(p.get("hold_seconds", 180)),
                     reason=f"break below {lo:.2f}")

    # Inside range — lean toward the closer edge (anticipating the break).
    span = hi - lo
    if span <= 0:
        return _lean(tribe, 0.0, reason="flat range", horizon_seconds=int(p.get("hold_seconds", 180)))
    mid = (hi + lo) / 2.0
    signed = (price - mid) / (span / 2.0) * 0.2     # weak directional lean, max 0.2
    return _lean(tribe, signed, reason=f"inside range, near {'high' if signed>0 else 'low'}",
                 horizon_seconds=int(p.get("hold_seconds", 180)))


@register("trend_follower")
def trend_follower(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Slow swing capital. Trades the persistent move, not the tick.

    Regulation regime override: a fresh SEC/Fed/CFTC item materially shifts
    the setup — even if price hasn't moved yet, swing traders DO reposition
    after a regulatory headline (2021 China ban, 2024 ETF approval). We
    blend `regulation_pulse` into the signal so the tribe anticipates the
    next leg rather than waiting for price to confirm.

    HTF confirmation: the 1h trend score from the HTF kline poller is
    blended in as a regime-alignment signal. When the intraday move agrees
    with the 1h regime, the tribe scales up; when they disagree, conviction
    is muted (classic swing discipline — don't fight the higher timeframe).

    Idle: sign of the longer-term drift, with weak conviction."""
    p = tribe.spec.strategy.params
    win = float(p.get("trend_window_seconds", 300))
    long_win = float(p.get("long_trend_window_seconds", 1800))
    min_bps = float(p.get("min_persistent_move_bps", 80))
    funding_guard = float(p.get("funding_guard", 0.001))
    reg_weight = float(p.get("regulation_weight", 0.5))
    htf_weight = float(p.get("htf_weight", 0.35))

    move = state.price_change_bps(win)
    f = state.last_funding_rate
    reg = state.regulation_pulse(3600.0)
    htf = float(getattr(state, "htf_trend_score", 0.0) or 0.0)

    if abs(move) >= min_bps:
        if (move > 0 and f > funding_guard) or (move < 0 and f < -funding_guard):
            long_move = state.price_change_bps(long_win)
            signed = (long_move / (min_bps * 4.0)) * 0.4 + reg * reg_weight * 0.3 + htf * htf_weight * 0.4
            return _lean(tribe, max(-1.0, min(1.0, signed)),
                         reason=f"funding blocks trend, reg={reg:+.2f} htf={htf:+.2f} f={f:.5f}",
                         horizon_seconds=int(p.get("hold_seconds", 3600)))
        # HTF alignment modulates trending signal: agreement amplifies (×1.15),
        # disagreement mutes (×0.6). htf=0 (no data) is neutral pass-through.
        aligned = 1.0 if htf == 0.0 else (1.15 if move * htf > 0 else 0.6)
        signed = (move / (min_bps * 2.5)) * aligned + reg * reg_weight * 0.2 + htf * htf_weight * 0.3
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"trend {move:.0f}bps reg={reg:+.2f} htf={htf:+.2f}×{aligned:.2f} f={f:.5f}",
                     horizon_seconds=int(p.get("hold_seconds", 3600)))

    # Idle: longer-window drift + regulation pulse + HTF regime (even
    # without acute price movement, a fresh regulatory signal or a clear
    # 1h trend is enough to take a position).
    long_move = state.price_change_bps(long_win)
    signed = (long_move / (min_bps * 3.0)) * 0.3 + reg * reg_weight * 0.4 + htf * htf_weight * 0.5
    return _lean(tribe, max(-1.0, min(1.0, signed)),
                 reason=f"quiet; drift={long_move:+.0f}bps reg={reg:+.2f} htf={htf:+.2f}",
                 horizon_seconds=int(p.get("hold_seconds", 3600)))


@register("mean_reverter_mm")
def mean_reverter_mm(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """MMs fade mild imbalance, pull quotes on heavy flow (structural lean = 0).
    Idle posture is very close to neutral by design — they hedge everything."""
    p = tribe.spec.strategy.params
    win = float(p.get("flow_window_seconds", 30))
    max_imb = float(p.get("max_imbalance_usd", 1_500_000))
    net = state.taker_flow_usd(win)
    abs_n = abs(net)

    if abs_n > max_imb:
        # Pulling quotes — truly unwilling to take a side.
        return _offline(tribe, "flow too one-sided, pulling quotes")

    if abs_n >= max_imb * 0.2:
        signed = (-net / max_imb) * 0.5           # fade with capped conviction
        return _lean(tribe, signed, reason=f"mm fade net={net:.0f}",
                     horizon_seconds=int(p.get("hold_seconds", 60)))

    # Idle — whisper against the tiny imbalance.
    signed = (-net / max(max_imb * 0.2, 1.0)) * 0.1
    return _lean(tribe, signed, reason=f"balanced flow {net:+.0f}",
                 horizon_seconds=int(p.get("hold_seconds", 60)))


@register("liquidation_hunter")
def liquidation_hunter(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Pros who position FOR continuation after large liquidation prints.
    Idle: dormant, very small lean toward recent liq direction."""
    p = tribe.spec.strategy.params
    win = float(p.get("liq_window_seconds", 60))
    min_liq = float(p.get("min_liq_usd", 500_000))
    longs = state.long_liq_usd(win)
    shorts = state.short_liq_usd(win)
    net = longs - shorts

    if abs(net) >= min_liq:
        # Long liqs → continuation is DOWN → short.
        signed = -net / (min_liq * 5.0)
        return _lean(tribe, signed, reason=f"liq cascade {net:.0f}",
                     horizon_seconds=int(p.get("hold_seconds", 120)))

    # Idle: weak residual lean from small liq imbalance.
    signed = -(net / max(min_liq, 1.0)) * 0.15
    return _lean(tribe, signed, reason=f"no meaningful liq cluster ({net:+.0f})",
                 horizon_seconds=int(p.get("hold_seconds", 120)))


@register("funding_fader")
def funding_fader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Contrarians fading extreme funding rates. Idle: fade the sign of
    whatever funding currently is, weakly — they always mildly dislike
    whichever side is paying to stay on."""
    p = tribe.spec.strategy.params
    extreme = float(p.get("extreme_funding_rate", 0.0005))
    f = state.last_funding_rate

    if abs(f) >= extreme:
        signed = -f / (extreme * 4.0)     # fade, scale by how extreme
        return _lean(tribe, signed, reason=f"fade funding {f:.5f}",
                     horizon_seconds=int(p.get("hold_seconds", 1800)))

    # Idle: weak fade of current funding sign.
    if extreme > 0:
        signed = -(f / extreme) * 0.3
    else:
        signed = 0.0
    return _lean(tribe, signed, reason=f"funding normal ({f:.5f})",
                 horizon_seconds=int(p.get("hold_seconds", 1800)))


@register("leverage_cycle")
def leverage_cycle(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Reads the open-interest cycle: expansions are "late pile-on" setups
    (fade the concurrent price trend), contractions are "squeeze/capitulate"
    setups (follow the concurrent price trend — the OI drain IS the
    unwind that's driving the move).

    Four quadrants (oi_delta_pct × price_change_bps):
      OI up, price up     → late longs piling in → fade (sell)
      OI up, price down   → late shorts piling in → fade (buy)
      OI down, price up   → short squeeze, capitulation buys → follow (buy)
      OI down, price down → long capitulation → follow (sell)

    Needs OI data from at least one venue. Idle: weak lean in the direction
    implied by the current quadrant with low conviction."""
    p = tribe.spec.strategy.params
    oi_window_s = float(p.get("oi_window_s", 3600.0))
    oi_ref_pct = float(p.get("oi_reference_pct", 3.0))
    price_window_s = float(p.get("price_window_s", 3600.0))
    price_ref_bps = float(p.get("price_reference_bps", 60.0))
    strong_thr = float(p.get("strong_activation", 0.8))

    if not state.oi:
        return _offline(tribe, "no OI data yet")

    oi_delta = state.oi_delta_pct(oi_window_s)
    move_bps = state.price_change_bps(price_window_s)

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    oi_signal = oi_delta / max(oi_ref_pct, 1e-6)
    price_signal = move_bps / max(price_ref_bps, 1.0)

    # Direction: fade when OI expanding (price_signal negated), follow when
    # OI draining (price_signal passed through).
    if oi_signal >= 0:
        # expansion quadrant — fade
        direction = -1.0
    else:
        # contraction quadrant — follow the unwind
        direction = 1.0

    # Magnitude requires BOTH axes to be meaningful.
    magnitude = min(abs(oi_signal), abs(price_signal))
    signed = direction * magnitude * (1.0 if price_signal > 0 else -1.0)
    # For the expansion quadrant, direction=-1 means: if price up, go down
    # (fade), if price down, go up (fade). That's exactly -sign(price) × mag.
    # For contraction, direction=+1 means: if price up, go up (follow), etc.
    # That's exactly +sign(price) × mag. The formula above collapses to
    # direction × sign(price) × mag — correct for both.

    if magnitude >= strong_thr:
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"leverage {'expansion-fade' if oi_signal>=0 else 'unwind-follow'} "
                            f"oi={oi_delta:+.2f}% move={move_bps:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 3600)))

    # Below the strong activation threshold — weak lean in the same
    # directional quadrant but with capped conviction.
    signed_weak = signed * 0.5
    return _lean(tribe, max(-1.0, min(1.0, signed_weak)),
                 reason=f"leverage cycle quiet oi={oi_delta:+.2f}% move={move_bps:+.0f}bps",
                 horizon_seconds=int(p.get("hold_seconds", 3600)), cap=0.4)


@register("options_hedger_proxy")
def options_hedger_proxy(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Proxy for options-dealer hedging flow. We don't have Deribit book
    data on the free tier, so this archetype SIMULATES the two dominant
    hedging patterns from what we CAN see:

      1. Volatility regime: when realized vol is calm (book stable, dealers
         net LONG gamma from retail call-buying), hedges FADE intraday
         moves — buy dips, sell rips. This is the dominant pattern in
         BTC perps during range regimes.

      2. Volatility regime flip: once realized vol exceeds a threshold
         (~60bps in a 5min window), the assumption is the market has
         forced dealers SHORT gamma — their hedges now AMPLIFY the move.

      3. Time-of-day weight: dealer-hedge flow is concentrated at the
         two daily rebalance windows — Asia open (UTC 23-02) and US open
         (UTC 13-15). Midday US and late Asia are quiet. We scale the
         effective conviction by a per-hour factor.

    This is explicitly a PROXY. It will misfire on days when the actual
    options book has a dealer position opposite what our vol-regime
    heuristic assumes. Treated as one voice in the council, not as
    truth."""
    p = tribe.spec.strategy.params
    move_window_s = float(p.get("move_window_s", 900.0))       # 15min
    move_ref_bps = float(p.get("move_reference_bps", 20.0))
    vol_window_s = float(p.get("vol_window_s", 300.0))
    flip_vol_bps = float(p.get("flip_vol_bps", 60.0))

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    move_bps = state.price_change_bps(move_window_s)
    rng = state.price_range(vol_window_s)
    if rng is None or rng[0] <= 0:
        return _lean(tribe, 0.0, reason="no vol data yet",
                     horizon_seconds=int(p.get("hold_seconds", 1800)))
    lo, hi = rng
    vol_bps = (hi - lo) / ((hi + lo) / 2.0) * 10_000.0

    # Active-window weight: Asia open + US open are prime hedge-flow
    # hours. Outside those, scale conviction down.
    hour = ts.hour
    if hour in (23, 0, 1, 2, 13, 14, 15):
        tod_weight = 1.0
    elif hour in (3, 4, 12, 16, 22):
        tod_weight = 0.7
    else:
        tod_weight = 0.4

    # Long-gamma regime: fade the move. Short-gamma regime: follow it.
    short_gamma = vol_bps >= flip_vol_bps
    raw = move_bps / max(move_ref_bps, 1.0)

    if short_gamma:
        signed = raw * 0.7 * tod_weight       # follow, muted — amplify the move
        tag = "short-gamma amplify"
    else:
        signed = -raw * 0.7 * tod_weight      # fade — long-gamma dampen
        tag = "long-gamma fade"

    return _lean(tribe, max(-1.0, min(1.0, signed)),
                 reason=f"{tag} move={move_bps:+.0f}bps vol={vol_bps:.0f}bps h={hour:02d}UTC tod_w={tod_weight:.1f}",
                 horizon_seconds=int(p.get("hold_seconds", 1800)))


@register("cross_venue_arb")
def cross_venue_arb(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Spread traders watching Binance vs Bybit/OKX taker aggression.

    When Binance's flow runs materially hotter than the external-venue
    average — say Binance is heavy-buying while Bybit/OKX are balanced or
    selling — the move is being *led* by one venue. Two things then tend
    to happen: either (a) external venues catch up (Binance was right,
    trade continues) or (b) Binance reverts (it was the only buyer, price
    rolls back). Empirically, the SHORT-horizon outcome is predominantly
    (b): divergence at the print typically reverts over the next 30-120s
    as spreads close. That's the trade.

    Fade short-term divergence, confirmed by longer-term divergence being
    same-sign (persistent one-venue dominance, not just a single aggressive
    print).

    Idle: weak fade of current divergence."""
    p = tribe.spec.strategy.params
    short_w = float(p.get("short_window_s", 60.0))
    long_w = float(p.get("long_window_s", 300.0))
    strong_thr = float(p.get("strong_divergence", 0.35))
    confirm_thr = float(p.get("confirm_divergence", 0.15))

    # cross_venue_divergence returns 0.0 when no cross-venue data — the
    # strategy should treat that as offline (nothing to arb).
    if not state.cross_flow:
        return _offline(tribe, "no cross-venue flow yet")

    short_div = state.cross_venue_divergence(short_w)
    long_div = state.cross_venue_divergence(long_w)

    # Strong + confirmed → full fade.
    if abs(short_div) >= strong_thr and short_div * long_div > 0 and abs(long_div) >= confirm_thr:
        signed = -short_div / strong_thr * 0.9
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"fade venue divergence short={short_div:+.2f} long={long_div:+.2f}",
                     horizon_seconds=int(p.get("hold_seconds", 300)))

    # Strong short-term but NOT confirmed on longer window → weaker fade
    # (it may be a fleeting one-print event).
    if abs(short_div) >= strong_thr:
        signed = -short_div / strong_thr * 0.5
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"one-shot venue divergence {short_div:+.2f}",
                     horizon_seconds=int(p.get("hold_seconds", 300)), cap=0.6)

    # Idle — weak fade of short-window divergence. Even when below the
    # strong threshold, a spread trader always holds a residual lean.
    signed = -short_div * 0.4
    return _lean(tribe, signed,
                 reason=f"venue flow balanced ({short_div:+.2f})",
                 horizon_seconds=int(p.get("hold_seconds", 300)))


@register("basis_arb")
def basis_arb(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Cash-and-carry structural arb. Reads three channels that funding_fader
    ignores:

      1. *Annualized* basis (rate × 3 × 365) rather than the raw 8h print —
         a 1bps epoch and a 3bps epoch feel the same to funding_fader but
         are 11% vs 33% annualized to a basis trader.
      2. Perp/mark premium in bps — direct measure of same-side wedge.
      3. OI-delta over the last hour — is leverage BUILDING into this
         basis, or unwinding out of it?

    The archetype fades persistent rich basis (short perp / long spot), and
    fades it HARDER when OI is expanding on the same side (crowded trade
    about to unwind). Slow horizons (4-24h) — this is structural capital,
    not scalp.

    Idle: weak fade of whatever residual basis currently is."""
    p = tribe.spec.strategy.params
    rich_annualized_pct = float(p.get("rich_annualized_pct", 15.0))
    oi_window_s = float(p.get("oi_window_s", 3600.0))
    oi_ref_pct = float(p.get("oi_reference_pct", 2.0))
    premium_ref_bps = float(p.get("premium_reference_bps", 10.0))

    f = float(state.last_funding_rate or 0.0)
    annualized_pct = f * 3.0 * 365.0 * 100.0

    if state.last_price is not None and state.last_mark_price and state.last_mark_price > 0:
        premium_bps = (state.last_price - state.last_mark_price) / state.last_mark_price * 10_000.0
    else:
        premium_bps = 0.0

    oi_delta = state.oi_delta_pct(oi_window_s)

    fund_signal = annualized_pct / max(rich_annualized_pct, 1e-6)
    prem_signal = premium_bps / max(premium_ref_bps, 1e-6)
    oi_signal = oi_delta / max(oi_ref_pct, 1e-6)

    if abs(fund_signal) >= 1.0:
        # Basis rich. Boost conviction when OI is expanding on the paid side
        # (crowded carry → deeper unwind when it snaps).
        boost = 1.0 + min(max(oi_signal * (1.0 if fund_signal > 0 else -1.0), 0.0), 2.0) * 0.4
        signed = -((fund_signal + prem_signal * 0.3) / 2.0) * boost
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"basis rich ann={annualized_pct:+.1f}% prem={premium_bps:+.0f}bps oi={oi_delta:+.2f}%",
                     horizon_seconds=int(p.get("hold_seconds", 14400)))

    if abs(prem_signal) >= 1.0 and fund_signal * prem_signal > 0:
        # Premium alone is extreme, funding co-signs — moderate fade.
        signed = -(prem_signal + fund_signal) / 2.5
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"premium wedge ann={annualized_pct:+.1f}% prem={premium_bps:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 14400)))

    # Idle: weak fade of residual basis. Still structural — these guys
    # always have a slight short-carry bias when funding is positive.
    signed = -(fund_signal * 0.35 + prem_signal * 0.15)
    return _lean(tribe, max(-1.0, min(1.0, signed)),
                 reason=f"basis quiet ann={annualized_pct:+.1f}% prem={premium_bps:+.0f}bps",
                 horizon_seconds=int(p.get("hold_seconds", 14400)))


@register("grid_bot")
def grid_bot(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Retail auto-bots (3Commas, Pionex, Binance grid). Place a ladder of
    limit orders around a mid; every micro-move triggers a passive fade.
    They buy dips, sell rips, mechanically, forever. Less sophisticated
    than an MM — no quote-pulling on toxic flow — so under sustained
    one-way flow they bleed inventory rather than step aside.

    Idle: constant weak mean-reversion to recent average price."""
    p = tribe.spec.strategy.params
    win = float(p.get("grid_window_seconds", 120))
    band_bps = float(p.get("grid_band_bps", 15))    # half-span of the grid
    move = state.price_change_bps(win)

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    # Price dislocation from recent mid → fade it. More extreme = stronger fade,
    # but a grid bot never conviction-yells; cap at 0.45.
    if abs(move) >= band_bps:
        signed = -move / (band_bps * 3.0)
        return _lean(tribe, signed, reason=f"grid fade {move:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 300)), cap=0.45)

    # Idle: mechanical micro-fade of whatever drift exists.
    signed = -(move / max(band_bps, 1.0)) * 0.25
    return _lean(tribe, signed, reason=f"grid idle ({move:+.1f}bps)",
                 horizon_seconds=int(p.get("hold_seconds", 300)))


@register("copy_trader")
def copy_trader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Retail auto-bots mirroring 'lead traders'. Influencer feeds
    (substack/Twitter via Nitter) are the PRIMARY trigger — that's the
    literal mechanism copy-traders follow. Reddit social pulse is the
    secondary ambient herd signal. Price confirmation must align.

    Idle: herd lean in the direction of the broader short-window drift —
    copy-traders are inherently trend-chasers, even with no signal."""
    p = tribe.spec.strategy.params
    inf_win = float(p.get("influencer_window_s", 1800))
    inf_trigger = float(p.get("influencer_trigger", 0.4))
    sw = float(p.get("social_window_s", 600))
    pw = float(p.get("confirm_window_s", 120))
    min_vol = int(p.get("min_social_volume", 2))
    trigger = float(p.get("min_social_score", 1.2))

    confirm = state.price_change_bps(pw)
    if state.last_price is None:
        return _offline(tribe, "no price yet")

    # Influencer-led: substack/Twitter post + same-direction price nudge.
    # Cap is intentionally lower than lead-moving tribes (mev/liq hunter): by
    # the time a copy-trader sees the signal, the lead has already priced it
    # in. This is a chasing strategy, not a first-mover strategy — higher
    # caps would model copy-traders as informed leaders, which they aren't.
    inf = state.influencer_pulse(inf_win)
    inf_n = state.news_volume(inf_win, tag="influencer")
    if inf_n >= 1 and abs(inf) >= inf_trigger and (inf * confirm) >= 0:
        signed = inf / (inf_trigger * 1.8)
        return _lean(tribe, signed,
                     reason=f"copy influencer {inf:+.2f} (n={inf_n}) conf={confirm:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 900)), cap=0.55)

    # Reddit herd + price confirmation.
    soc = state.social_score(sw)
    vol = state.social_volume(sw)
    if vol >= min_vol and abs(soc) >= trigger and (soc * confirm) > 0:
        signed = (soc / (trigger * 2.0))
        return _lean(tribe, signed, reason=f"copy signal {soc:+.1f} conf={confirm:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 900)), cap=0.50)

    # Idle: herd lean in the direction of short-window drift, with a gentle
    # pull from residual influencer sentiment (even flat drift, people copy
    # the loudest voice they saw this morning).
    drift_signal = (confirm / max(float(p.get("herd_drift_bps", 40)), 1.0)) * 0.35
    signed = max(-1.0, min(1.0, drift_signal + inf * 0.25))
    return _lean(tribe, signed,
                 reason=f"herd drift {confirm:+.0f}bps inf={inf:+.2f}",
                 horizon_seconds=int(p.get("hold_seconds", 900)))


@register("news_reactor")
def news_reactor(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Fades sentiment overreactions. Input priority (high → low):

      1. Regulation/macro news — SEC/Fed/CFTC items, severity-weighted. These
         don't get faded; they get FOLLOWED (a real Fed rate cut is not noise).
      2. Headline/influencer news — crypto press + Twitter/Substack talkers.
         This IS typically overreaction; fade when the crowd yells.
      3. Reddit social pulse — slower, noisier. Fade at extremes.
      4. FGI fallback — daily sentiment regime, weakly faded.

    Idle (channels quiet): weak fade of whatever residual sentiment exists."""
    p = tribe.spec.strategy.params
    news_win = float(p.get("news_window_s", 1800))
    headline_trigger = float(p.get("headline_trigger", 0.6))
    regulation_trigger = float(p.get("regulation_trigger", 0.5))
    soc_win = float(p.get("social_window_s", 900))
    min_vol = int(p.get("min_social_volume", 3))
    extreme_soc = float(p.get("extreme_social_score", 2.5))
    fgi_extreme = float(p.get("fgi_extreme", 0.5))

    reg = state.regulation_pulse(news_win)
    if abs(reg) >= regulation_trigger:
        # Regulation moves regime — follow, don't fade. Magnitude is small
        # because severity-weighted scores rarely exceed ~1.0 per item.
        signed = max(-1.0, min(1.0, reg * 1.5))
        return _lean(tribe, signed, reason=f"regulation pulse {reg:+.2f}",
                     horizon_seconds=int(p.get("hold_seconds", 900)))

    news_any = state.news_score(news_win)
    news_n = state.news_volume(news_win)
    if news_n >= 2 and abs(news_any) >= headline_trigger:
        # Headline overreaction — fade with capped conviction.
        signed = -news_any / (headline_trigger * 2.0)
        return _lean(tribe, signed,
                     reason=f"fade headlines {news_any:+.2f} (n={news_n})",
                     horizon_seconds=int(p.get("hold_seconds", 600)), cap=0.8)

    soc = state.social_score(soc_win)
    vol = state.social_volume(soc_win)
    fgi = state.fgi_bias()

    if vol == 0 and news_n == 0 and state.fgi_value is None:
        return _offline(tribe, "no sentiment data yet")

    if vol >= min_vol and abs(soc) >= extreme_soc:
        signed = -soc / (extreme_soc * 2.0)
        return _lean(tribe, signed, reason=f"fade social {soc:+.1f} (n={vol})",
                     horizon_seconds=int(p.get("hold_seconds", 300)))

    if abs(fgi) >= fgi_extreme:
        signed = -fgi
        return _lean(tribe, signed,
                     reason=f"fade FGI {state.fgi_value} ({state.fgi_classification})",
                     horizon_seconds=int(p.get("hold_seconds", 600)), cap=0.7)

    # Idle: weak fade of aggregated news + FGI sentiment.
    residual = (news_any * 0.3) + (fgi * 0.3)
    signed = -residual
    return _lean(tribe, signed,
                 reason=f"sentiment quiet (news={news_any:+.2f} fgi={state.fgi_value})",
                 horizon_seconds=int(p.get("hold_seconds", 600)))


@register("mev_frontrun")
def mev_frontrun(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """MEV / Front-Runner — competes with liquidation_hunter by acting first.

    We don't have mempool data on the free tier, so the free-tier proxy for
    "large pending pressure" is the intersection of three observable signals:

      1. Aggressive taker flow in the last `flow_window_seconds` — directional
         pressure that's already printing.
      2. Recent liquidation cascade in the opposite direction (longs getting
         flushed → continuation DOWN → we front-run by selling BEFORE the
         cascade wave has fully traveled).
      3. Book imbalance at the top of book — a thin side gets eaten next.

    Any one trigger enters; confluence of two gives max conviction. The
    position is tiny and short — this tribe pulls liquidity inside 30s and
    leaves, which is what `extraction_efficiency` and the short hold_seconds
    encode. `block_priority_fee` scales final conviction (willingness to pay
    for priority == willingness to take the trade).

    Idle: weak residual lean in the direction of whatever micro-pressure
    exists — they're never flat, always scoping.
    """
    p = tribe.spec.strategy.params
    flow_win = float(p.get("flow_window_seconds", 10))
    min_flow = float(p.get("min_net_flow_usd", 400_000))
    liq_win = float(p.get("liq_window_seconds", 30))
    min_liq = float(p.get("min_liq_usd", 400_000))
    imb_thr = float(p.get("imbalance_threshold", 0.60))
    priority = max(0.0, min(1.0, float(p.get("block_priority_fee", 0.7))))
    efficiency = max(0.0, min(1.0, float(p.get("extraction_efficiency", 0.6))))
    horizon = int(p.get("hold_seconds", 30))

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    net_flow = state.taker_flow_usd(flow_win)
    liq_net = state.long_liq_usd(liq_win) - state.short_liq_usd(liq_win)

    # Book imbalance — front-runs the side that's about to get eaten.
    bid_d = state.bid_depth_5bps_usd or 0.0
    ask_d = state.ask_depth_5bps_usd or 0.0
    imb_signed = 0.0
    if bid_d + ask_d > 0:
        imb = bid_d / (bid_d + ask_d)
        if imb >= imb_thr or imb <= (1.0 - imb_thr):
            # Heavy bid side → thin ask → price lifts into the thin ask.
            imb_signed = (imb - 0.5) / max(imb_thr - 0.5, 1e-6)

    # Score each signal to [-1, +1].
    flow_signed = (net_flow / max(min_flow, 1.0)) if min_flow > 0 else 0.0
    # Longs getting liquidated → continuation DOWN → we front-run by selling.
    liq_signed = -(liq_net / max(min_liq, 1.0)) if min_liq > 0 else 0.0

    triggers = [x for x in (flow_signed, liq_signed, imb_signed) if abs(x) >= 0.5]
    if triggers:
        # Average of triggered signals; confluence means bigger magnitudes compound.
        raw = sum(triggers) / len(triggers)
        # Priority fee + efficiency scale the extractor's willingness and
        # hit rate; two confluent triggers get amplified toward the cap.
        boost = 1.0 + 0.4 * (len(triggers) - 1)
        signed = max(-1.0, min(1.0, raw * boost * efficiency * (0.5 + 0.5 * priority)))
        tags = []
        if abs(flow_signed) >= 0.5: tags.append(f"flow={net_flow:+,.0f}")
        if abs(liq_signed) >= 0.5: tags.append(f"liq={liq_net:+,.0f}")
        if abs(imb_signed) >= 0.5: tags.append(f"imb={imb_signed:+.2f}")
        return _lean(tribe, signed,
                     reason=f"mev frontrun [{', '.join(tags)}] eff={efficiency:.2f} prio={priority:.2f}",
                     horizon_seconds=horizon)

    # Idle — residual lean scaled by efficiency (the hit-rate) on the strongest
    # sub-threshold signal. Never loud; never silent.
    residual = max((flow_signed, liq_signed, imb_signed), key=abs)
    signed = max(-1.0, min(1.0, residual * 0.20 * efficiency))
    return _lean(tribe, signed,
                 reason=f"mev scoping flow={net_flow:+,.0f} liq={liq_net:+,.0f} imb={imb_signed:+.2f}",
                 horizon_seconds=horizon)


@register("twap_whale")
def twap_whale(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Passive Whale / ETF TWAP — price-insensitive slicing over the session.

    ETF market-makers and treasury desks run large orders as TWAPs: equal-
    weight slices across a time window, executed regardless of where the
    tape is. The whale's direction for the day is fixed by whatever capital
    flow arrived that morning — our free-tier proxy is the multi-day price
    drift (persistent inflows tend to track persistent uptrends and vice
    versa). Conviction is steady and only weakly tied to the tape.

    Two knobs encode the inflexibility:
      - `twap_remainder`: fraction of the planned slice window still to be
        executed. Decays linearly across `session_duration_s` anchored on
        `session_start_utc_hour`. Outside the session window the whale is
        silent (remainder = 0).
      - `execution_urgency`: scales conviction. A lazy VWAP runs at 0.3;
        an aggressive ETF close-auction ramp runs at 0.9.

    Key property: this tribe does NOT respond to short-term price or flow.
    It deliberately carries a constant per-tick pressure so the council
    has a voice representing "the steady buyer who's going to keep
    pressing buttons regardless of what the chart did in the last 60s."

    Idle: silent outside session window (returns ABSTAIN via _offline).
    """
    p = tribe.spec.strategy.params
    session_start = int(p.get("session_start_utc_hour", 13))     # US cash open
    session_duration = float(p.get("session_duration_s", 6 * 3600.0))  # 6h TWAP
    drift_window_s = float(p.get("drift_window_s", 86400.0))     # 24h direction read
    drift_ref_bps = float(p.get("drift_reference_bps", 300.0))
    urgency = max(0.0, min(1.0, float(p.get("execution_urgency", 0.5))))
    horizon = int(p.get("hold_seconds", 1800))

    # Where are we in the session?
    hour = ts.astimezone(timezone.utc).hour
    minute = ts.astimezone(timezone.utc).minute
    hours_since_open = ((hour - session_start) % 24) + minute / 60.0
    session_hours = session_duration / 3600.0
    if hours_since_open > session_hours:
        # Outside the programmed window — whale is idle.
        return _offline(tribe, f"outside TWAP window (h={hour:02d}UTC, since_open={hours_since_open:.1f}h)")

    twap_remainder = max(0.0, 1.0 - hours_since_open / max(session_hours, 1e-6))

    # Direction read: multi-day drift polarity. Zero drift → abstain (no
    # directional information, they don't invent a trade). Otherwise the
    # sign sticks for the entire session.
    drift_bps = state.price_change_bps(drift_window_s)
    if abs(drift_bps) < drift_ref_bps * 0.25:
        # Schedule is running but there's no persistent direction to slice
        # into — rare, but correct behavior (a true ETF order wouldn't exist
        # without a net inflow to deploy).
        return _lean(tribe, 0.0, reason=f"TWAP armed but no drift (d={drift_bps:+.0f}bps)",
                     horizon_seconds=horizon)

    signed_dir = 1.0 if drift_bps > 0 else -1.0
    # Conviction = urgency × remainder — high early-session and decays to
    # zero as the slice window closes. Capped; whales are never loud per-tick.
    conv = max(0.0, min(0.55, urgency * twap_remainder * (0.5 + 0.5 * min(1.0, abs(drift_bps) / drift_ref_bps))))
    return _vote(tribe, Side.BUY if signed_dir > 0 else Side.SELL, conv, horizon,
                 reason=(f"TWAP slice urg={urgency:.2f} rem={twap_remainder:.2f} "
                         f"drift={drift_bps:+.0f}bps"))


@register("microstructure_prober")
def microstructure_prober(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Phase 12 — silent tribe.

    Reads the order-book microstructure that the rest of the council ignores:

      A. **L2 imbalance** — bid_5bps_usd / (bid+ask). Persistent skew >55% in
         either direction is a quiet directional bid; it precedes mid-price
         drift before any trade prints.
      B. **Queue-position decay** — % loss in same-side depth over the last
         15s. A side burning down fast (depth halving) means the resting
         orders are getting eaten — continuation in the *opposing* side's
         direction (because the eaten side WAS the resistance).
      C. **Iceberg detection** — last-window's max single-trade notional vs
         median trade notional. A single print >= 4× the median that's same-
         signed as net flow is an iceberg refill — follow.
      D. **Trade-size entropy** — Shannon-style entropy across 4 size buckets.
         Low entropy (one bucket dominates) = an institution algo running on
         a fixed slice; that's directional flow, follow it.

    Each input contributes a partial signed signal in [-1, +1]; the final
    vote is their cap-summed average. Orthogonal to every other tribe — none
    of them touch the depth series or per-trade notional distribution.

    Channels offline (no book data, no trades) → ABSTAIN.
    """
    p = tribe.spec.strategy.params
    win = float(p.get("imb_window_s", 15))
    decay_window = float(p.get("decay_window_s", 15))
    iceberg_window = float(p.get("iceberg_window_s", 30))
    iceberg_mult = float(p.get("iceberg_mult", 4.0))
    bias_threshold = float(p.get("imbalance_threshold", 0.55))
    horizon = int(p.get("hold_seconds", 90))

    bid_d = state.bid_depth_5bps_usd
    ask_d = state.ask_depth_5bps_usd
    if not bid_d or not ask_d or (bid_d + ask_d) <= 0:
        return _offline(tribe, "no L2 depth yet")

    contributions: list[tuple[float, str]] = []

    # A. L2 imbalance.
    imb = bid_d / (bid_d + ask_d)  # ∈ (0,1); 0.5 = balanced
    if imb >= bias_threshold or imb <= (1.0 - bias_threshold):
        # Map (1-T)..T → -1..+1.
        signed_imb = (imb - 0.5) / max(bias_threshold - 0.5, 1e-6)
        signed_imb = max(-1.0, min(1.0, signed_imb))
        contributions.append((signed_imb * 0.5,
                              f"L2 imb {imb:.2f}"))

    # B. Queue-position decay. Compare oldest vs newest depth in the window.
    def _decay(series, window_s: float) -> float | None:
        fl = series.first_last(window_s)
        if not fl:
            return None
        first, last = fl
        if first <= 0:
            return None
        return (last - first) / first  # negative when burning down
    bid_decay = _decay(state.bid_depth_series, decay_window)
    ask_decay = _decay(state.ask_depth_series, decay_window)
    if bid_decay is not None and ask_decay is not None:
        # Bid melting → bears are hitting it → follow down (negative side).
        # Ask melting → bulls are lifting it → follow up (positive).
        # Combine: ask_decay < bid_decay → bullish (ask is melting faster).
        diff = ask_decay - bid_decay  # in (-1, 1) typically; sign matters
        if abs(diff) > 0.10:  # at least 10pp differential
            contributions.append((max(-1.0, min(1.0, -diff * 2.0)),
                                  f"queue Δbid={bid_decay:+.0%} Δask={ask_decay:+.0%}"))

    # C. Iceberg detection.
    sizes = list(state.trade_sizes._window(iceberg_window))
    if len(sizes) >= 5:
        absvals = sorted(abs(v) for _, v in sizes)
        median = absvals[len(absvals) // 2]
        if median > 0:
            # Largest signed print this window.
            biggest_signed = max((v for _, v in sizes), key=lambda x: abs(x))
            if abs(biggest_signed) >= iceberg_mult * median:
                ice = max(-1.0, min(1.0, biggest_signed
                                    / (iceberg_mult * 5.0 * median)))
                contributions.append((ice * 0.5,
                                      f"iceberg {biggest_signed:+,.0f} ≥{iceberg_mult}× med"))

    # D. Trade-size entropy. Bucket |notional| into 4 quantile-style bands;
    # measure entropy. Lower entropy = one bucket dominates = algo running.
    if len(sizes) >= 8:
        absvals = [abs(v) for _, v in sizes]
        mx = max(absvals) or 1.0
        bins = [0, 0, 0, 0]
        for a in absvals:
            r = a / mx
            idx = min(3, int(r * 4))
            bins[idx] += 1
        n = sum(bins)
        ent = 0.0
        for c in bins:
            if c > 0:
                pp = c / n
                ent -= pp * math.log2(pp)
        # Max entropy with 4 equal buckets = 2.0 bits; low = one bucket has all.
        if ent < 1.2:
            net_dir = sum(v for _, v in sizes)
            if abs(net_dir) > 0:
                # Algo signal direction = sign of net_dir; magnitude scales by
                # how peaked the distribution is.
                peakiness = (1.2 - ent) / 1.2
                contributions.append((max(-1.0, min(1.0,
                                       (1.0 if net_dir > 0 else -1.0) * peakiness * 0.6)),
                                      f"size entropy {ent:.2f} net={net_dir:+,.0f}"))

    if not contributions:
        # Quiet microstructure — small lean from raw imbalance only.
        signed = (imb - 0.5) * 0.4  # max ±0.2
        return _lean(tribe, signed,
                     reason=f"micro quiet imb={imb:.2f}",
                     horizon_seconds=horizon)

    avg_signed = sum(s for s, _ in contributions) / len(contributions)
    avg_signed = max(-1.0, min(1.0, avg_signed))
    why = " | ".join(label for _, label in contributions)
    return _lean(tribe, avg_signed, reason=f"micro: {why}",
                 horizon_seconds=horizon)


@register("cta_systematic_trend")
def cta_systematic_trend(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Systematic CTA / managed-futures — 60/120d trend-following with vol-
    adjusted sizing. Reads the HTF regime bias (from the 1h kline poller)
    and only takes a position when regime + intraday continuation co-sign.
    Realized vol drives a sizing rail: low vol = full size, high vol =
    shrunk position.

    Unlike the intraday breakout/momentum tribes, the CTA cares about
    *persistence* — a brief spike in one direction against a bearish HTF
    regime is explicitly ignored. Mirrors the live behavior of Man AHL,
    Winton, AQR Managed Futures: slow, disciplined, regime-aligned.

    Idle: weak lean in the HTF-regime direction — CTAs are effectively
    always positioned, even when the intraday tape is quiet."""
    p = tribe.spec.strategy.params
    vol_window_s = float(p.get("vol_window_s", 840.0))
    vol_cap_bps = float(p.get("vol_cap_bps", 80.0))
    htf_threshold = float(p.get("htf_threshold", 0.30))
    cont_window_s = float(p.get("continuation_window_s", 1200.0))
    cont_ref_bps = float(p.get("continuation_ref_bps", 40.0))
    horizon = int(p.get("hold_seconds", 14400))

    if state.last_price is None:
        return _offline(tribe, "no price yet")

    htf = float(getattr(state, "htf_trend_score", 0.0) or 0.0)
    cont_bps = state.price_change_bps(cont_window_s)

    # Vol rail: realized-vol in bps → sizing multiplier in [0.2, 1.0].
    # Low vol → full size. Near/above vol_cap → shrink hard.
    vol_bps = state.realized_vol_bps(vol_window_s)
    if vol_bps is None:
        size_mult = 0.5
        vol_tag = "vol=?"
    else:
        size_mult = max(0.2, min(1.0, 1.0 - (vol_bps / max(vol_cap_bps, 1.0)) * 0.7))
        vol_tag = f"vol={vol_bps:.0f}bps"

    # Active regime trade: HTF strong AND intraday continues same direction.
    if abs(htf) >= htf_threshold and htf * cont_bps >= 0:
        signed = htf * 0.7 + (cont_bps / max(cont_ref_bps, 1.0)) * 0.3
        signed = max(-1.0, min(1.0, signed)) * size_mult
        return _lean(tribe, signed,
                     reason=f"cta regime htf={htf:+.2f} cont={cont_bps:+.0f}bps {vol_tag}×{size_mult:.2f}",
                     horizon_seconds=horizon)

    # HTF strong but intraday counter — stick with the regime, lightly.
    if abs(htf) >= htf_threshold and htf * cont_bps < 0:
        signed = htf * 0.3 * size_mult
        return _lean(tribe, signed,
                     reason=f"cta hold htf={htf:+.2f} (counter cont={cont_bps:+.0f}bps)",
                     horizon_seconds=horizon, cap=0.30)

    # Idle: weak HTF-direction lean.
    signed = htf * 0.4 * size_mult
    return _lean(tribe, signed,
                 reason=f"cta idle htf={htf:+.2f} {vol_tag}",
                 horizon_seconds=horizon, cap=0.30)


@register("news_scalper")
def news_scalper(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Headline scalper — follows the initial news spike for <60s then exits.
    Opposite time-horizon from `news_reactor` (which fades the overreaction
    over 5-10min). The two archetypes consume the same news surface but
    express orthogonal views: scalper rides the first leg, reactor fades
    the echo.

    Trigger: severity-weighted pulse in the last ~120s exceeds threshold.
    Pulse is a weighted blend of regulation, influencer, and general
    headline scores — regulation weights most (Fed/SEC moves regime),
    influencer next (Twitter/Substack talkers actually move price in <2min),
    headline residual fills in.

    Idle: weak residual in the direction of the last-15min news sentiment.
    Never loud, never silent."""
    p = tribe.spec.strategy.params
    fresh_window_s = float(p.get("fresh_window_s", 120.0))
    trigger_score = float(p.get("trigger_score", 0.3))
    long_window_s = float(p.get("long_window_s", 900.0))
    horizon = int(p.get("hold_seconds", 60))

    reg = state.regulation_pulse(fresh_window_s)
    inf = state.influencer_pulse(fresh_window_s)
    headlines = state.news_score(fresh_window_s)
    n_items = state.news_volume(fresh_window_s)

    # Blend: regulation carries most weight, influencer next, residual
    # headlines (not regulation, not influencer) fill in.
    residual_headlines = headlines - reg - inf
    pulse = reg * 1.5 + inf * 0.8 + residual_headlines * 0.5
    pulse = max(-2.0, min(2.0, pulse))

    if n_items >= 1 and abs(pulse) >= trigger_score:
        signed = max(-1.0, min(1.0, pulse / (trigger_score * 2.0)))
        return _lean(tribe, signed,
                     reason=f"news spike pulse={pulse:+.2f} (n={n_items} in {fresh_window_s:.0f}s)",
                     horizon_seconds=horizon)

    # Idle — whisper in the direction of residual 15min news sentiment.
    residual = state.news_score(long_window_s)
    signed = max(-1.0, min(1.0, residual * 0.4))
    return _lean(tribe, signed,
                 reason=f"news quiet pulse={pulse:+.2f} n15={state.news_volume(long_window_s)}",
                 horizon_seconds=horizon, cap=0.25)


@register("cascade_amplifier")
def cascade_amplifier(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Liq-velocity reader — conviction scales with the SECOND derivative of
    liquidation flow. The tribe trades the RATE OF CHANGE, not the level.

    Compute two liq-rates: fast (last 15s) and slow (last 60s). When
    fast_rate / slow_rate >= accel_threshold the cascade is accelerating
    and we fire HARD — there's more stop-loss fuel still to burn. When the
    ratio is below threshold the cascade is a steady drip (weak idle lean)
    or already decelerating (exhausted — no trade).

    Compared to `liquidation_hunter` (which reads net liq notional), this
    tribe won't fire on a big one-shot liq that happened 45s ago — it fires
    on the acceleration IN the last 15s. Orthogonality is the acceleration
    signal itself.

    Idle: weak residual in the direction implied by the slow-window net
    (follow — if longs were liq'd, continuation is still DOWN)."""
    p = tribe.spec.strategy.params
    fast_win = float(p.get("fast_window_s", 15.0))
    slow_win = float(p.get("slow_window_s", 60.0))
    accel_threshold = float(p.get("accel_threshold", 1.5))
    min_fast_usd = float(p.get("min_fast_usd", 400_000))
    horizon = int(p.get("hold_seconds", 90))

    fast_net = state.long_liq_usd(fast_win) - state.short_liq_usd(fast_win)
    slow_net = state.long_liq_usd(slow_win) - state.short_liq_usd(slow_win)

    fast_rate = abs(fast_net) / max(fast_win, 1.0)
    slow_rate = abs(slow_net) / max(slow_win, 1.0)

    if abs(fast_net) >= min_fast_usd and slow_rate > 0:
        accel_ratio = fast_rate / slow_rate
        if accel_ratio >= accel_threshold:
            # Cascade accelerating. Long liqs → continuation DOWN (short).
            direction = -1.0 if fast_net > 0 else 1.0
            magnitude = min(1.0, (accel_ratio - 1.0) / (accel_threshold * 2.0))
            signed = direction * (0.6 + 0.4 * magnitude)
            return _lean(tribe, signed,
                         reason=f"cascade accel {accel_ratio:.2f}× fast={fast_net:+,.0f}",
                         horizon_seconds=horizon)

    # Steady liq flow (no acceleration) → follow weakly.
    if abs(fast_net) >= min_fast_usd:
        signed = (-1.0 if fast_net > 0 else 1.0) * 0.35
        return _lean(tribe, signed,
                     reason=f"liq steady fast={fast_net:+,.0f}",
                     horizon_seconds=horizon, cap=0.45)

    # Idle: weak residual follow on slow-window imbalance.
    residual = slow_net / max(min_fast_usd * 2.0, 1.0)
    signed = -residual * 0.3
    return _lean(tribe, signed,
                 reason=f"no cascade (fast={fast_net:+,.0f} slow={slow_net:+,.0f})",
                 horizon_seconds=horizon, cap=0.20)


@register("capitulation_buyer")
def capitulation_buyer(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Patient contrarian — FADES liq cascades once they've gone far enough.
    Closes the directional-bias gap: three tribes (liquidation_hunter,
    mev_frontrun, cascade_amplifier) all FOLLOW liq direction; this tribe
    is the balancing force.

    Two gates must BOTH fire:
      1. Total liq (long+short) notional in window ≥ `min_liq_usd` — it's
         a real wash-out, not drip liquidations.
      2. Price has travelled ≥ `stretch_vol_mult` × realized_vol over
         the same stretch window — truly stretched, not just panicked.

    When both fire, take the OPPOSITE side of the net cascade direction:
    longs liq'd → BUY; shorts liq'd → SELL the squeeze top. Conviction
    scales with total-liq intensity and price-stretch factor.

    Idle: patient cash. A very weak contrarian whisper against recent flow,
    capped tiny."""
    p = tribe.spec.strategy.params
    liq_window_s = float(p.get("liq_window_s", 120.0))
    min_liq_usd = float(p.get("min_liq_usd", 1_200_000))
    stretch_window_s = float(p.get("stretch_window_s", 600.0))
    stretch_vol_mult = float(p.get("stretch_vol_mult", 1.3))
    horizon = int(p.get("hold_seconds", 1800))

    longs = state.long_liq_usd(liq_window_s)
    shorts = state.short_liq_usd(liq_window_s)
    net = longs - shorts
    total = longs + shorts

    move_bps = state.price_change_bps(stretch_window_s)
    vol_bps = state.realized_vol_bps(stretch_window_s)

    # Both gates: capitulation-sized liq AND price stretched vs realized vol.
    if total >= min_liq_usd and vol_bps and vol_bps > 0 and \
            abs(move_bps) >= vol_bps * stretch_vol_mult:
        # Fade the net-cascade direction.
        # net>0 (long liqs dominate) → price dumped → BUY.
        direction = 1.0 if net > 0 else -1.0
        intensity = min(1.0, total / (min_liq_usd * 3.0))
        stretch_factor = min(1.0, abs(move_bps) / max(vol_bps * stretch_vol_mult * 2.0, 1.0))
        signed = direction * (0.5 + 0.5 * intensity) * stretch_factor
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"capitulation fade total={total:,.0f} move={move_bps:+.0f}bps vol={vol_bps:.0f}bps",
                     horizon_seconds=horizon)

    # Large liq but not stretched yet — patience, no active vote.
    if total >= min_liq_usd:
        vol_tag = f"vol={vol_bps:.0f}bps" if vol_bps else "vol=?"
        return _lean(tribe, 0.0,
                     reason=f"liq cluster, waiting stretch (total={total:,.0f} move={move_bps:+.0f}bps {vol_tag})",
                     horizon_seconds=horizon)

    # Idle: weak contrarian lean to recent flow, capped tiny. Never aligned
    # with the herd.
    flow = state.taker_flow_usd(stretch_window_s)
    signed = max(-0.15, min(0.15, -flow / 10_000_000.0 * 0.1))
    return _lean(tribe, signed,
                 reason=f"waiting capitulation (flow={flow:+,.0f})",
                 horizon_seconds=horizon, cap=0.15)


@register("options_dealer_gamma")
def options_dealer_gamma(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Options market-maker hedging flow proxy.

    Reads Deribit aggregate OI to estimate the dealer gamma regime, then
    positions spot accordingly:

      PIN MODE (long-gamma, low realized vol, spot near OI-weighted strike):
        Dealers mean-revert spot toward the strike via delta hedges. Lean
        in the direction of (strike − spot), scaled by the deviation.

      AMPLIFY MODE (short-gamma proxy: high realized vol + unbalanced OI):
        Dealer hedges chase the tape, amplifying moves. Lean with recent
        taker flow.

      Else: idle (no identifiable regime).

    Idle posture: flat. This tribe only adds directional signal when the
    options structure is readable — no background bias.
    """
    p = tribe.spec.strategy.params
    vol_window_s = float(p.get("vol_window_s", 900.0))
    low_vol_bps = float(p.get("low_vol_bps", 25.0))
    high_vol_bps = float(p.get("high_vol_bps", 80.0))
    pin_band_bps = float(p.get("pin_band_bps", 150.0))
    flow_window_s = float(p.get("flow_window_s", 300.0))
    flow_ref_usd = float(p.get("flow_ref_usd", 2_500_000.0))
    horizon = int(p.get("hold_seconds", 1200))

    strike = state.last_oi_weighted_strike
    spot = state.last_price or state.last_mark_price
    vol_bps = state.realized_vol_bps(vol_window_s) or 0.0

    if strike is None or strike <= 0 or spot is None or spot <= 0:
        return _lean(tribe, 0.0,
                     reason="options surface not yet populated",
                     horizon_seconds=horizon, cap=0.08)

    dev_bps = (spot - strike) / strike * 10_000.0

    # PIN MODE: long-gamma regime, near the strike, calm tape.
    if vol_bps <= low_vol_bps and abs(dev_bps) <= pin_band_bps:
        # Lean toward strike — if spot > strike, SELL; if spot < strike, BUY.
        direction = -1.0 if dev_bps > 0 else 1.0
        magnitude = min(1.0, abs(dev_bps) / max(pin_band_bps, 1.0))
        signed = direction * (0.35 + 0.5 * magnitude)
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"gamma-pin fade dev={dev_bps:+.0f}bps vol={vol_bps:.0f}bps strike={strike:,.0f}",
                     horizon_seconds=horizon)

    # AMPLIFY MODE: short-gamma proxy — elevated vol with real OI imbalance.
    net_delta = state.last_net_delta_exposure_contracts or 0.0
    if vol_bps >= high_vol_bps and abs(net_delta) > 5_000:
        flow = state.taker_flow_usd(flow_window_s)
        if abs(flow) >= flow_ref_usd * 0.5:
            direction = 1.0 if flow > 0 else -1.0
            intensity = min(1.0, abs(flow) / max(flow_ref_usd, 1.0))
            signed = direction * (0.3 + 0.5 * intensity)
            return _lean(tribe, max(-1.0, min(1.0, signed)),
                         reason=f"short-gamma amplify flow={flow:+,.0f} vol={vol_bps:.0f}bps net_oi={net_delta:+,.0f}",
                         horizon_seconds=horizon)

    return _lean(tribe, 0.0,
                 reason=f"no gamma regime read (dev={dev_bps:+.0f}bps vol={vol_bps:.0f}bps)",
                 horizon_seconds=horizon, cap=0.05)


@register("put_call_fader")
def put_call_fader(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Contrarian on Deribit put/call ratio.

    High pc_ratio = aggregate option buyers are fear-loaded → FADE → BUY.
    Low pc_ratio  = call-heavy euphoria → FADE → SELL.
    Between thresholds: idle (no crowd extreme to fade).

    Conviction scales with distance beyond the threshold, boosted by a
    recent pc_ratio CHANGE (captures fresh sentiment shocks, not just
    slow-moving state). No trend-following; pure contrarian.
    """
    p = tribe.spec.strategy.params
    pc_lo = float(p.get("pc_ratio_lo", 0.60))
    pc_hi = float(p.get("pc_ratio_hi", 1.30))
    change_window_s = float(p.get("change_window_s", 1800.0))
    change_ref = float(p.get("change_ref", 0.20))
    horizon = int(p.get("hold_seconds", 3600))

    pc = state.last_pc_ratio
    if pc is None or pc <= 0:
        return _lean(tribe, 0.0,
                     reason="pc_ratio not yet populated",
                     horizon_seconds=horizon, cap=0.04)

    pc_delta = state.pc_ratio_change(change_window_s)

    if pc >= pc_hi:
        # Fear extreme → BUY. Magnitude scales with how far beyond pc_hi.
        over = pc - pc_hi
        mag = min(1.0, over / max(pc_hi * 0.4, 1e-6))
        # A rising pc_ratio (positive delta) reinforces fear → boost conviction.
        boost = min(0.3, max(0.0, pc_delta) / max(change_ref, 1e-6) * 0.3)
        signed = 0.45 + 0.45 * mag + boost
        return _lean(tribe, min(1.0, signed),
                     reason=f"pc_ratio={pc:.2f} (>{pc_hi:.2f}) fear fade Δ{change_window_s/60:.0f}m={pc_delta:+.2f}",
                     horizon_seconds=horizon)

    if pc <= pc_lo:
        # Euphoria extreme → SELL. Magnitude scales with how far below pc_lo.
        under = pc_lo - pc
        mag = min(1.0, under / max(pc_lo * 0.4, 1e-6))
        boost = min(0.3, max(0.0, -pc_delta) / max(change_ref, 1e-6) * 0.3)
        signed = -(0.45 + 0.45 * mag + boost)
        return _lean(tribe, max(-1.0, signed),
                     reason=f"pc_ratio={pc:.2f} (<{pc_lo:.2f}) euphoria fade Δ{change_window_s/60:.0f}m={pc_delta:+.2f}",
                     horizon_seconds=horizon)

    # Mid-zone — idle.
    return _lean(tribe, 0.0,
                 reason=f"pc_ratio={pc:.2f} in neutral band [{pc_lo:.2f}, {pc_hi:.2f}]",
                 horizon_seconds=horizon, cap=0.05)


@register("stablecoin_flow_buyer")
def stablecoin_flow_buyer(tribe: "Tribe", ts: datetime, state: MarketState) -> Vote:
    """Macro allocator reading aggregate USDT+USDC circulating supply.

    Expanding supply → fresh fiat bridging to exchanges → bullish bias on
    BTC over 2-4h. Contracting supply → redemptions / deleveraging → bearish.

    Two-window confirmation: the 6h change must cross a threshold AND the
    24h trend must agree in sign. This double gate filters out the routine
    daily minting noise (Tether mints in ~1B batches; we want the
    multi-batch direction, not single events).

    Idle: slight structural long bias per demographics.
    """
    p = tribe.spec.strategy.params
    flow_window_s = float(p.get("flow_window_s", 21600.0))
    pos_ref_pct = float(p.get("pos_ref_pct", 0.25))
    neg_ref_pct = float(p.get("neg_ref_pct", 0.15))
    trend_window_s = float(p.get("trend_window_s", 86400.0))
    horizon = int(p.get("hold_seconds", 14400))

    short_pct = state.stablecoin_supply_change_pct(flow_window_s)
    long_pct = state.stablecoin_supply_change_pct(trend_window_s)

    if state.last_stablecoin_combined is None:
        return _lean(tribe, 0.0,
                     reason="stablecoin supply not yet populated",
                     horizon_seconds=horizon, cap=0.05)

    # Bullish gate: 6h expansion + 24h trend agrees.
    if short_pct >= pos_ref_pct and long_pct > 0:
        mag = min(1.0, short_pct / max(pos_ref_pct * 3.0, 1e-6))
        trend_boost = min(0.25, long_pct / max(pos_ref_pct * 2.0, 1e-6) * 0.25)
        signed = 0.4 + 0.5 * mag + trend_boost
        return _lean(tribe, min(1.0, signed),
                     reason=f"stablecoin expansion 6h={short_pct:+.2f}% 24h={long_pct:+.2f}% → bullish bridge flow",
                     horizon_seconds=horizon)

    # Bearish gate: 6h contraction + 24h trend agrees.
    if short_pct <= -neg_ref_pct and long_pct < 0:
        mag = min(1.0, abs(short_pct) / max(neg_ref_pct * 3.0, 1e-6))
        trend_boost = min(0.25, abs(long_pct) / max(neg_ref_pct * 2.0, 1e-6) * 0.25)
        signed = -(0.4 + 0.5 * mag + trend_boost)
        return _lean(tribe, max(-1.0, signed),
                     reason=f"stablecoin contraction 6h={short_pct:+.2f}% 24h={long_pct:+.2f}% → redemption pressure",
                     horizon_seconds=horizon)

    # Mixed signals or within neutral band — idle with tribe's structural
    # long bias.
    return _lean(tribe, 0.0,
                 reason=f"stablecoin flow neutral 6h={short_pct:+.2f}% 24h={long_pct:+.2f}%",
                 horizon_seconds=horizon, cap=0.10)
