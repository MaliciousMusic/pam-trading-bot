"""Chess projector — forward-simulate N ticks ahead via Agent-Based Recursion.

Each tribe is its own agent with a deterministic Behavioral DNA function
(see `behavioral_dna.py`). At step h the tribe receives:
    State_{i,h-1}, Delta_{i,h-1}, regime, perceived_shocks_{i,h}
and returns Delta_{i,h} → State_{i,h} = State_{i,h-1} + Delta_{i,h}.

Information Asymmetry — not Monte Carlo noise — is the source of
divergence between paths:
  • A sparse schedule of typed World Shocks (macro / micro / sentiment /
    liquidity) is sampled once per projection from a regime-dependent
    distribution.
  • Each tribe category has a Latency (lag between shock firing and the
    tribe perceiving it) and a Visibility map (per-kind multiplier).
  • A liquidity shock hits MM at h=0 (immediate spread-widening spike),
    arb at h=1 (begins fading toward fair value), TA at h=2-3 (only via
    prev_delta-driven momentum), retail at h=5+ (panic threshold trips,
    quadratic chase kicks in). This cascade is the alpha — the
    "Microstructure Lead" lets MM gap before arb can reprice.

Per-step:
  1. Advance the shadow clock by tick_seconds; stale window entries age out.
  2. DNA(prev_state, prev_delta, regime, perceived[h]) → step_bps.
  3. Real scheduled events (`step_event_bps`) layered on top.
  4. Depth-scaled by side-of-book liquidity.
  5. Soft-Limit Anchor (mean_reversion_rate ρ) pulls cumulative drift
     back toward zero — prevents the recursive paths from exploding.

The decayed-vote shadow used for confidence math (`aggregate_votes`) is
built from each tribe's *initial* vote at h=0 multiplied by the per-step
horizon kernel, so scalpers still fade out within the first few steps
and swing traders persist across the full 10 minutes — but the path
shape is governed entirely by the DNA, not by per-step re-voting.

Adaptive Temporal Weighting (prediction ledger → per-(tribe, horizon)
weight matrix) and the Regime-aware pool weight overlay are unchanged.
"""
from __future__ import annotations

import math
import random
from collections import deque
from msgspec.structs import replace
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..aggregator.voting import aggregate_votes
from ..core.types import Forecast, Side
from ..tribes.market_state import MarketState
from ..tribes.tribe import Tribe
from .behavioral_dna import (
    DNAContext,
    dna_for,
    generate_shock_schedule,
    perceive_for_tribe,
    profile_for,
)
from .event_wave import step_event_bps


# ── Regime-dependent pool weight overlay ─────────────────────────────────
# ω_{i,(regime)} = ω_{i,(base)} · f(regime, category_i)
# The static flow-share weight (`tribe.weight`) tells us how much capital a
# tribe normally pushes around. But capital isn't regime-agnostic: market
# makers *pull* liquidity in a cascade, arb desks *lean in*, retail panics,
# TA traders stare at broken patterns. The multiplier below retunes each
# tribe's voice in the consensus to reflect its observed behavior under
# the current tape rather than its average-day footprint.
_TRIBE_CATEGORY: dict[str, str] = {
    "mm_algo": "mm",
    "retail_grid_bot": "mm",  # grid bots post limit orders like passive MMs
    "basis_arb": "arb",
    "cross_venue_arb": "arb",
    "contrarian_funding_fader": "arb",
    "retail_degen_leveraged": "retail",
    "retail_app_newbie": "retail",
    "copy_trader": "retail",
    "ta_day_trader": "ta",
    "swing_trend_follower": "ta",
    "liquidation_hunter": "event",
    "news_fader": "event",
    "options_hedger_proxy": "hedger",
    "leverage_cycle": "hedger",
    "microstructure_prober": "microstructure",
    "mev_frontrunner": "mev",
    "passive_whale_twap": "whale",
}
# Rows: regime label (see regime.classifier.RegimeLabel).
# Columns: tribe category.
# Design notes:
#   - CASCADE: MM pull liquidity (0.35), arb desks lean hard into dislocation
#     (1.60), retail panic-adds to the move (1.45), event tribes lit up (1.55),
#     hedgers step in (1.30), TA largely irrelevant inside the shock (1.15).
#   - TREND: MM less relevant (inventory pinned to one side), TA + retail
#     ride the trend, hedgers keep steady.
#   - CHOP: MM dominate (1.30), arb still present, retail fades, TA patterns
#     are false-signal-heavy (0.75).
#   - NORMAL: neutral baseline — every category at 1.0.
# ── Phase 3b: Dissension Veto ────────────────────────────────────────────
# A tribe's Vote carries `buy_frac` and `sell_frac` — the fraction of its
# active membership voting each way. When both sides are close in magnitude
# (say 45% buy / 55% sell) the "net" vote represents internal disagreement
# rather than a directional read; averaging it into the council is
# averaging noise. Any vote where min/max > _DISSENSION_RATIO_THRESHOLD is
# dropped to abstain — we'd rather have fewer loud voices than many split ones.
_DISSENSION_RATIO_THRESHOLD: float = 0.82
# Boundary examples (lo/hi ratio):
#   45/55 → 0.818 — passes (vote kept, boundary case just under threshold)
#   46/54 → 0.852 — vetoed (vote dropped to abstain, too internally split)
#   50/50 → 1.000 — always vetoed (perfect split, no directional signal)


