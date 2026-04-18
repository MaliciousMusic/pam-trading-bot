"""Runtime Tribe — wraps a spec and produces Votes at tick time.

A Tribe is nearly stateless: strategies read from MarketState. The
instance keeps only the minimum it genuinely needs (cooldowns, rolling
personal memory if any).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..core.types import Event, Side, Vote
from .indicators import evaluate as evaluate_indicators
from .market_state import MarketState
from .spec import TribeSpec
from .strategies import get as get_strategy


class Tribe:
    def __init__(self, spec: TribeSpec) -> None:
        self.spec = spec
        self._cooldown_until: datetime | None = None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def weight(self) -> float:
        return self.spec.flow.weight

    def presence_factor(self, ts: datetime) -> float:
        """0..1 multiplier on this tribe's weight at the given UTC hour.
        Captures 'the APAC crowd is asleep right now' — people sleep, bots
        don't. Retail tribes should drop here; 24/7 algos stay at 1.0."""
        hour = ts.astimezone(timezone.utc).hour
        curve = self.spec.circadian.presence_by_hour_utc
        if not curve:
            return 1.0
        # Tolerant of misconfigured curves (not exactly length 24).
        return max(0.0, min(1.0, float(curve[hour % len(curve)])))

    def effective_weight(self, ts: datetime) -> float:
        return self.weight * self.presence_factor(ts)

    def observe(self, ev: Event) -> None:
        """Optional hook. MarketState already absorbs everything — override
        only if this tribe needs private memory beyond what MarketState offers.
        """
        return None

    def indicator_blend(self, state: MarketState) -> tuple[float, dict[str, float]]:
        """Evaluate this tribe's declared indicator lens. Returns
        (signed_signal in [-1,+1], per-indicator breakdown for logging)."""
        return evaluate_indicators(state, self.spec.indicators)

    def _effective_indicator_weight(self, state: MarketState) -> float:
        """Blend weight, optionally shifted by realized-volatility regime.

        Low-vol whipsaw punishes indicator-heavy tribes unless they fade
        (grid bots live in chop). High-vol trending rewards momentum /
        RSI / zscore readings. When `indicator_regime_range == 0` the
        weight is the static spec value (old behavior)."""
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
        side = 2.0 * norm - 1.0  # -1 at/below lo, +1 at/above hi
        if str(self.spec.indicator_regime_prefer).lower() == "low":
            side = -side
        return max(0.0, min(1.0, w + rng * side))

    def _blend_with_indicators(self, state: MarketState, v: Vote) -> Vote:
        """Mix the indicator-lens signal into the strategy's vote.

          final_signed = (1-w)·strat_signed + w·blend
          w            = spec.indicator_weight ∈ [0,1], optionally regime-shifted

        Agreement amplifies conviction (both vectors point same way);
        disagreement shrinks it (honest reflection of internal conflict).
        Side flips to follow the blended sign so the tribe never votes
        against its own indicator reading just because the strat flipped
        first on noise. When net combined is ≈0, the vote abstains — the
        archetype has no read."""
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

    def vote(self, ts: datetime, state: MarketState) -> Vote:
        if self._cooldown_until and ts < self._cooldown_until:
            # Resting from a strong take, but still present — idle lean only.
            v = self._idle_lean("cooldown")
        else:
            strat = get_strategy(self.spec.strategy.family)
            v = strat(self, ts, state)
            # Co-sign with declarative indicator lens. The strategy produced
            # its core signed conviction; the tribe's indicator block is its
            # "view of the tape" — windows, thresholds, follow/fade actions
            # tuned to this archetype. Blending them means the final vote
            # reflects BOTH the programmatic strat and the indicator reading.
            v = self._blend_with_indicators(state, v)
            cd = self.spec.flow.cooldown_seconds
            trigger = float(getattr(self.spec.flow, "strong_vote_conviction", 0.3))
            if v.side is not Side.ABSTAIN and v.conviction >= trigger and cd > 0:
                self._cooldown_until = ts + timedelta(seconds=cd)
        # Population-model corrections, applied to every vote:
        #   1) Circadian presence: the cohort that's asleep contributes less.
        #   2) Activation rate: only a fraction of the population is actually
        #      expressing a directional view this tick (0.5% for app newbies,
        #      100% for colo MMs). This scales CONVICTION, not weight — a
        #      newbie tribe with 0.5% activation means "most newbies aren't
        #      trading right now", so their aggregate force is dampened. We
        #      keep conviction in (0,1] instead of collapsing to idle so the
        #      few who ARE active still move the signal proportionally.
        eff = self.effective_weight(ts)
        act = max(0.0, min(1.0, float(getattr(self.spec.flow, "activation_rate", 1.0))))
        new_conv = v.conviction * act
        if eff != v.weight or new_conv != v.conviction:
            v = replace(v, weight=eff, conviction=max(0.0, min(1.0, new_conv)))
        return v

    def _idle_lean(self, reason: str) -> Vote:
        d = self.spec.disposition
        bias = max(-1.0, min(1.0, float(d.idle_bias)))
        conv = max(0.0, min(1.0, float(d.idle_conviction)))
        if bias == 0.0 or conv == 0.0:
            side = Side.ABSTAIN
            conv = 0.0
        else:
            side = Side.BUY if bias > 0 else Side.SELL
            conv = conv * abs(bias)        # scale conviction by |bias|
        return Vote(
            tribe_id=self.spec.id, side=side, conviction=conv,
            horizon_seconds=0, weight=self.weight, reason=reason,
        )

