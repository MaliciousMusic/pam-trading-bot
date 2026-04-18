"""Chess projector — forward-simulate N ticks ahead.

At each virtual step:
  1. Advance the shadow clock by tick_seconds; stale window entries age out.
  2. Each tribe re-votes against the projected state (strategies are pure
     functions, so we bypass the live Tribe.vote() cooldown machinery).
  3. Votes are aggregated; the step's net_pressure implies a price move
     via a simple linear kernel (bps_per_unit_pressure).
  4. Confidence multiplicatively decays with depth.
  5. The shadow state's price is mutated for the next step.

What this projector intentionally does NOT simulate yet:
  - Cascade-triggered liquidations on projected price moves.
  - Synthetic taker flow from projected activity.
  - Funding shifts.
These are honest simplifications. When audit data shows they matter,
we graduate them — not before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from ..aggregator.voting import aggregate_votes
from ..core.types import Forecast
from ..tribes.market_state import MarketState
from ..tribes.strategies import get as get_strategy
from ..tribes.tribe import Tribe


class ChessProjector:
    def __init__(
        self,
        tribes: list[Tribe],
        steps: int,
        tick_seconds: int,
        bps_per_unit_pressure: float = 8.0,
        confidence_decay: float = 0.9,
    ) -> None:
        self.tribes = tribes
        self.steps = steps
        self.tick_seconds = tick_seconds
        self.bps_per_unit_pressure = bps_per_unit_pressure
        self.confidence_decay = confidence_decay

    def project(self, state: MarketState) -> Forecast:
        sim = state.snapshot()
        start_ts = sim.last_update or datetime.now(timezone.utc)
        ts = start_ts
        step_aggs = []
        cumulative_bps = 0.0
        confidence = 1.0
        for _ in range(self.steps):
            ts = ts + timedelta(seconds=self.tick_seconds)
            sim.advance_clock(ts)
            votes = []
            for t in self.tribes:
                try:
                    strat = get_strategy(t.spec.strategy.family)
                except KeyError:
                    continue
                votes.append(strat(t, ts, sim))
            agg = aggregate_votes(votes, ts)
            step_aggs.append(agg)
            step_bps = agg.net_pressure * self.bps_per_unit_pressure
            cumulative_bps += step_bps * confidence
            confidence *= self.confidence_decay
            if sim.last_price is not None and sim.last_price > 0:
                sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                sim.prices.add(ts, sim.last_price)
        final_conf = confidence
        logger.debug(
            "projector: {} steps, cumulative {:+.1f}bps, final conf {:.3f}",
            self.steps, cumulative_bps, final_conf,
        )
        return Forecast(
            ts=start_ts,
            steps=step_aggs,
            expected_move_bps=cumulative_bps,
            confidence=final_conf,
            horizon_seconds=self.steps * self.tick_seconds,
        )
