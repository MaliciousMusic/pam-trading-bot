"""Runtime Tribe — wraps a spec and produces Votes at tick time.

Each tribe simulates its own open/hold/close lifecycle. The key invariant:
conviction represents the NET IMBALANCE within this archetype, not an
all-in position. A market-maker tribe at 38% weight always has buyers AND
sellers — the vote reflects only the directional excess.

When FLAT: looks for entry signal. Enters with full conviction.
When HOLDING: re-evaluates strategy each tick. Hold conviction scales with
  how much the CURRENT strategy confirms the position. If the strategy is
  fighting the held direction, conviction collapses toward zero (the tribe
  is internally balanced). When strategy flips with enough conviction the
  position closes.
On CLOSE: fires a brief counter-vote (unwind signal), enters cooldown.

Archetype DNA (Phase 2):
  - Shadow Inventory: arb/mm/hedger/mev run on finite "simulated capital."
    Every tick spent on the same side fills the inventory by a small
    amount; once near capacity the same-direction conviction gets haircut
    toward zero. Unwinds (voting the opposite side) are untouched. Retail,
    TA, event-hunters, and whales (who model their capacity differently)
    bypass this.
  - Drawdown aggression: retail tribes with several close-in-succession
    exits enter a "tilt" state — panic knob amplifies conviction. Models
    revenge-trading after a burn.
  - Daily budget: event-firing tribes (liquidation_hunter, news_fader,
    mev_frontrunner) wear out. Each firing debits a per-UTC-day budget;
    when exhausted, activation decays until midnight resets it. Models
    "the scanner pros ran out of capital after the third cascade."
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..core.types import Event, Side, Vote
from .indicators import evaluate as evaluate_indicators
from .market_state import MarketState
from .spec import TribeSpec
from .strategies import get as get_strategy

# Peak conviction emitted while holding (net imbalance cap).
_HOLD_CONVICTION_MAX = 0.05
# Minimum strategy conviction to trigger a new entry (when flat).
_ENTRY_MIN_CONV = 0.10
# Minimum strategy conviction to trigger an early exit (direction flip).
_FLIP_MIN_CONV = 0.15
# Exit vote = this fraction of entry conviction, reverse side.
_EXIT_SCALE = 0.50

# Strategy families that respect a finite simulated inventory. Arb/MM/hedger
# desks are capital-bounded by definition; MEV extractors likewise have a
# finite book of risk willingness per block. Retail/TA/event tribes either
# have crowd-scale collective capital (no single inventory ceiling) or
# already encode their capacity via other knobs (whale via twap_remainder).
_CAPACITY_CONSTRAINED_FAMILIES: frozenset[str] = frozenset({
    "basis_arb", "cross_venue_arb", "funding_fader",
    "mean_reverter_mm", "grid_bot",
    "options_hedger_proxy", "leverage_cycle",
    "mev_frontrun",
})
# Per-tick inventory fill while holding (normalized to capacity=1.0). 0.05
# means a tribe holding for 20 consecutive ticks fills to full capacity —
# at the cadence the bot runs (5s ticks) that's 100 seconds, a plausible
# fill window for a ladder of limit orders getting eaten.
_INVENTORY_FILL_PER_HOLD_TICK: float = 0.05
# Decay applied to inventory on close — some residual position remains
# (not every close fully flattens).
_INVENTORY_CLOSE_DECAY: float = 0.4
# Same-direction haircut takes effect once inventory exceeds this floor.
# Below the floor, the tribe votes at full conviction.
_INVENTORY_HAIRCUT_FLOOR: float = 0.5

# Retail tilt: this many closes inside this many seconds triggers the
# drawdown_aggression state, amplifying next entry's conviction by
# `_RETAIL_TILT_BOOST`. Models revenge trading.
_RETAIL_TILT_WINDOW_S: float = 300.0
_RETAIL_TILT_MIN_CLOSES: int = 3
_RETAIL_TILT_BOOST: float = 1.35
_RETAIL_FAMILIES: frozenset[str] = frozenset({
    "app_fomo", "momentum_chaser", "copy_trader",
})

# Event-firing tribes have a per-UTC-day budget. Each close debits 1. When
# we've fired past the budget, activation rate decays linearly toward zero
# over the next `_EVENT_FATIGUE_OVERDRAW` firings (then clamps). Reset at
# UTC midnight.
_EVENT_DAILY_BUDGET: int = 12
_EVENT_FATIGUE_OVERDRAW: int = 8
_EVENT_FAMILIES: frozenset[str] = frozenset({
    "liquidation_hunter", "news_reactor", "mev_frontrun",
})


class Tribe:
    def __init__(self, spec: TribeSpec, *, rng_seed: int | None = None) -> None:
        self.spec = spec
        self._cooldown_until: datetime | None = None
        # Simulated position state
        self._sim_side: Side | None = None          # None = flat
        self._sim_entry_ts: datetime | None = None
        self._sim_entry_conv: float = 0.0
        self._sim_hold_seconds: float = 0.0
        # Per-tribe RNG. Isolates each tribe's stochastic draws so enabling
        # or disabling one tribe never reshuffles another's stream — the key
        # property for deterministic replay across variant configurations.
        # rng_seed=None → nondeterministic fresh Random (live behavior).
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
        # Phase 2a — Shadow Inventory. Signed ∈ [-1, +1]; positive = net
        # long. Filled each tick while holding in the held direction, decayed
        # on close. Non-capacity-constrained families keep this at 0 forever.
        self._capacity_constrained: bool = spec.strategy.family in _CAPACITY_CONSTRAINED_FAMILIES
        self._shadow_inventory: float = 0.0
        # Phase 2b — Tilt (retail) + Daily budget / Fatigue (event).
        self._recent_close_ts: deque[datetime] = deque(maxlen=_RETAIL_TILT_MIN_CLOSES + 2)
        self._daily_fire_count: int = 0
        self._daily_fire_day: datetime | None = None   # UTC date of last fire

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def weight(self) -> float:
        return self.spec.flow.weight

    def presence_factor(self, ts: datetime) -> float:
        hour = ts.astimezone(timezone.utc).hour
        curve = self.spec.circadian.presence_by_hour_utc
        if not curve:
            return 1.0
        return max(0.0, min(1.0, float(curve[hour % len(curve)])))

    def effective_weight(self, ts: datetime) -> float:
        return self.weight * self.presence_factor(ts)

    def observe(self, ev: Event) -> None:
        return None

    def indicator_blend(self, state: MarketState) -> tuple[float, dict[str, float]]:
        return evaluate_indicators(state, self.spec.indicators)

    def _effective_indicator_weight(self, state: MarketState) -> float:
        w = max(0.0, min(1.0, float(self.spec.indicator_weight)))
        rng = float(getattr(self.spec, "indicator_regime_range", 0.0) or 0.0)
        if rng == 0.0:
            return w
        window = float(getattr(self.spec, "indicator_regime_window_s", 300.0))
        rng_band = state.price_range(window)
        if rng_band is None or rng_band[0] <= 0:
            return w
        realized_bps = (rng_band[1] - rng_band[0]) / rng_band[0] * 10_000.0
        lo = float(self.spec.indicator_regime_low_bps)
        hi = float(self.spec.indicator_regime_high_bps)
        if hi <= lo:
            return w
        norm = max(0.0, min(1.0, (realized_bps - lo) / (hi - lo)))
        side = 2.0 * norm - 1.0
        if str(self.spec.indicator_regime_prefer).lower() == "low":
            side = -side
        return max(0.0, min(1.0, w + rng * side))

    def _blend_with_indicators(self, state: MarketState, v: Vote) -> Vote:
        if not self.spec.indicators or self.spec.indicator_weight <= 0:
            return v
        if v.side is Side.ABSTAIN:
            return v
        blend, _ = self.indicator_blend(state)
        if blend == 0.0:
            return v
        w = self._effective_indicator_weight(state)
        strat_signed = v.conviction if v.side is Side.BUY else -v.conviction
        combined = max(-1.0, min(1.0, (1.0 - w) * strat_signed + w * blend))
        if abs(combined) < 1e-6:
            return replace(v, side=Side.ABSTAIN, conviction=0.0,
                           reason=v.reason + " | indicators cancel strat")
        side = Side.BUY if combined > 0 else Side.SELL
        return replace(v, side=side, conviction=abs(combined),
                       reason=v.reason + f" | ind={blend:+.2f}")

    def _sample_hold_seconds(self) -> float:
        lo, hi = self.spec.flow.typical_hold_seconds
        lo = max(1.0, float(lo))
        hi = max(lo, float(hi))
        log_lo = math.log(lo)
        log_hi = math.log(hi)
        return min(math.exp(self._rng.uniform(log_lo, log_hi)), 86400.0)

    def _fresh_signal(self, ts: datetime, state: MarketState) -> Vote:
        strat = get_strategy(self.spec.strategy.family)
        v = strat(self, ts, state)
        return self._blend_with_indicators(state, v)

    def project_vote(self, ts: datetime, state: MarketState) -> Vote:
        """Raw strategy signal for forward projection — no lifecycle gating.
        Bypasses flat/hold/cooldown so the projector gets a fresh strategy
        read at every step instead of collapsing to hold-conviction after step 1."""
        v = self._fresh_signal(ts, state)
        return self._apply_weight_and_fractions(ts, v)

    def _apply_weight_and_fractions(self, ts: datetime, v: Vote) -> Vote:
        eff = self.effective_weight(ts)
        act_base = max(0.0, min(1.0, float(getattr(self.spec.flow, "activation_rate", 1.0))))
        # Phase 2b — daily fatigue for event-firing tribes. Rolls UTC daily.
        act = act_base * self._fatigue_activation_factor(ts)
        raw_conv = max(0.0, min(1.0, v.conviction))
        # Phase 2a — Shadow Inventory haircut. Only bites when the tribe is
        # capacity-constrained AND voting to ADD to its current inventory
        # (same-sign as _shadow_inventory). Unwinds are untouched: if a
        # full-long arb tribe votes SELL, we want that exit to fire loud,
        # not muted.
        if self._capacity_constrained and v.side is not Side.ABSTAIN:
            inv = self._shadow_inventory
            adding = (v.side is Side.BUY and inv > 0) or (v.side is Side.SELL and inv < 0)
            if adding and abs(inv) > _INVENTORY_HAIRCUT_FLOOR:
                # room ∈ [0, 1]: 1 when we're right at the floor, 0 at full.
                room = max(0.0, (1.0 - abs(inv)) / (1.0 - _INVENTORY_HAIRCUT_FLOOR))
                raw_conv *= room
        # Phase 2b — retail tilt amplifier.
        raw_conv = min(1.0, raw_conv * self._retail_tilt_factor(ts))
        new_conv = raw_conv * act
        if v.side is Side.BUY:
            buy_frac = act * (0.5 + raw_conv / 2.0)
            sell_frac = act * max(0.0, 0.5 - raw_conv / 2.0)
        elif v.side is Side.SELL:
            sell_frac = act * (0.5 + raw_conv / 2.0)
            buy_frac = act * max(0.0, 0.5 - raw_conv / 2.0)
        else:
            buy_frac = 0.0
            sell_frac = 0.0
        return replace(v, weight=eff, conviction=max(0.0, min(1.0, new_conv)),
                       buy_frac=buy_frac, sell_frac=sell_frac)

    def _retail_tilt_factor(self, ts: datetime) -> float:
        """Retail-only. Returns >1 if the tribe has closed ≥N positions inside
        the last `_RETAIL_TILT_WINDOW_S` — a "revenge-trading" amplifier that
        lets the panic knob actually express itself in the voting signal."""
        if self.spec.strategy.family not in _RETAIL_FAMILIES:
            return 1.0
        if len(self._recent_close_ts) < _RETAIL_TILT_MIN_CLOSES:
            return 1.0
        # Oldest close among the tracked window.
        oldest = self._recent_close_ts[0]
        age_s = (ts - oldest).total_seconds()
        if age_s > _RETAIL_TILT_WINDOW_S:
            return 1.0
        # Scale the boost by the tribe's intrinsic panic knob — so chill
        # retail tribes (low panic) tilt less than degen ones (high panic).
        panic = max(0.0, min(1.0, float(self.spec.personality.panic)))
        return 1.0 + (_RETAIL_TILT_BOOST - 1.0) * (0.5 + 0.5 * panic)

    def _fatigue_activation_factor(self, ts: datetime) -> float:
        """Event-tribe only. Daily firing budget — once past the budget, the
        activation rate decays linearly, reaching 0 after `_EVENT_FATIGUE_OVERDRAW`
        extra firings. Resets on UTC midnight."""
        if self.spec.strategy.family not in _EVENT_FAMILIES:
            return 1.0
        # Day roll → reset counter.
        today = ts.astimezone(timezone.utc).date()
        last_day = self._daily_fire_day.date() if self._daily_fire_day else None
        if last_day is None or today != last_day:
            return 1.0  # fresh day; counter will reset on next close event
        if self._daily_fire_count <= _EVENT_DAILY_BUDGET:
            return 1.0
        overdraw = self._daily_fire_count - _EVENT_DAILY_BUDGET
        return max(0.0, 1.0 - overdraw / max(1, _EVENT_FATIGUE_OVERDRAW))

    def vote(self, ts: datetime, state: MarketState) -> Vote:
        v = self._lifecycle_vote(ts, state)
        return self._apply_weight_and_fractions(ts, v)

    def _lifecycle_vote(self, ts: datetime, state: MarketState) -> Vote:
        if self._sim_side is None:
            return self._flat_vote(ts, state)
        return self._inposition_vote(ts, state)

    def _flat_vote(self, ts: datetime, state: MarketState) -> Vote:
        if self._cooldown_until and ts < self._cooldown_until:
            return self._idle_lean("cooldown")

        v = self._fresh_signal(ts, state)

        if v.side is not Side.ABSTAIN and v.conviction >= _ENTRY_MIN_CONV:
            self._sim_side = v.side
            self._sim_entry_ts = ts
            self._sim_entry_conv = v.conviction
            self._sim_hold_seconds = self._sample_hold_seconds()
            return replace(v, reason=v.reason + f" | SIM_OPEN hold≈{self._sim_hold_seconds:.0f}s")

        return v

    def _inposition_vote(self, ts: datetime, state: MarketState) -> Vote:
        assert self._sim_entry_ts is not None
        held_s = (ts - self._sim_entry_ts).total_seconds()

        # Phase 2a — accumulate shadow inventory while holding (only for
        # capacity-constrained tribes; others ignore the fill).
        if self._capacity_constrained and self._sim_side is not None:
            signed_fill = (_INVENTORY_FILL_PER_HOLD_TICK
                           if self._sim_side is Side.BUY else -_INVENTORY_FILL_PER_HOLD_TICK)
            self._shadow_inventory = max(-1.0, min(1.0,
                self._shadow_inventory + signed_fill))

        # Fresh strategy signal — used for both exit detection and hold sizing.
        sig = self._fresh_signal(ts, state)

        # Exit: hold window expired OR direction flip with enough conviction.
        hold_expired = held_s >= self._sim_hold_seconds
        direction_flip = (
            sig.side is not Side.ABSTAIN
            and sig.side is not self._sim_side
            and sig.conviction >= _FLIP_MIN_CONV
        )
        if hold_expired or direction_flip:
            return self._close_sim(ts, held_s, "expired" if hold_expired else "flip")

        # Hold conviction reflects the NET imbalance at this moment.
        # When the current strategy confirms the position: tribe is tilted,
        # emit up to _HOLD_CONVICTION_MAX. When strategy is flat or opposing:
        # the tribe's buyers and sellers are balanced → near-zero.
        if sig.side is self._sim_side:
            alignment = sig.conviction          # 0..1 confirming
            hold_conv = _HOLD_CONVICTION_MAX * max(0.2, alignment)
        elif sig.side is Side.ABSTAIN:
            hold_conv = _HOLD_CONVICTION_MAX * 0.3   # no read → quiet
        else:
            hold_conv = _HOLD_CONVICTION_MAX * 0.05  # fighting → near-silent

        reason = (f"SIM_HOLD {held_s:.0f}s/{self._sim_hold_seconds:.0f}s "
                  f"sig={sig.conviction:+.2f}")
        return Vote(
            tribe_id=self.spec.id,
            side=self._sim_side,
            conviction=hold_conv,
            horizon_seconds=max(0, int(self._sim_hold_seconds - held_s)),
            weight=self.weight,
            reason=reason,
        )

    def _close_sim(self, ts: datetime, held_s: float, why: str) -> Vote:
        exit_side = Side.SELL if self._sim_side is Side.BUY else Side.BUY
        exit_conv = self._sim_entry_conv * _EXIT_SCALE
        reason = f"SIM_CLOSE {why} held={held_s:.0f}s"

        cd = self.spec.flow.cooldown_seconds
        if cd > 0:
            self._cooldown_until = ts + timedelta(seconds=cd)

        # Phase 2a — inventory partial-flush on close.
        if self._capacity_constrained:
            self._shadow_inventory *= _INVENTORY_CLOSE_DECAY

        # Phase 2b — bookkeeping for retail tilt and event daily-budget.
        self._recent_close_ts.append(ts)
        today = ts.astimezone(timezone.utc).date()
        last_day = self._daily_fire_day.date() if self._daily_fire_day else None
        if last_day != today:
            self._daily_fire_count = 0
        self._daily_fire_count += 1
        self._daily_fire_day = ts

        self._sim_side = None
        self._sim_entry_ts = None
        self._sim_entry_conv = 0.0
        self._sim_hold_seconds = 0.0

        return Vote(
            tribe_id=self.spec.id,
            side=exit_side,
            conviction=max(0.0, min(1.0, exit_conv)),
            horizon_seconds=0,
            weight=self.weight,
            reason=reason,
        )

    def _idle_lean(self, reason: str) -> Vote:
        d = self.spec.disposition
        bias = max(-1.0, min(1.0, float(d.idle_bias)))
        conv = max(0.0, min(1.0, float(d.idle_conviction)))
        if bias == 0.0 or conv == 0.0:
            side = Side.ABSTAIN
            conv = 0.0
        else:
            side = Side.BUY if bias > 0 else Side.SELL
            conv = conv * abs(bias)
        return Vote(
            tribe_id=self.spec.id, side=side, conviction=conv,
            horizon_seconds=0, weight=self.weight, reason=reason,
        )
