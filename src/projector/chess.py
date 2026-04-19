"""Chess projector — forward-simulate N ticks ahead.

At each virtual step:
  1. Advance the shadow clock by tick_seconds; stale window entries age out.
  2. Each tribe re-votes against the projected state (strategies are pure
     functions, so we bypass the live Tribe.vote() cooldown machinery).
  3. Per-tribe horizon decay: every vote's conviction is multiplied by
     exp(-step_seconds × i / hold_seconds) before aggregation, so scalpers
     (60s) fade out within the first few steps while swing traders (3600s)
     persist across the whole 10-minute projection.
  4. Aggregated net_pressure → bps via linear kernel.
  5. Ornstein-Uhlenbeck mean-reversion pulls cumulative drift back toward
     zero — a counterweight to the scale-invariant projector that previously
     produced smoothly-runaway curves.
  6. Uncertainty fan + Monte Carlo: per-step sigma is interpolated from the
     backscorer's horizon residuals; N stochastic realizations are drawn
     around the expected curve so the UI can render a wiggly band instead
     of a single stiff line.

Expected move = depth-weighted sum of per-step bps (decay kernel damps
later steps). Confidence = signal_quality × directional_consistency,
both in [0,1].

What this projector intentionally does NOT simulate:
  - Cascade-triggered liquidations (creates echo-chamber amplification).
  - Synthetic taker flow (self-reinforcing, inflates confidence).
  - Funding shifts.
"""
from __future__ import annotations

