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
        # Per-tribe skill overlay. Set externally after construction (the
        # live wiring in run_bot_ui.py builds the calibrator after the
        # projector). When present, the projector scales each tribe's
        # contribution by `calibrator.skill_multiplier(tribe_id)` so tribes
        # with measured accuracy speak louder and chronically-wrong ones
        # get damped. Defaults to None → no-op (projector ignores skill).
        self.calibrator = None
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
        """Per-tribe isolated projection.

        Each tribe runs forward in its own sim snapshot — it sees only the
        price movement its own pressure creates, not the aggregate echo. At
        combine time the 17 tribe curves are summed (weight and skill are
        already baked into each curve) to produce the deterministic "thick"
        forecast line. The individual tribe curves double as the wiggly fan
        the UI draws — no gaussian noise layer, just the honest per-archetype
        futures.
        """
        start_ref = state.snapshot()
        start_ts = start_ref.last_update or datetime.now(timezone.utc)
        effective_gain = self.bps_per_unit_pressure * self.regime_multiplier
        dt_s = float(self.tick_seconds)
        n_steps = self.steps

        skill: dict[str, float] = {}
        if self.calibrator is not None:
            for t in self.tribes:
                try:
                    skill[t.spec.id] = float(self.calibrator.skill_multiplier(t.spec.id))
                except Exception:
                    skill[t.spec.id] = 1.0

        # Per-tribe cumulative bps curves. Each tribe projects in isolation —
        # its simulated price trajectory reflects only its own contribution,
        # breaking the echo-chamber feedback from the old aggregate-first
        # projection.
        per_tribe_curves: list[list[float]] = []
        per_tribe_ids: list[str] = []
        # Per-step stochastic shock sigma shared by every tribe. Derived from
        # the backscorer's residual stddev (via `_sigma_per_step`), then
        # converted to per-step increments. This is the ingredient that makes
        # each tribe's forecast an actual wiggly curve rather than a straight
        # line — the tribe's deterministic read is a drift, and the noise is
        # that tribe's forecast uncertainty at each horizon. With it, the
        # paths look like plausible futures; without it, they'd look like
        # rulers.
        sigma_cum = self._sigma_per_step(n_steps)
        n_tribes = max(1, len(self.tribes))
        step_shock_sigma: list[float] = []
        prev_var = 0.0
        for s in sigma_cum:
            cur_var = float(s) ** 2
            inc = max(0.0, cur_var - prev_var)
            # Split variance evenly across tribes so the sum of 17
            # independent per-tribe shocks reproduces the target horizon
            # sigma instead of sqrt(N) × target.
            step_shock_sigma.append(math.sqrt(inc / n_tribes))
            prev_var = cur_var
        # Decayed votes per step — collected from the isolated per-tribe walks
        # so we still have an aggregate object per step for confidence math.
        decayed_by_step: list[list] = [[] for _ in range(n_steps)]

        for tribe in self.tribes:
            sk = skill.get(tribe.spec.id, 1.0)
            w = float(tribe.weight)
            sim = state.snapshot()
            ts = start_ts
            prev_ts = ts
            cum_bps = 0.0
            curve: list[float] = []
            for step_i in range(n_steps):
                prev_ts = ts
                ts = ts + timedelta(seconds=dt_s)
                sim.advance_clock(ts)
                elapsed_s = (step_i + 1) * dt_s
                v = tribe.project_vote(ts, sim)
                hs = self._tribe_horizon_s.get(v.tribe_id, self.default_horizon_seconds)
                factor = math.exp(-elapsed_s / hs)
                combo = factor * sk
                new_conv = max(0.0, min(1.0, v.conviction * combo))
                decayed_by_step[step_i].append(replace(
                    v, conviction=new_conv,
                    buy_frac=v.buy_frac * combo,
                    sell_frac=v.sell_frac * combo,
                ))
                signed = (1.0 if v.side.value == "buy" else
                          -1.0 if v.side.value == "sell" else 0.0)
                # Tribe's own contribution to the consensus: signed conviction
                # scaled by horizon decay, skill, pool weight, and the gain
                # kernel. This is what this tribe alone would push the price by.
                vote_bps = signed * v.conviction * factor * sk * w * effective_gain
                event_bps = step_event_bps(
                    sim.pending_events, [tribe],
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
                # Stochastic shock — the tribe's per-step forecast error.
                # Drawn from its own RNG so two tribes with the same drift
                # still produce distinct wiggly futures.
                shock_sig = step_shock_sigma[step_i] if step_i < len(step_shock_sigma) else 0.0
                if shock_sig > 0.0:
                    step_bps += self._rng.gauss(0.0, shock_sig)
                if self.mean_reversion_rate > 0.0 and cum_bps != 0.0:
                    step_bps -= self.mean_reversion_rate * cum_bps
                cum_bps += step_bps
                curve.append(cum_bps)
                if sim.last_price is not None and sim.last_price > 0:
                    sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                    sim.prices.add(ts, sim.last_price)
                if sim.last_funding_rate is not None:
                    sim.last_funding_rate *= 0.995
            per_tribe_curves.append(curve)
            per_tribe_ids.append(tribe.spec.id)

        # Consensus thick line = simple sum across tribes. Weight and skill
        # are already inside each curve, so summing gives the weighted chorus.
        steps_bps: list[float] = []
        for i in range(n_steps):
            steps_bps.append(sum(curve[i] for curve in per_tribe_curves))
        step_bps_list: list[float] = []
        prev_cum = 0.0
        for cum in steps_bps:
            step_bps_list.append(cum - prev_cum)
            prev_cum = cum

        # Per-step aggregate for the confidence computation. The decayed
        # votes came from the isolated walks (each tribe's vote at its own
        # step timestamp), so the aggregate's uncertainty measure is still
        # a fair "how coherent was the council at step i" signal.
        step_aggs = [
            aggregate_votes(decayed_by_step[i], start_ts + timedelta(seconds=(i + 1) * dt_s))
            for i in range(n_steps)
        ]

        kernel = [self.confidence_decay ** i for i in range(n_steps)]
        k_sum = sum(kernel) or 1.0
        expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))
        signal_quality = sum(
            (1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)
        ) / k_sum
        abs_activity = sum(abs(b) * w for b, w in zip(step_bps_list, kernel))
        directional = abs(expected_move_bps) / abs_activity if abs_activity > 0 else 0.0
        final_conf = max(0.0, min(1.0, signal_quality * directional))

        steps_sigma_bps = self._sigma_per_step(n_steps)

        # Directional agreement — fraction of tribe terminals sharing the
        # sign of the consensus. With 17 archetypes this is a sharper read
        # than a 0/1 gaussian split.
        terminals = [c[-1] if c else 0.0 for c in per_tribe_curves]
        sign_ref = 1.0 if expected_move_bps >= 0 else -1.0
        if terminals:
            agree = sum(1 for t in terminals if (1.0 if t >= 0 else -1.0) == sign_ref)
            directional_agreement = agree / len(terminals)
        else:
            directional_agreement = None

        logger.debug(
            "projector: {} steps, move {:+.1f}bps, signal={:.2f} dir={:.2f} "
            "conf={:.2f} σ₀={:.1f} tribes={}",
            n_steps, expected_move_bps, signal_quality, directional, final_conf,
            steps_sigma_bps[0] if steps_sigma_bps else 0.0, len(per_tribe_curves),
        )
        return Forecast(
            ts=start_ts,
            steps=step_aggs,
            expected_move_bps=expected_move_bps,
            confidence=final_conf,
            horizon_seconds=n_steps * self.tick_seconds,
            steps_bps=steps_bps,
            steps_sigma_bps=steps_sigma_bps,
            sample_paths_bps=per_tribe_curves,
            directional_agreement=directional_agreement,
            sample_terminals_bps=terminals,
            sample_paths_tribe_ids=per_tribe_ids,
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

