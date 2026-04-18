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
"""
from __future__ import annotations

import math
import random
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


class Tribe:
    def __init__(self, spec: TribeSpec) -> None:
        self.spec = spec
        self._cooldown_until: datetime | None = None
        # Simulated position state
        self._sim_side: Side | None = None          # None = flat
        self._sim_entry_ts: datetime | None = None
        self._sim_entry_conv: float = 0.0
        self._sim_hold_seconds: float = 0.0

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
        return min(math.exp(random.uniform(log_lo, log_hi)), 86400.0)

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
        act = max(0.0, min(1.0, float(getattr(self.spec.flow, "activation_rate", 1.0))))
        raw_conv = max(0.0, min(1.0, v.conviction))
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
