"""Runtime Tribe — wraps a spec and produces Votes at tick time.

A Tribe is nearly stateless: strategies read from MarketState. The
instance keeps only the minimum it genuinely needs (cooldowns, rolling
personal memory if any).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..core.types import Event, Side, Vote
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

    def vote(self, ts: datetime, state: MarketState) -> Vote:
        if self._cooldown_until and ts < self._cooldown_until:
            # Resting from a strong take, but still present — idle lean only.
            v = self._idle_lean("cooldown")
        else:
            strat = get_strategy(self.spec.strategy.family)
            v = strat(self, ts, state)
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

