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

    Idle: sign of the longer-term drift, with weak conviction."""
    p = tribe.spec.strategy.params
    win = float(p.get("trend_window_seconds", 300))
    long_win = float(p.get("long_trend_window_seconds", 1800))
    min_bps = float(p.get("min_persistent_move_bps", 80))
    funding_guard = float(p.get("funding_guard", 0.001))
    reg_weight = float(p.get("regulation_weight", 0.5))

    move = state.price_change_bps(win)
    f = state.last_funding_rate
    reg = state.regulation_pulse(3600.0)

    if abs(move) >= min_bps:
        if (move > 0 and f > funding_guard) or (move < 0 and f < -funding_guard):
            long_move = state.price_change_bps(long_win)
            signed = (long_move / (min_bps * 4.0)) * 0.4 + reg * reg_weight * 0.3
            return _lean(tribe, max(-1.0, min(1.0, signed)),
                         reason=f"funding blocks trend, reg={reg:+.2f} f={f:.5f}",
                         horizon_seconds=int(p.get("hold_seconds", 3600)))
        signed = move / (min_bps * 2.5) + reg * reg_weight * 0.2
        return _lean(tribe, max(-1.0, min(1.0, signed)),
                     reason=f"trend {move:.0f}bps reg={reg:+.2f} f={f:.5f}",
                     horizon_seconds=int(p.get("hold_seconds", 3600)))

    # Idle: longer-window drift + regulation pulse (even without acute price
    # movement, a fresh regulatory signal is enough to take a position).
    long_move = state.price_change_bps(long_win)
    signed = (long_move / (min_bps * 3.0)) * 0.3 + reg * reg_weight * 0.4
    return _lean(tribe, max(-1.0, min(1.0, signed)),
                 reason=f"quiet; drift={long_move:+.0f}bps reg={reg:+.2f}",
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
    inf = state.influencer_pulse(inf_win)
    inf_n = state.news_volume(inf_win, tag="influencer")
    if inf_n >= 1 and abs(inf) >= inf_trigger and (inf * confirm) >= 0:
        signed = inf / (inf_trigger * 1.8)
        return _lean(tribe, signed,
                     reason=f"copy influencer {inf:+.2f} (n={inf_n}) conf={confirm:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 900)), cap=0.85)

    # Reddit herd + price confirmation.
    soc = state.social_score(sw)
    vol = state.social_volume(sw)
    if vol >= min_vol and abs(soc) >= trigger and (soc * confirm) > 0:
        signed = (soc / (trigger * 2.0))
        return _lean(tribe, signed, reason=f"copy signal {soc:+.1f} conf={confirm:+.0f}bps",
                     horizon_seconds=int(p.get("hold_seconds", 900)), cap=0.75)

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
