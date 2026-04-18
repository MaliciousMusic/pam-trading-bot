"""Runtime Tribe — wraps a spec and produces Votes at tick time.

A Tribe is nearly stateless: strategies read from MarketState. The
instance keeps only the minimum it genuinely needs (cooldowns, rolling
personal memory if any).
"""
from __future__ import annotations

from datetime import datetime, timedelta

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

    def observe(self, ev: Event) -> None:
        """Optional hook. MarketState already absorbs everything — override
        only if this tribe needs private memory beyond what MarketState offers.
        """
        return None

    def vote(self, ts: datetime, state: MarketState) -> Vote:
        if self._cooldown_until and ts < self._cooldown_until:
            return self._abstain("cooldown")
        strat = get_strategy(self.spec.strategy.family)
        v = strat(self, ts, state)
        cd = self.spec.flow.cooldown_seconds
        if v.side is not Side.ABSTAIN and cd > 0:
            self._cooldown_until = ts + timedelta(seconds=cd)
        return v

    def _abstain(self, reason: str = "") -> Vote:
        return Vote(
            tribe_id=self.spec.id,
            side=Side.ABSTAIN,
            conviction=0.0,
            horizon_seconds=0,
            weight=self.weight,
            reason=reason,
        )