import math
import random
from dataclasses import replace
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
        *,
        auditor=None,
        mean_reversion_rate: float = 0.0,
        sample_paths_n: int = 0,
        default_horizon_seconds: float = 600.0,
        default_sigma_bps: float = 8.0,
        rng_seed: int | None = None,
    ) -> None:
        self.tribes = tribes
        self.steps = steps
        self.tick_seconds = tick_seconds
        self.bps_per_unit_pressure = bps_per_unit_pressure
        self.confidence_decay = confidence_decay
        self.depth_ref_usd = depth_ref_usd
        self.depth_floor_usd = depth_floor_usd
        self.regime_multiplier: float = 1.0
        # Wiggly-path knobs.
        self.auditor = auditor
        self.mean_reversion_rate = float(mean_reversion_rate)
        self.sample_paths_n = int(sample_paths_n)
        self.default_horizon_seconds = float(default_horizon_seconds)
        self.default_sigma_bps = float(default_sigma_bps)
        # Pre-cache each tribe's horizon so we don't dig through spec on every
        # step. Falls back to the default when the strategy leaves it off.
        self._tribe_horizon_s: dict[str, float] = {}
        for t in tribes:
            params = getattr(getattr(t.spec, "strategy", None), "params", {}) or {}
            hs = float(params.get("hold_seconds", default_horizon_seconds))
            self._tribe_horizon_s[t.spec.id] = max(1.0, hs)
        self._rng = random.Random(rng_seed)

    def project(self, state: MarketState) -> Forecast:
        sim = state.snapshot()
        start_ts = sim.last_update or datetime.now(timezone.utc)
        ts = start_ts
        step_aggs: list = []
        step_bps_list: list[float] = []
        cum_bps = 0.0
        steps_bps: list[float] = []
        prev_ts = ts
        effective_gain = self.bps_per_unit_pressure * self.regime_multiplier
        dt_s = float(self.tick_seconds)

        for step_i in range(self.steps):
            prev_ts = ts
            ts = ts + timedelta(seconds=dt_s)
            sim.advance_clock(ts)
            raw_votes = [t.project_vote(ts, sim) for t in self.tribes]
            # Per-tribe horizon decay — a scalper's signal dies out quickly,
            # a swing trader's persists. Exponential decay in *elapsed time*
            # since the projection started; the vote's own horizon is the
            # decay time-constant. Votes with ABSTAIN side carry zero
            # conviction already, so they're unaffected by the scaling.
            elapsed_s = (step_i + 1) * dt_s
            decayed_votes = []
            for v in raw_votes:
                hs = self._tribe_horizon_s.get(v.tribe_id, self.default_horizon_seconds)
                factor = math.exp(-elapsed_s / hs)
                new_conv = v.conviction * factor
                new_buy = v.buy_frac * factor
                new_sell = v.sell_frac * factor
                decayed_votes.append(replace(
                    v, conviction=new_conv,
                    buy_frac=new_buy, sell_frac=new_sell,
                ))
            agg = aggregate_votes(decayed_votes, ts)
            step_aggs.append(agg)
            vote_bps = agg.net_pressure * effective_gain
            event_bps = step_event_bps(
                sim.pending_events, self.tribes,
                prev_ts.timestamp(), ts.timestamp(),
            )
            total_bps = vote_bps + event_bps
            side_depth = (
                sim.ask_depth_5bps_usd if total_bps >= 0
                else sim.bid_depth_5bps_usd
            )
            if side_depth and side_depth > 0:
                scale = self.depth_ref_usd / max(side_depth, self.depth_floor_usd)
            else:
                scale = 1.0
            step_bps = total_bps * scale
            # Ornstein-Uhlenbeck mean-reversion on cumulative drift. Pulls
            # the running total toward zero at `mean_reversion_rate` per
            # step — counteracts the projector's previous bias of letting
            # small persistent pressures compound into enormous straight
            # lines. Rate of 0 disables (exact prior behavior).
            if self.mean_reversion_rate > 0.0 and cum_bps != 0.0:
                step_bps -= self.mean_reversion_rate * cum_bps
            step_bps_list.append(step_bps)
            cum_bps += step_bps
            steps_bps.append(cum_bps)
            if sim.last_price is not None and sim.last_price > 0:
                sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                sim.prices.add(ts, sim.last_price)
            if sim.last_funding_rate is not None:
                sim.last_funding_rate *= 0.995

        kernel = [self.confidence_decay ** i for i in range(len(step_bps_list))]
        k_sum = sum(kernel) or 1.0
        expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))

        signal_quality = sum(
            (1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)
        ) / k_sum
        abs_activity = sum(abs(b) * w for b, w in zip(step_bps_list, kernel))
        directional = abs(expected_move_bps) / abs_activity if abs_activity > 0 else 0.0
        final_conf = max(0.0, min(1.0, signal_quality * directional))

        steps_sigma_bps = self._sigma_per_step(len(step_bps_list))
        sample_paths, directional_agreement, terminals = self._per_tribe_paths(
            state, expected_move_bps, steps_sigma_bps,
        )
        # If per-tribe sampling is disabled, fall back to gaussian-around-mean
        # so the UI fan still has something to draw.
        if not sample_paths and self.sample_paths_n > 0:
            sample_paths = self._gaussian_paths(steps_bps, steps_sigma_bps)

        logger.debug(
            "projector: {} steps, move {:+.1f}bps, signal={:.2f} dir={:.2f} "
            "conf={:.2f} σ₀={:.1f} samples={}",
            self.steps, expected_move_bps, signal_quality, directional, final_conf,
            steps_sigma_bps[0] if steps_sigma_bps else 0.0, len(sample_paths),
        )
        return Forecast(
            ts=start_ts,
            steps=step_aggs,
            expected_move_bps=expected_move_bps,
            confidence=final_conf,
            horizon_seconds=self.steps * self.tick_seconds,
            steps_bps=steps_bps,
            steps_sigma_bps=steps_sigma_bps,
            sample_paths_bps=sample_paths,
            directional_agreement=directional_agreement,
            sample_terminals_bps=terminals,
        )

    # -- wiggly-path helpers ---------------------------------------------

    def _sigma_per_step(self, n_steps: int) -> list[float]:
        """Per-step cumulative sigma (bps). Interpolates the auditor's
        horizon-keyed residual stddevs across the step grid; falls back to
        √t scaling on `default_sigma_bps` when the auditor hasn't resolved
        enough anchors yet."""
        default = self.default_sigma_bps
        observed: dict[int, float] = {}
        if self.auditor is not None and hasattr(self.auditor, "sigma_by_horizon"):
            try:
                observed = self.auditor.sigma_by_horizon() or {}
            except Exception:
                observed = {}
        out: list[float] = []
        keys = sorted(observed.keys())
        for i in range(n_steps):
            t_s = (i + 1) * self.tick_seconds
            if not keys:
                # √t scaling anchored on default sigma at 60s (1 min).
                out.append(default * math.sqrt(t_s / 60.0))
                continue
            if t_s <= keys[0]:
                sigma = observed[keys[0]] * math.sqrt(t_s / keys[0])
            elif t_s >= keys[-1]:
                # Extrapolate with √t beyond the last known horizon.
                sigma = observed[keys[-1]] * math.sqrt(t_s / keys[-1])
            else:
                # Linear interpolation between the two surrounding horizons.
                lo = max(k for k in keys if k <= t_s)
                hi = min(k for k in keys if k >= t_s)
                if hi == lo:
                    sigma = observed[lo]
                else:
                    frac = (t_s - lo) / (hi - lo)
                    sigma = observed[lo] + frac * (observed[hi] - observed[lo])
            out.append(float(sigma))
        return out

    def _per_tribe_paths(self, state: MarketState, expected_move_bps: float,
                         steps_sigma_bps: list[float],
                         ) -> tuple[list[list[float]], float | None, list[float] | None]:
        """Run N shadow simulations where each tribe's vote is stochastically
        perturbed per path. The fraction of paths whose terminal bps shares
        the sign of the deterministic expected move is the directional
        agreement — a sharper confidence signal than the aggregate-then-noise
        approach. Returns (paths, agreement, terminals)."""
        n_paths = self.sample_paths_n
        n_steps = self.steps
        if n_paths <= 0 or n_steps == 0 or not self.tribes:
            return [], None, None
        effective_gain = self.bps_per_unit_pressure * self.regime_multiplier
        dt_s = float(self.tick_seconds)
        paths: list[list[float]] = []
        terminals: list[float] = []
        agree_count = 0
        sign_ref = 1.0 if expected_move_bps >= 0 else -1.0
        # Per-step shock sigma, split evenly across tribes. Gives each tribe
        # its own noise draw without exploding the total variance.
        n_tribes = max(1, len(self.tribes))
        step_shock_sigma: list[float] = []
        prev_var = 0.0
        for s in steps_sigma_bps:
            cur_var = float(s) ** 2
            inc = max(0.0, cur_var - prev_var)
            step_shock_sigma.append(math.sqrt(inc / n_tribes))
            prev_var = cur_var

        for _path in range(n_paths):
            sim = state.snapshot()
            ts = sim.last_update or datetime.now(timezone.utc)
            cum_bps = 0.0
            path_curve: list[float] = []
            for step_i in range(n_steps):
                prev_ts = ts
                ts = ts + timedelta(seconds=dt_s)
                sim.advance_clock(ts)
                elapsed_s = (step_i + 1) * dt_s
                per_tribe_bps = 0.0
                shock_sig = step_shock_sigma[step_i] if step_i < len(step_shock_sigma) else 0.0
                for t in self.tribes:
                    v = t.project_vote(ts, sim)
                    hs = self._tribe_horizon_s.get(v.tribe_id, self.default_horizon_seconds)
                    factor = math.exp(-elapsed_s / hs)
                    signed = (1.0 if v.side.value == "buy" else
                              -1.0 if v.side.value == "sell" else 0.0)
                    contrib = signed * v.conviction * factor * t.weight
                    noise = self._rng.gauss(0.0, shock_sig) if shock_sig > 0 else 0.0
                    per_tribe_bps += contrib * effective_gain + noise
                # Same mean-reversion as the deterministic path so the fan
                # shape matches.
                if self.mean_reversion_rate > 0.0 and cum_bps != 0.0:
                    per_tribe_bps -= self.mean_reversion_rate * cum_bps
                cum_bps += per_tribe_bps
                path_curve.append(cum_bps)
                if sim.last_price is not None and sim.last_price > 0:
                    sim.last_price = sim.last_price * (1 + per_tribe_bps / 10_000.0)
            paths.append(path_curve)
            terminals.append(cum_bps)
            sign_path = 1.0 if cum_bps >= 0 else -1.0
            if sign_path == sign_ref:
                agree_count += 1
        agreement = agree_count / n_paths
        return paths, agreement, terminals

    def _gaussian_paths(self, steps_bps: list[float],
                        steps_sigma_bps: list[float]) -> list[list[float]]:
        """Fallback — gaussian noise around the deterministic curve."""
        n_paths = self.sample_paths_n
        n_steps = len(steps_bps)
        if n_paths <= 0 or n_steps == 0:
            return []
        # Per-step shock stddev from the differences of the cumulative sigma
        # curve. Assuming independent increments, sigma_cum²(t) = Σ σ_step².
        step_shock_sigma: list[float] = []
        prev_var = 0.0
        for s in steps_sigma_bps:
            cur_var = float(s) ** 2
            inc = max(0.0, cur_var - prev_var)
            step_shock_sigma.append(math.sqrt(inc))
            prev_var = cur_var
        paths: list[list[float]] = []
        for _ in range(n_paths):
            path: list[float] = []
            running = 0.0
            for i in range(n_steps):
                shock = self._rng.gauss(0.0, step_shock_sigma[i])
                running += (steps_bps[i] - (steps_bps[i - 1] if i > 0 else 0.0)) + shock
                path.append(running)
            paths.append(path)
        return paths