def _is_dissension(vote) -> bool:
    b = max(0.0, float(vote.buy_frac))
    s = max(0.0, float(vote.sell_frac))
    if b <= 1e-9 and s <= 1e-9:
        return False   # pure abstain — already silent
    hi = max(b, s)
    lo = min(b, s)
    if hi <= 1e-9:
        return False
    return (lo / hi) >= _DISSENSION_RATIO_THRESHOLD


_REGIME_CATEGORY_MULT: dict[str, dict[str, float]] = {
    "normal":       {"mm": 1.00, "arb": 1.00, "retail": 1.00, "ta": 1.00, "event": 1.00, "hedger": 1.00, "microstructure": 1.15, "mev": 1.10, "whale": 1.00},
    "chop":         {"mm": 1.30, "arb": 1.15, "retail": 0.70, "ta": 0.75, "event": 0.80, "hedger": 0.90, "microstructure": 1.40, "mev": 0.80, "whale": 1.05},
    "trend_up":     {"mm": 0.85, "arb": 1.05, "retail": 1.25, "ta": 1.35, "event": 1.10, "hedger": 1.10, "microstructure": 0.95, "mev": 1.15, "whale": 1.20},
    "trend_down":   {"mm": 0.85, "arb": 1.05, "retail": 1.25, "ta": 1.35, "event": 1.10, "hedger": 1.10, "microstructure": 0.95, "mev": 1.15, "whale": 1.20},
    # Cascade is MEV's day job — the liquidation tape is pure extraction
    # territory. Whales, by contrast, pause/slow their schedule during
    # visible dislocation so they aren't left holding the bag; the
    # multiplier dials them down rather than up.
    "cascade_up":   {"mm": 0.35, "arb": 1.60, "retail": 1.45, "ta": 1.15, "event": 1.55, "hedger": 1.30, "microstructure": 1.25, "mev": 1.70, "whale": 0.60},
    "cascade_down": {"mm": 0.35, "arb": 1.60, "retail": 1.45, "ta": 1.15, "event": 1.55, "hedger": 1.30, "microstructure": 1.25, "mev": 1.70, "whale": 0.60},
}


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
        # Regime label pushed in by the engine each tick. Drives the
        # category-level pool-weight overlay below — see _REGIME_CATEGORY_MULT.
        self.regime_label: str = "normal"
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
        # RNG drives only the World Shock schedule sampling (a handful of
        # draws per project() call), not per-tribe per-step noise — the
        # ABM's wiggle comes from DNA × shock interaction, not from
        # independent Gaussians.
        self._rng = random.Random(rng_seed)
        # ── Adaptive Temporal Weighting state ──────────────────────────
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
        # Widened from [0.1, 1.5] → [0.05, 2.5]. The tight cap was masking
        # specialist edges: a tribe that's right 85% of the time at h=1
        # shouldn't be capped at 1.5× influence when its peer-relative
        # advantage at that horizon is 3-4×. Floor dropped to 0.05 so
        # chronic underperformers can be almost-fully-silenced (and their
        # conviction re-purposed as contrarian signal via Task 2 below).
        self._weight_min = 0.05
        self._weight_max = 2.5
        # ── Phase 1a: Cross-Correlation Squasher ──────────────────────
        # Rolling per-tribe signed-conviction history. After enough samples,
        # pairwise Pearson corr detects redundancy clusters — if three TA
        # tribes always vote in lockstep, treating them as three independent
        # voices triple-counts the same signal. The haircut below scales
        # down the pool weight of tribes whose conviction series correlates
        # strongly with peers so the combined council voice reflects
        # *information*, not repeated broadcasts of the same opinion.
        self._corr_window = 64          # ~5min at 5s ticks
        self._corr_min_samples = 32
        self._corr_threshold = 0.8
        # Max haircut at full-redundancy: cap at 50% — redundancy should
        # dampen, not silence, a tribe. A tribe that's a perfect copy of
        # another still contributes a small amount of de-noising.
        self._corr_max_haircut = 0.5
        self._conviction_history: dict[str, deque] = {
            t.spec.id: deque(maxlen=self._corr_window) for t in tribes
        }
        self._last_capitulation_triggers: int = 0
        # Validate that the capitulation trio exists — if any of these IDs
        # are missing the capitulation logic silently no-ops, which would
        # disable a core contrarian-mass amplification pathway. Warn loud.
        _known_tids = {t.spec.id for t in tribes}
        for _cap_tid in ("liquidation_hunter", "retail_grid_bot", "capitulation_buyer"):
            if _cap_tid not in _known_tids:
                from loguru import logger as _cap_logger
                _cap_logger.warning(
                    "[projector] capitulation tribe '{}' missing from tribe set; "
                    "capitulation multiplier will not fire", _cap_tid,
                )
        # ── Phase 3a: regime smoothing ────────────────────────────────
        # Track the previous regime label + timestamp of last change so
        # the category multiplier table can be blended rather than
        # step-function switched on each tick.
        self._prev_regime_label: str = "normal"
        self._regime_change_ts: datetime | None = None
        self._regime_smoothing_s: float = 7.5   # center of the sigmoid
        # Task 4e / PIECE 3 — top-quartile harvest inflation. Each contrarian
        # tribe in the scorer's top-25% composite cohort has its pool-weight
        # contribution to harvest_mass scaled by this factor. 1.0 = no extra
        # boost (original behavior); 2.0 = double. Wired from config via
        # run_bot_ui at construction and mutable afterward.
        self._harvest_top_quartile_boost: float = 2.0

    def project(self, state: MarketState) -> Forecast:
        """Agent-Based Recursive projection with Adaptive Temporal Weighting.

        Each tribe's path is a deterministic execution of its category's
        Behavioral DNA function (see `behavioral_dna.py`), driven by:
          • its initial directional intent + base strength (locked at h=0
            from `tribe.project_vote(start_ts, state)`),
          • its perceived World Shock pipeline (latency- and visibility-
            filtered from a single shared sparse schedule),
          • the recursive prior step's (state, delta).

        Three stateful mechanisms layered on top of the recursive sim:

          1. **Typed World Shocks** — sparse schedule sampled per call from
             a regime-dependent kind distribution. Each tribe category
             perceives them at its own latency and visibility, so the
             same liquidity event hits MM at h=0 and retail at h=5+.
          2. **Prediction ledger** — circular buffer of the last N frames.
             At every tick we look back at what each tribe predicted h
             steps ago, score it against the actual price move, and nudge
             that tribe's weight at horizon h up or down.
          3. **Performance-weighted consensus** — the thick line is the
             weighted average of the per-tribe paths, with `W[tribe_id][h]`
             set by the tribe's track record at that specific horizon.
        """
        start_ref = state.snapshot()
        start_ts = start_ref.last_update or datetime.now(timezone.utc)
        effective_gain = self.bps_per_unit_pressure * self.regime_multiplier
        dt_s = float(self.tick_seconds)
        n_steps = self.steps

        # Per-(tribe, horizon_step) specialist skill. Task 1 + 4b: instead
        # of a scalar multiplier per tribe, pull a vector of n_steps values
        # where each slot is the tribe's skill at THAT horizon bucket (the
        # calibrator buckets short ≤60s, medium 60–300s, long >300s). This
        # lets microstructure_prober's hot 1-step edge amplify at h=0 while
        # its weaker 30-step edge stays muted, and vice-versa for basis_arb.
        # Fallback to scalar legacy multiplier if the bucket has too few
        # samples (horizon_min_samples threshold). Also kept for legacy call
        # sites that use the scalar form.
        skill: dict[str, float] = {}
        skill_per_h: dict[str, list[float]] = {}
        if self.calibrator is not None:
            horizon_s_by_step = [float((h + 1) * dt_s) for h in range(n_steps)]
            for t in self.tribes:
                try:
                    scalar = float(self.calibrator.skill_multiplier(t.spec.id))
                except Exception:
                    scalar = 1.0
                skill[t.spec.id] = scalar
                vec = []
                for hs in horizon_s_by_step:
                    try:
                        vec.append(float(self.calibrator.skill_multiplier(
                            t.spec.id, horizon_s=hs)))
                    except Exception:
                        vec.append(scalar)
                skill_per_h[t.spec.id] = vec
        else:
            for t in self.tribes:
                skill[t.spec.id] = 1.0
                skill_per_h[t.spec.id] = [1.0] * n_steps

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

        # ── 2. Generate the shared World Shock schedule ──
        # A single sparse list of typed shocks for this projection. Each
        # tribe will perceive its own filtered + delayed view of this
        # schedule via `perceive_for_tribe` below. Shock magnitude scales
        # with the projector's terminal sigma so cascades hit harder
        # than calm regimes.
        sigma_cum = self._sigma_per_step(n_steps)
        sigma_terminal = sigma_cum[-1] if sigma_cum else self.default_sigma_bps
        world_shocks = generate_shock_schedule(
            n_steps,
            regime=self.regime_label,
            sigma_terminal_bps=sigma_terminal,
            rng=self._rng,
            target_count=4,
        )

        # ── 3. Lock each tribe's initial directional intent at h=0 ──
        # The tribe's "opening read" of the market — used as signed_intent
        # and base_strength inputs to the DNA. We deliberately do NOT
        # re-vote on the simulated future state at every step (that was
        # the old "ball of yarn" feedback); the DNA is the response
        # function that evolves the path from this opening read.
        # `decayed_by_step` still receives a per-step horizon-decayed copy
        # so the confidence aggregate keeps its meaning (scalpers fade,
        # swing traders persist).
        #
        # Phase 3b — Dissension Veto: if a tribe's buy_frac and sell_frac
        # are too close in magnitude, the tribe's active membership is
        # split 45/55 (or closer) on direction — the "net" signal is
        # mostly noise. Drop the vote to abstain so it doesn't get
        # averaged into the council signal.
        # Task 2 — TA Cemetery / Contrarian Inversion, now surgical on the
        # (tribe × regime × horizon) cube. A tribe can be reliably-worse-
        # than-chance in cascade-down at 5m while remaining skilled in
        # trend-up at 1m — the 3-D mask catches the bad slice without
        # punishing the good one. Falls back to the scalar candidate test
        # when the cube mask is False (not-yet-enough samples) so existing
        # global losers still get flipped during cold-start.
        contrarian_tids: set[str] = set()
        if self.calibrator is not None:
            projector_horizon_s = float(self.default_horizon_seconds)
            for t in self.tribes:
                try:
                    cube_hit = self.calibrator.contrarian_mask(
                        t.spec.id, self.regime_label, projector_horizon_s,
                    )
                    scalar_hit = self.calibrator.is_contrarian_candidate(t.spec.id)
                    if cube_hit or scalar_hit:
                        contrarian_tids.add(t.spec.id)
                except Exception:
                    pass

        initial_votes: dict[str, tuple] = {}
        for tribe in self.tribes:
            v0 = tribe.project_vote(start_ts, state)
            if _is_dissension(v0):
                # Force to neutral — still passes through the aggregator,
                # but as an abstention rather than a split-direction wash.
                v0 = replace(v0, side=Side.ABSTAIN,
                             conviction=0.0, buy_frac=0.0, sell_frac=0.0,
                             reason=v0.reason + " | DISSENSION_VETO")
            # Task 2: flip reliably-wrong tribes. We swap Side (not just the
            # numeric `signed`) so every downstream consumer — aggregator,
            # redundancy history, and pool consensus — sees the inverted
            # vote consistently.
            if tribe.spec.id in contrarian_tids and v0.side is not Side.ABSTAIN:
                flipped_side = Side.SELL if v0.side is Side.BUY else Side.BUY
                v0 = replace(v0, side=flipped_side,
                             buy_frac=v0.sell_frac, sell_frac=v0.buy_frac,
                             reason="[FLIPPED] " + v0.reason)
            signed = (1.0 if v0.side.value == "buy" else
                      -1.0 if v0.side.value == "sell" else 0.0)
            sk0 = skill.get(tribe.spec.id, 1.0)
            base_strength_pre = v0.conviction * sk0 * effective_gain
            initial_votes[tribe.spec.id] = (v0, signed, base_strength_pre)
            # Phase 1a — record signed conviction for the redundancy
            # correlation window. Abstain → 0 (contributes zero variance).
            signed_conv = signed * v0.conviction
            hist = self._conviction_history.setdefault(
                tribe.spec.id, deque(maxlen=self._corr_window))
            hist.append(signed_conv)

        # Phase 1a — Cross-Correlation Squasher. Build per-tribe redundancy
        # factor from the rolling conviction history. A tribe whose series
        # correlates > threshold with many peers contributes mostly
        # redundant information; haircut its pool weight proportionally.
        redundancy: dict[str, float] = self._compute_redundancy(
            [t.spec.id for t in self.tribes])

        # Phase 3a — sigmoid blend between previous regime's category
        # multiplier table and current one. Captures the realistic fact
        # that participation doesn't flip instantly when the classifier
        # label changes — MMs take a few seconds to pull quotes, arb desks
        # take a few seconds to adjust, etc.
        if self.regime_label != self._prev_regime_label:
            self._regime_change_ts = start_ts
            prev_label_for_blend = self._prev_regime_label
            self._prev_regime_label = self.regime_label
        else:
            prev_label_for_blend = self._prev_regime_label
        regime_blend_alpha = self._regime_blend_alpha(start_ts)

        # ── 4. Pre-compute each tribe's perceived shock pipeline ──
        # Latency shifts h_origin → h_origin + latency_steps; visibility
        # multiplies magnitude per-kind (zero = blind to that kind).
        # Result: perceived_per_tribe[tid][h] = list of WorldShock the
        # tribe sees at horizon h, with `kind` preserved so DNA can react
        # differently to e.g. a liquidity vs. macro shock.
        perceived_per_tribe: dict[str, list[list]] = {}
        for tribe in self.tribes:
            cat = _TRIBE_CATEGORY.get(tribe.spec.id, "ta")
            perceived_per_tribe[tribe.spec.id] = perceive_for_tribe(
                world_shocks, profile_for(cat), n_steps=n_steps,
            )

        # ── 5. Per-tribe recursive walk via Behavioral DNA ──
        per_tribe_curves: list[list[float]] = []
        per_tribe_ids: list[str] = []
        decayed_by_step: list[list] = [[] for _ in range(n_steps)]

        # Phase 2-lite — precompute the step-time axis once per project()
        # call so the inner tribe × step loop below doesn't re-derive
        # `ts + timedelta` or re-call `ts.timestamp()` on every iteration.
        # Profiling showed ~18k ts.timestamp() calls per tick before this
        # cache (advance_clock's 13 series trims × 24 tribes × 50 steps +
        # step_event_bps's 2 per step). Lifting the cache here keeps the
        # *semantics* identical — datetime arithmetic and timestamps are
        # pure functions of start_ts + step index — while collapsing the
        # Python-level call count by ~360×.
        step_timestamps: list[datetime] = []
        step_epochs: list[float] = []
        _cur = start_ts
        for _h in range(n_steps):
            _cur = _cur + timedelta(seconds=dt_s)
            step_timestamps.append(_cur)
            step_epochs.append(_cur.timestamp())
        prev_epochs: list[float] = [start_ts.timestamp()] + step_epochs[:-1]

        for tribe in self.tribes:
            tid = tribe.spec.id
            cat = _TRIBE_CATEGORY.get(tid, "ta")
            dna_fn = dna_for(cat)
            v0, signed, base_strength_pre = initial_votes[tid]
            perceived = perceived_per_tribe[tid]
            hs = self._tribe_horizon_s.get(tid, self.default_horizon_seconds)
            sim = state.snapshot()
            cum_bps = 0.0
            prev_delta = 0.0
            curve: list[float] = []
            for step_i in range(n_steps):
                ts = step_timestamps[step_i]
                t_now = step_epochs[step_i]
                t_prev = prev_epochs[step_i]
                sim._advance_clock_epoch(t_now, ts)
                elapsed_s = (step_i + 1) * dt_s
                # Horizon decay — scalpers fade out fast, swing traders
                # carry through the full window. Same kernel as before.
                decay = math.exp(-elapsed_s / hs)
                base_strength = base_strength_pre * decay
                # Decayed-vote shadow for the confidence aggregate.
                new_conv = max(0.0, min(1.0, v0.conviction * decay))
                decayed_by_step[step_i].append(replace(
                    v0, conviction=new_conv,
                    buy_frac=v0.buy_frac * decay,
                    sell_frac=v0.sell_frac * decay,
                ))
                # ── DNA: deterministic recursive step ──
                ctx = DNAContext(
                    prev_state_bps=cum_bps,
                    prev_delta_bps=prev_delta,
                    regime=self.regime_label,
                    perceived=perceived[step_i] if step_i < len(perceived) else [],
                    signed_intent=signed,
                    base_strength=base_strength,
                    h=step_i,
                )
                dna_delta = dna_fn(ctx)
                # Real scheduled events (separate from the random world
                # shocks) — preserves the existing event-wave overlay.
                event_bps = step_event_bps(
                    sim.pending_events, [tribe],
                    t_prev, t_now,
                )
                total_bps = dna_delta + event_bps
                # Depth scaling — thin books amplify pushes.
                side_depth = (
                    sim.ask_depth_5bps_usd if total_bps >= 0
                    else sim.bid_depth_5bps_usd
                )
                if side_depth and side_depth > 0:
                    scale = self.depth_ref_usd / max(side_depth, self.depth_floor_usd)
                else:
                    scale = 1.0
                step_bps = total_bps * scale
                # Soft-Limit Anchor (ρ) — pulls the recursive path back
                # toward zero so a runaway DNA + shock combination can't
                # explode the simulation.
                if self.mean_reversion_rate > 0.0 and cum_bps != 0.0:
                    step_bps -= self.mean_reversion_rate * cum_bps
                cum_bps += step_bps
                prev_delta = step_bps
                curve.append(cum_bps)
                if sim.last_price is not None and sim.last_price > 0:
                    sim.last_price = sim.last_price * (1 + step_bps / 10_000.0)
                    sim.prices._add_epoch(t_now, sim.last_price)
                if sim.last_funding_rate is not None:
                    sim.last_funding_rate *= 0.995
            per_tribe_curves.append(curve)
            per_tribe_ids.append(tid)

        # ── 6. Performance-weighted consensus ──
        # Consensus_h = Σ (Path_i,h · W_i,h · pool_weight_i) / Σ (W_i,h · pool_weight_i)
        # Pool weight gates flow share (a 1% tribe can't override a 20% tribe
        # just because its last few calls happened to land), while W_i,h is
        # the horizon-specific accuracy overlay the ledger is learning.
        # Regime-aware pool weight: ω_i_eff = ω_i_base · f(regime, category_i).
        # The base flow-share is what each tribe pushes around on an average
        # day; the overlay retunes who gets amplified or throttled under the
        # current tape. In a cascade, MM tribes drop to 0.35× while arb and
        # event tribes rise to 1.55–1.60× — the consensus reflects who is
        # *actually* trading, not who nominally holds inventory.
        #
        # Phase 3a — the category multiplier is a sigmoid-blend between
        # previous and current regime so weights don't jitter on a noisy
        # classifier flapping between `normal` and `chop`.
        # Phase 1a — redundancy haircut applied on top: a tribe whose
        # conviction series correlates heavily with peers gets its pool
        # weight damped, preventing any single cluster (3 TA tribes voting
        # identically) from triple-counting the same information.
        cur_tbl = _REGIME_CATEGORY_MULT.get(self.regime_label) \
            or _REGIME_CATEGORY_MULT["normal"]
        prev_tbl = _REGIME_CATEGORY_MULT.get(prev_label_for_blend) \
            or _REGIME_CATEGORY_MULT["normal"]
        pool_w: dict[str, float] = {}
        for t in self.tribes:
            cat = _TRIBE_CATEGORY.get(t.spec.id, "ta")
            mult_cur = cur_tbl.get(cat, 1.0)
            mult_prev = prev_tbl.get(cat, 1.0)
            mult = mult_prev + regime_blend_alpha * (mult_cur - mult_prev)
            haircut = 1.0 - self._corr_max_haircut * redundancy.get(t.spec.id, 0.0)
            pool_w[t.spec.id] = float(t.weight) * mult * haircut

        # ── Capitulation Multiplier ───────────────────────────────────
        # When the two cascade-follower losers (liquidation_hunter +
        # retail_grid_bot) are both in contrarian mode AND post-flip
        # voting BUY with high conviction — i.e. their pre-flip signal
        # was a high-conviction panic-SELL into a cascade they're
        # losing on — hand the microphone to capitulation_buyer, the
        # tribe architecturally designed to fade exactly this. Pure
        # weight reallocation, no new strategy. Produces Mean Reversion
        # Long via the existing harvest path.
        LOSER_TIDS = ("liquidation_hunter", "retail_grid_bot")
        CAP_TID = "capitulation_buyer"
        cap_triggers = 0
        if CAP_TID in pool_w:
            for loser_tid in LOSER_TIDS:
                if loser_tid not in contrarian_tids:
                    continue
                iv = initial_votes.get(loser_tid)
                if iv is None:
                    continue
                v_loser = iv[0]
                if v_loser.side is Side.BUY and v_loser.conviction >= 0.5:
                    cap_triggers += 1
            if cap_triggers >= 2:
                pool_w[CAP_TID] *= 3.0
            elif cap_triggers == 1:
                pool_w[CAP_TID] *= 1.5
        self._last_capitulation_triggers = cap_triggers

        # ── Task 4d: TA Family Redundancy Cap ─────────────────────────
        # The 5 TA tribes (4 day-traders + swing_trend_follower) read the
        # same RSI/MA/MACD textbook. Even after per-pair correlation
        # haircuts they still form a voting bloc: each tick they push
        # roughly the same direction, and the council hears the same
        # signal five times. Cap their COMBINED pool weight at 20% of the
        # total. Beyond 20% the whole family is scaled down proportionally
        # — we don't silence TA (chart-watchers do move real flow), we
        # just prevent them from drowning out the other 15 archetypes.
        ta_tids = [tid for tid in pool_w if _TRIBE_CATEGORY.get(tid) == "ta"]
        if ta_tids:
            ta_sum = sum(pool_w[tid] for tid in ta_tids)
            total_sum = sum(pool_w.values())
            if total_sum > 0 and ta_sum > 0:
                ta_share = ta_sum / total_sum
                ta_cap = 0.20
                if ta_share > ta_cap:
                    # Solve for scale s s.t.  (s·ta_sum) / (s·ta_sum + non_ta_sum) = ta_cap
                    # ⇒ s = ta_cap · non_ta_sum / ((1 − ta_cap) · ta_sum)
                    non_ta_sum = total_sum - ta_sum
                    scale = (ta_cap * non_ta_sum) / max(1e-12, (1.0 - ta_cap) * ta_sum)
                    for tid in ta_tids:
                        pool_w[tid] *= scale

        # ── Task 4e: Harvest Mass Boost ─────────────────────────────────
        # When a large slice of pool weight sits in the 'Contrarian-Flip'
        # zone (chronically-wrong tribes whose votes we now invert), the
        # council is effectively harvesting alpha from dumb money. The raw
        # weighted-consensus path already rotates these votes, but it does
        # not reflect the *size* of the opportunity — a loud-and-wrong
        # bloc is a higher-capacity fade than a quiet-and-wrong one.
        # Scale the final cumulative path by  1 + harvest_scale × ratio,
        # where ratio is the share of total pool weight that is currently
        # flagged contrarian. Cap the boost by construction via the scale
        # constant (0.5 → max 1.5×), and only apply when some contrarian
        # mass actually exists.
        harvest_scale = 0.5
        total_mass = sum(pool_w.values())
        # Top-quartile boost — tribes whose shadow-ledger composite score
        # sits in the top 25% get their contrarian harvest contribution
        # doubled. The projector asks the attached calibrator for the scorer;
        # falls back to no boost when the scorer isn't attached yet (cold
        # start, < min_trades on everyone). Default factor comes from config
        # but is hard-stopped at 4× here so a misconfig can't explode mass.
        top_quartile: set[str] = set()
        try:
            scorer = getattr(self.calibrator, "_trading_scorer", None)
            if scorer is not None:
                top_quartile = set(scorer.top_quartile_tribes())
        except Exception:
            top_quartile = set()
        hq_boost = max(1.0, min(4.0, float(self._harvest_top_quartile_boost)))
        harvest_mass = 0.0
        for tid in contrarian_tids:
            mass = pool_w.get(tid, 0.0)
            if tid in top_quartile:
                mass *= hq_boost
            harvest_mass += mass
        harvest_mass_ratio = (
            harvest_mass / total_mass if total_mass > 0.0 else 0.0
        )
        # Clamp the ratio so the top-quartile inflation can't push the
        # conviction_boost past 1.0 + harvest_scale (preserves the original
        # 1.5× cap).
        harvest_mass_ratio = max(0.0, min(1.0, harvest_mass_ratio))
        conviction_boost = 1.0 + harvest_scale * harvest_mass_ratio

        steps_bps: list[float] = []
        for h in range(n_steps):
            num = 0.0
            den = 0.0
            for tid, curve in zip(per_tribe_ids, per_tribe_curves):
                w_row = self._weights.get(tid)
                atw = w_row[h] if w_row and h < len(w_row) else 1.0
                # Specialist overlay: per-horizon skill from the calibrator's
                # horizon-bucketed EWMA. Stacks on top of the ATW learned
                # weight — ATW is within-tick curve-fitting, sk_h is the
                # cross-session "is this tribe actually good at THIS horizon"
                # prior. They reinforce: a tribe ATW-good at h=1 AND
                # calibrator-confirmed-good at short horizons gets double
                # amplification; a tribe ATW-good but calibrator-bad is
                # flagged as overfitting-this-tick and dampened.
                sk_h_row = skill_per_h.get(tid)
                sk_h = sk_h_row[h] if sk_h_row and h < len(sk_h_row) else 1.0
                wv = atw * sk_h * pool_w.get(tid, 0.0)
                num += curve[h] * wv
                den += wv
            steps_bps.append(num / den if den > 0 else 0.0)

        # `conviction_boost` is computed above and stamped on the Forecast
        # below so DecisionEngine can read it as a sizing multiplier (with
        # Path-Integrity clamp). It is no longer multiplied into the
        # projected path: that double-counted the harvest signal once on the
        # prediction side and again on the sizing side, and forced four
        # downstream sites to divide it back out before consuming the curve.

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
        base_conf = max(0.0, min(1.0, signal_quality * directional))

        # Phase 1b — Council entropy / diversity score. Compute once the
        # consensus direction is known, then scale confidence by it: a high
        # signal coming from a single category (e.g. 4 TA tribes agreeing)
        # is worth less than the same signal coming from 4 orthogonal
        # categories. Floor at 0.6× so a true single-bloc call still fires
        # when the signal itself is clear.
        consensus_sign = 1.0 if expected_move_bps >= 0 else -1.0
        diversity_score, category_mass = self._diversity_and_breakdown(
            initial_votes, pool_w, consensus_sign,
        )
        diversity_mult = 0.6 + 0.4 * diversity_score
        final_conf = max(0.0, min(1.0, base_conf * diversity_mult))

        # Phase 3c — weighted-average voter horizon. Short-horizon-dominated
        # councils signal panic-scalping; engine uses this to deleverage.
        avg_voter_horizon_s = self._weighted_avg_horizon(initial_votes, pool_w)

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

        # Task 4c — Path-Integrity. Weighted-average calibrator skill of
        # tribes voting WITH the consensus direction (pool-weight ×
        # |conviction|-weighted). If this drops below 0.7 the decision
        # engine must refuse to amplify size via agree_mult/crowd_mult:
        # a 100%-agreement signal from the council's worst 5 tribes is
        # not a stronger signal, it's a louder wrong one.
        driver_skill_avg: float | None = None
        if initial_votes:
            num_ds = 0.0
            den_ds = 0.0
            for tid, (v0, signed, _) in initial_votes.items():
                if signed * sign_ref <= 0.0:
                    continue  # abstain or voted against consensus
                pw = pool_w.get(tid, 0.0)
                contrib = pw * abs(float(v0.conviction))
                if contrib <= 0.0:
                    continue
                sk = skill.get(tid, 1.0)
                num_ds += contrib * sk
                den_ds += contrib
            if den_ds > 0.0:
                driver_skill_avg = num_ds / den_ds

        # Realized-volatility rail (Task 2). Compute once per tick from the
        # shared MarketState price history and publish on the Forecast so
        # both the paper executor and every shadow ledger see the same
        # number. None when there aren't enough price samples yet — in
        # which case executors skip the cap and trust the raw forecast.
        try:
            realized_vol_bps = state.realized_vol_bps(840.0)
        except Exception:
            realized_vol_bps = None

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
            diversity_score=diversity_score,
            category_mass=category_mass,
            avg_voter_horizon_s=avg_voter_horizon_s,
            driver_skill_avg=driver_skill_avg,
            harvest_mass_ratio=harvest_mass_ratio,
            conviction_boost=conviction_boost,
            realized_vol_bps=realized_vol_bps,
        )

    # -- Phase 1a: redundancy / cross-correlation ------------------------

    def _compute_redundancy(self, tribe_ids: list[str]) -> dict[str, float]:
        """For each tribe, compute the mean of |ρ(i,j)| across peers where
        |ρ| exceeds `_corr_threshold`. The resulting redundancy factor is
        in [0, 1]: 0 = this tribe's conviction series is uncorrelated with
        any peer; 1 = it moves in lockstep with everybody above threshold.
        Returns an empty dict when history is too short — callers interpret
        missing keys as zero redundancy (full pool weight)."""
        # Assemble aligned series — only tribes with a full window contribute.
        series: dict[str, list[float]] = {}
        for tid in tribe_ids:
            h = self._conviction_history.get(tid)
            if h is None:
                continue
            if len(h) < self._corr_min_samples:
                continue
            series[tid] = list(h)
        n = len(series)
        if n < 2:
            return {}
        # Truncate all series to the shortest length — the first few ticks
        # after startup have mixed history lengths, and zipping corr across
        # different windows would bias toward tribes that started earlier.
        min_len = min(len(v) for v in series.values())
        for tid in series:
            series[tid] = series[tid][-min_len:]
        # Pre-compute means + stddevs for Pearson.
        stats: dict[str, tuple[float, float]] = {}
        for tid, xs in series.items():
            m = sum(xs) / min_len
            var = sum((x - m) ** 2 for x in xs) / min_len
            sd = math.sqrt(var) if var > 0 else 0.0
            stats[tid] = (m, sd)
        out: dict[str, float] = {}
        ids = list(series.keys())
        for i_idx, i_tid in enumerate(ids):
            xs = series[i_tid]
            mi, si = stats[i_tid]
            if si == 0.0:
                out[i_tid] = 0.0
                continue
            high_corrs: list[float] = []
            for j_idx, j_tid in enumerate(ids):
                if j_idx == i_idx:
                    continue
                ys = series[j_tid]
                mj, sj = stats[j_tid]
                if sj == 0.0:
                    continue
                cov = sum((xs[k] - mi) * (ys[k] - mj) for k in range(min_len)) / min_len
                rho = cov / (si * sj)
                if abs(rho) > self._corr_threshold:
                    high_corrs.append(abs(rho))
            if not high_corrs:
                out[i_tid] = 0.0
            else:
                # Mean of the above-threshold correlations, rescaled so
                # `_corr_threshold`→0 haircut, 1.0→full haircut. Below the
                # threshold contributes nothing; at the threshold the
                # factor is zero (ignore); at perfect correlation, factor=1.
                avg = sum(high_corrs) / len(high_corrs)
                out[i_tid] = max(0.0, min(1.0,
                    (avg - self._corr_threshold) / max(1e-6, 1.0 - self._corr_threshold)))
        return out

    # -- Phase 3a: sigmoid regime smoothing -----------------------------

    def _regime_blend_alpha(self, now_ts: datetime) -> float:
        """Fraction of the way from the previous regime's multiplier table
        to the current one, using a sigmoid centered on `_regime_smoothing_s`.
        Returns 1.0 when enough time has elapsed since the last change
        (fully in the new regime), 0.0 right after a change (still mostly
        in the old one). When no change is tracked yet, returns 1.0."""
        if self._regime_change_ts is None:
            return 1.0
        elapsed = (now_ts - self._regime_change_ts).total_seconds()
        if elapsed <= 0:
            return 0.0
        # Logistic: α(t) = 1 / (1 + exp(-(t - t_mid) / k))
        # k controls how sharp the transition is; pick k = t_mid/3 so the
        # sigmoid spans ~10s of useful transition around the center.
        k = max(0.5, self._regime_smoothing_s / 3.0)
        x = (elapsed - self._regime_smoothing_s) / k
        # Saturate cleanly far from the midpoint.
        if x > 20:
            return 1.0
        if x < -20:
            return 0.0
        return 1.0 / (1.0 + math.exp(-x))

    # -- Phase 1b/1c/3c: diversity, category breakdown, voter horizon ---

    def _diversity_and_breakdown(
        self,
        initial_votes: dict[str, tuple],
        pool_w: dict[str, float],
        consensus_sign: float,
    ) -> tuple[float, dict[str, dict[str, float]]]:
        """Returns (diversity_score, category_mass). Diversity is Shannon
        entropy over tribe categories, computed against the dominant-side
        directional mass only, and normalized by log(n_active_categories).
        `category_mass` exposes the full per-category {buy, sell} breakdown
        so the decision engine can detect crowded trades."""
        category_mass: dict[str, dict[str, float]] = {}
        for tid, (v, _signed, _strength) in initial_votes.items():
            cat = _TRIBE_CATEGORY.get(tid, "ta")
            pw = pool_w.get(tid, 0.0)
            bucket = category_mass.setdefault(cat, {"buy": 0.0, "sell": 0.0})
            bucket["buy"] += pw * v.buy_frac
            bucket["sell"] += pw * v.sell_frac
        # Pick out the dominant-side mass per category.
        key = "buy" if consensus_sign >= 0 else "sell"
        active_shares = [m[key] for m in category_mass.values() if m[key] > 0]
        total = sum(active_shares)
        if total <= 0 or len(active_shares) <= 1:
            return 0.0, category_mass
        probs = [s / total for s in active_shares]
        entropy = -sum(p * math.log(p) for p in probs if p > 0)
        norm = math.log(len(active_shares))
        diversity = entropy / norm if norm > 0 else 0.0
        return max(0.0, min(1.0, diversity)), category_mass

    def _weighted_avg_horizon(
        self,
        initial_votes: dict[str, tuple],
        pool_w: dict[str, float],
    ) -> float | None:
        """Pool-weighted mean of each active tribe's horizon_seconds. Returns
        None when no tribe has active mass (council fully silent). Weight
        uses (buy_frac + sell_frac) * pool_w — abstaining tribes don't
        influence the mean. The decision engine uses this to detect a
        scalp-dominated council and deleverage accordingly."""
        num = 0.0
        den = 0.0
        for tid, (v, _s, _bs) in initial_votes.items():
            active = float(v.buy_frac + v.sell_frac)
            if active <= 0:
                continue
            pw = pool_w.get(tid, 0.0)
            w = active * pw
            if w <= 0:
                continue
            hs = float(getattr(v, "horizon_seconds", 0) or 0)
            if hs <= 0:
                # Fall back to the cached spec horizon; scalper tribes
                # sometimes report horizon_seconds=0 on idle votes and we
                # don't want that to collapse the mean to zero.
                hs = self._tribe_horizon_s.get(tid, self.default_horizon_seconds)
            num += w * hs
            den += w
        if den <= 0:
            return None
        return num / den

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

