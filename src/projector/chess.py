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
from ..tribes.tribe import Tribe
from .event_wave import step_event_bps


class ChessProjector:
    def __init__(
        self,
        tribes: list[Tribe],
        steps: int,
        tick_seconds: int,
        bps_per_unit_pressure: float = 8.0,
        confidence_decay: float = 0.9,
        depth_ref_usd: float = 10_000_000.0,
        depth_floor_usd: float = 500_000.0,
    ) -> None:
        self.tribes = tribes
        self.steps = steps
        self.tick_seconds = tick_seconds
        self.bps_per_unit_pressure = bps_per_unit_pressure
        self.confidence_decay = confidence_decay
        # Depth-aware kernel. The per-step bps becomes:
        #   step_bps = pressure × bps_per_unit_pressure × (depth_ref / depth_now)
        # so a thinner book than reference amplifies the move and a deeper
        # book absorbs it. Defaults: 10M USD reference (typical BTC-perp 5bps
        # depth), 500k floor so a momentarily empty book doesn't explode the
        # ratio. When no book is live yet, we fall back to the constant.
        self.depth_ref_usd = depth_ref_usd
        self.depth_floor_usd = depth_floor_usd

    def project(self, state: MarketState) -> Forecast:
        sim = state.snapshot()
        start_ts = sim.last_update or datetime.now(timezone.utc)
        ts = start_ts
        step_aggs: list = []
        step_bps_list: list[float] = []
        # Snapshot cooldown state per tribe so the projection doesn't mutate
        # the live Tribe object. Without this, a strong projected vote would
        # put the REAL tribe into cooldown.
        saved_cooldowns = [t._cooldown_until for t in self.tribes]
        prev_ts = ts
        try:
            for _ in range(self.steps):
                prev_ts = ts
                ts = ts + timedelta(seconds=self.tick_seconds)
                sim.advance_clock(ts)
                # Route through Tribe.vote() so circadian presence AND
                # activation_rate dampening are honored inside the projection.
                # Otherwise retail tribes vote at full conviction 24/7 in the
                # forecast, inflating confidence in off-peak hours.
                votes = [t.vote(ts, sim) for t in self.tribes]
                agg = aggregate_votes(votes, ts)
                step_aggs.append(agg)
                vote_bps = agg.net_pressure * self.bps_per_unit_pressure
                # Event wave: classified news items inject a Gaussian-spread
                # impact_bps whose mass arrives in this step is proportional
                # to how many tribes just "woke up" to the headline.
                event_bps = step_event_bps(
                    sim.pending_events, self.tribes,
                    prev_ts.timestamp(), ts.timestamp(),
                )
                total_bps = vote_bps + event_bps
                # Depth-aware scale: the side that has to absorb the total
                # flow sets the scale. Positive → ASK book absorbs; negative
                # → BID. Thinner absorbing side ⇒ bigger move.
                side_depth = (
                    sim.ask_depth_5bps_usd if total_bps >= 0
                    else sim.bid_depth_5bps_usd
                )
                if side_depth and side_depth > 0:
                    scale = self.depth_ref_usd / max(side_depth, self.depth_floor_usd)
                else:
                    scale = 1.0
                step_bps = total_bps * scale
                step_bps_list.append(step_bps)
                if sim.last_price is not None and sim.last_price > 0:
                    sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                    sim.prices.add(ts, sim.last_price)
                # Funding drifts toward zero as the market digests the move.
                # Crude, but better than the frozen value — long enough
                # horizons need SOME funding-dynamic so trend_follower's
                # funding_guard isn't stuck looking at stale extreme funding.
                if sim.last_funding_rate is not None:
                    sim.last_funding_rate *= 0.995
        finally:
            for t, cd in zip(self.tribes, saved_cooldowns):
                t._cooldown_until = cd

        kernel = [self.confidence_decay ** i for i in range(len(step_bps_list))]
        k_sum = sum(kernel) or 1.0
        expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))

        signal_quality = sum(
            (1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)
        ) / k_sum
        # Directional consistency uses kernel-weighted absolute activity so
        # that far-horizon noise (which barely contributes to expected_move
        # because of the decay kernel) doesn't pollute the denominator.
        # Numerator and denominator now use the same weighting — otherwise
        # extending horizon mechanically crushes conf toward zero.
        abs_activity = sum(abs(b) * w for b, w in zip(step_bps_list, kernel))
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
