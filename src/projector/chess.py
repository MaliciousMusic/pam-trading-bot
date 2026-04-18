"""Chess projector — forward-simulate N ticks ahead.

At each virtual step:
  1. Advance the shadow clock by tick_seconds; stale window entries age out.
  2. Each tribe re-votes against the projected state (strategies are pure
     functions, so we bypass the live Tribe.vote() cooldown machinery).
  3. Votes are aggregated; the step's net_pressure implies a price move
     via a simple linear kernel (bps_per_unit_pressure).
  4. The shadow state's price is mutated for the next step.

Expected move = depth-weighted sum of per-step bps (decay kernel damps
later steps). Confidence = signal_quality × directional_consistency,
both in [0,1]:
  - signal_quality: depth-weighted mean of (1 - step.uncertainty) — high
    when tribes agree and are engaged along the projected path.
  - directional_consistency: |net drift| / total activity — high when
    the path drifts decisively one way, low when steps whipsaw.
This replaces the prior pure-geometric decay (0.9^steps ≈ 0.12 regardless
of signal), so confidence actually moves with the forecast's quality.

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
        step_aggs: list = []
        step_bps_list: list[float] = []
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
            step_bps_list.append(step_bps)
            if sim.last_price is not None and sim.last_price > 0:
                sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                sim.prices.add(ts, sim.last_price)

        kernel = [self.confidence_decay ** i for i in range(len(step_bps_list))]
        k_sum = sum(kernel) or 1.0
        expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))

        signal_quality = sum(
            (1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)
        ) / k_sum
        abs_activity = sum(abs(b) for b in step_bps_list)
        directional = abs(expected_move_bps) / abs_activity if abs_activity > 0 else 0.0
        final_conf = max(0.0, min(1.0, signal_quality * directional))

        logger.debug(
            "projector: {} steps, move {:+.1f}bps, signal={:.2f} dir={:.2f} conf={:.2f}",
            self.steps, expected_move_bps, signal_quality, directional, final_conf,
        )
        return Forecast(
            ts=start_ts,
            steps=step_aggs,
            expected_move_bps=expected_move_bps,
            confidence=final_conf,
            horizon_seconds=self.steps * self.tick_seconds,
        )
