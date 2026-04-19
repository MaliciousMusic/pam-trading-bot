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
from collections import deque
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
        # ── Adaptive Temporal Weighting state ──────────────────────────
        # Persistent shock buffer: per-tribe list of unit-variance Gaussian
        # draws, length n_steps. On each project() call the buffer slides
        # left (shocks[k] = shocks[k+1]) and only the last slot is redrawn
        # — this is the "True Markov Slide". A prediction for T+50 stays
        # coherent as it marches toward T+0.
        self._shock_buffer: dict[str, list[float]] = {}
        # Prediction ledger — circular buffer of the last `steps` frames.
        # Each frame: {"price": float, "curves": {tribe_id: list[float]}}.
        # Used at every tick to backtest what each tribe predicted h steps
        # ago against what actually happened, then nudge W[i][h] up/down.
        self._ledger: deque = deque(maxlen=steps)
        # Per-(tribe, horizon) performance weights. Initialized to 1.0.
        # Bounded in [_weight_min, _weight_max] via multiplicative update
        # W *= exp(lr * delta), where delta ∈ [-1, +1] scales with
        # (horizon_sigma − |error|) / horizon_sigma. Perfect call → +lr,
        # prediction 1σ off target → 0, wildly off → −lr.
        self._weights: dict[str, list[float]] = {
            t.spec.id: [1.0] * steps for t in tribes
        }
        # Lowered from 0.05 to 0.01 — at 0.05 a tribe went from neutral to
        # the 3.0 cap in ~40 ticks, meaning "lucky streaks" locked in before
        # being washed out. 0.01 requires sustained outperformance over
        # several hundred ticks to reach the cap: the system now has memory.
        self._weight_lr = 0.01
        self._weight_min = 0.1
        # Cap tightened from 3.0 → 1.5. Combined with the zero-sum column
        # re-normalization below, no tribe can have more than 1.5× the
        # influence of a neutral peer at any horizon.
        self._weight_max = 1.5

    def project(self, state: MarketState) -> Forecast:
        """Per-tribe isolated projection with Adaptive Temporal Weighting.

        Three stateful mechanisms layered on top of the per-tribe isolated sim:

          1. **Persistent shock buffer** — each tribe carries a length-N list
             of unit-Gaussian draws that *slides* one slot per tick. A shock
             used at step 49 this tick becomes step 48's shock next tick, so
             a wiggle projected 50 ticks out in the future stays coherent as
             it approaches T+0 (True Markov Slide).
          2. **Prediction ledger** — a circular buffer of the last N frames.
             At every tick we look back at what each tribe predicted h steps
             ago for the current tick, score it against the actual price
             move, and nudge that tribe's weight at horizon h up or down.
          3. **Performance-weighted consensus** — the thick line is no
             longer a simple sum but the weighted average of the 17 tribe
             paths, with `W[tribe_id][h]` set by the tribe's track record
             at that specific horizon. Tribe A can dominate the near future
             while Tribe B dominates the far future.
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

        # ── 1. Score the ledger against reality, update per-horizon weights ──
        # *Peer-relative* grading: the pass/fail threshold for horizon h is
        # the median absolute error of the 17 tribes at that horizon. Being
        # close to σ is no longer a free win; you have to beat the median
        # of your own council to earn a bonus. Only ~half the pack can
        # outperform the median at any tick → Competitive Normalizer.
        cur_price = start_ref.last_price
        if cur_price is not None and cur_price > 0 and self._ledger:
            frames = list(self._ledger)
            for k_back_minus_1, frame in enumerate(reversed(frames)):
                h_idx = k_back_minus_1  # 0 = most-recent frame (predicted 1 step ahead)
                if h_idx >= n_steps:
                    break
                old_price = frame.get("price")
                if not old_price or old_price <= 0:
                    continue
                actual_bps = (cur_price / old_price - 1.0) * 10_000.0
                # Collect per-tribe errors for this horizon, derive median.
                errs: dict[str, float] = {}
                for tid, curve in frame.get("curves", {}).items():
                    if h_idx >= len(curve):
                        continue
                    errs[tid] = abs(curve[h_idx] - actual_bps)
                if not errs:
                    continue
                sorted_errs = sorted(errs.values())
                m = len(sorted_errs)
                median_err = (sorted_errs[m // 2] if m % 2
                              else 0.5 * (sorted_errs[m // 2 - 1] + sorted_errs[m // 2]))
                norm = max(1e-6, median_err)
                for tid, err in errs.items():
                    # delta = +1 when err is zero, 0 when err equals median,
                    # −1 when err is 2× median (clamped beyond).
                    delta = (norm - err) / norm
                    if delta > 1.0:
                        delta = 1.0
                    elif delta < -1.0:
                        delta = -1.0
                    w_row = self._weights.setdefault(tid, [1.0] * n_steps)
                    if len(w_row) != n_steps:
                        w_row = [1.0] * n_steps
                    new_w = w_row[h_idx] * math.exp(self._weight_lr * delta)
                    if new_w < self._weight_min:
                        new_w = self._weight_min
                    elif new_w > self._weight_max:
                        new_w = self._weight_max
                    w_row[h_idx] = new_w
                    self._weights[tid] = w_row

            # ── Zero-Sum Column Re-normalization ──
            # After the bonus/malus pass, rescale each horizon column so that
            # Σ W[:,h] == n_tribes. Neutral baseline is therefore always 1.0
            # on average — one tribe rising forces others to fall. The
            # consensus becomes a pure alpha-weighted combination rather
            # than an inflated sum.
            tribe_ids_w = list(self._weights.keys())
            n_w = len(tribe_ids_w)
            if n_w > 0:
                for h in range(n_steps):
                    col = [
                        self._weights[tid][h]
                        for tid in tribe_ids_w
                        if h < len(self._weights[tid])
                    ]
                    s = sum(col)
                    if s > 0:
                        scale = n_w / s
                        for tid in tribe_ids_w:
                            row = self._weights[tid]
                            if h < len(row):
                                v = row[h] * scale
                                if v < self._weight_min:
                                    v = self._weight_min
                                elif v > self._weight_max:
                                    v = self._weight_max
                                row[h] = v

        # ── 2. Slide each tribe's persistent shock buffer by one slot ──
        # buffer stores unit-variance draws; multiplied by step_shock_sigma
        # below to get the actual per-step bps kick.
        for tribe in self.tribes:
            tid = tribe.spec.id
            buf = self._shock_buffer.get(tid)
            if buf is None or len(buf) != n_steps:
                buf = [self._rng.gauss(0.0, 1.0) for _ in range(n_steps)]
            else:
                # Slide left, redraw only the terminal slot.
                buf = buf[1:] + [self._rng.gauss(0.0, 1.0)]
            self._shock_buffer[tid] = buf

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
            shock_buf = self._shock_buffer.get(tribe.spec.id, [0.0] * n_steps)
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
                # Each tribe's curve is a *scenario* — "if this tribe's read
                # on the market dominated, here's where price goes". Pool
                # weight is applied later via W[tid][h] in the consensus
                # blend, not here, so small-weight tribes still produce
                # legibly-sized paths for the UI to render.
                vote_bps = signed * v.conviction * factor * sk * effective_gain
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
                # Stochastic shock — pulled from the persistent buffer, not
                # redrawn each tick. The unit-variance slot for step_i
                # slid in from slot step_i+1 last tick, so the wiggle
                # projected N ticks ago stays visually stable as it marches
                # toward now. Only the terminal slot is fresh each tick.
                shock_sig = step_shock_sigma[step_i] if step_i < len(step_shock_sigma) else 0.0
                if shock_sig > 0.0:
                    unit = shock_buf[step_i] if step_i < len(shock_buf) else 0.0
                    step_bps += unit * shock_sig
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

        # ── 3. Performance-weighted consensus ──
        # Consensus_h = Σ (Path_i,h · W_i,h · pool_weight_i) / Σ (W_i,h · pool_weight_i)
        # Pool weight gates flow share (a 1% tribe can't override a 20% tribe
        # just because its last few calls happened to land), while W_i,h is
        # the horizon-specific accuracy overlay the ledger is learning.
        pool_w = {t.spec.id: float(t.weight) for t in self.tribes}
        steps_bps: list[float] = []
        for h in range(n_steps):
            num = 0.0
            den = 0.0
            for tid, curve in zip(per_tribe_ids, per_tribe_curves):
                w_row = self._weights.get(tid)
                wv = (w_row[h] if w_row and h < len(w_row) else 1.0) * pool_w.get(tid, 0.0)
                num += curve[h] * wv
                den += wv
            steps_bps.append(num / den if den > 0 else 0.0)

        # Record this tick's frame for future ledger-based weight updates.
        self._ledger.append({
            "price": cur_price,
            "curves": {tid: list(curve) for tid, curve in zip(per_tribe_ids, per_tribe_curves)},
        })
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

