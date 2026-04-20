"""Tribe skill calibrator — Phase G audit→calibration closed loop with
Phase H within-session regime adaptation.

Consumes resolved forecast anchors from `ForecastAuditor` and, for each
tick, scores every tribe that voted at that tick against the realized
direction. The running skill score (EWMA of conviction-weighted hit rate)
becomes a multiplier that scales each tribe's effective conviction in
future votes.

Two parallel EWMAs per tribe:
  - **slow** (halflife ~500 resolutions): the long-run stable skill
    estimate. Low variance, slow to react. Used as the default
    multiplier when the regime is stable.
  - **fast** (halflife ~50 resolutions): the recent-regime skill
    estimate. Higher variance, reacts quickly. Takes over when it
    diverges from the slow estimate by more than a threshold — that
    divergence IS the regime-shift signal.

This lets the calibrator adapt within a session: a tribe skilled in
chop but not in cascade has its multiplier drop the moment cascades
start resolving badly, rather than waiting 500 anchors for the slow
EWMA to catch up.

Design principles:
  1. Non-destructive — YAMLs stay the source of truth. The multiplier is
     a runtime overlay persisted to `data/calibration/tribe_skill.json`;
     delete that file to reset.
  2. Opt-in — `apply=False` by default: skill is measured and surfaced in
     the UI, but votes are not scaled. Flip to True to close the loop.
  3. Bounded — multiplier clamped to `[max_shrink, max_boost]` so no
     single tribe can dominate or be silenced by a short hot/cold streak.
  4. EWMA, not running mean — recent performance matters more than
     ancient; regime changes decay out of the score on a known halflife.

The loop: every resolved anchor reaches back to the tick_ts at which
it was emitted, looks up every tribe's vote at that tick (side × conviction),
and updates that tribe's EWMA of "did my side match the realized move?".
Strong wrong votes cost more than weak wrong votes. Strong right votes
pay more than weak right votes. Abstains don't move the score.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import orjson
from loguru import logger

from ..core.config import PROJECT_ROOT
from ..core.types import Side, Vote
from .backscore import Resolved


DEFAULT_DIR = PROJECT_ROOT / "data" / "calibration"


class TribeCalibrator:
    """Live per-tribe skill scorer.

    Parameters
    ----------
    halflife_resolved : int
        Halflife (in resolved anchors) of the EWMA. 500 means a tribe's
        score forgets ~half of its history every 500 resolutions — roughly
        one trading day at a 15s tick with 5 anchors per tick.
    min_samples : int
        Don't apply a multiplier until this many resolved anchors have
        scored the tribe. Guards against noisy early-tick swings.
    max_shrink, max_boost : float
        Clamp bounds for the multiplier. 0.5 / 1.5 means an unreliable
        tribe can be halved, and a skilled tribe can add 50%. Bigger
        ranges amplify the feedback — keep modest.
    apply : bool
        False (default): score and surface, don't modify votes.
        True: the engine should multiply each vote's conviction by
        `skill_multiplier(tribe_id)` before aggregation.
    """

    def __init__(
        self,
        *,
        directory: Path | None = None,
        halflife_resolved: int = 500,
        min_samples: int = 30,
        max_shrink: float = 0.6,
        max_boost: float = 1.4,
        apply: bool = False,
        vote_history_s: float = 1200.0,
        fast_halflife_resolved: int = 50,
        fast_min_samples: int = 10,
        regime_shift_threshold: float = 0.12,
        enable_regime_conditional: bool = False,
        regime_min_samples: int = 15,
        conviction_epsilon: float = 0.05,
        horizon_min_samples: int = 20,
    ) -> None:
        self.dir = directory or DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path = self.dir / "tribe_skill.json"
        self._halflife = max(1, int(halflife_resolved))
        self._min_samples = max(1, int(min_samples))
        self._max_shrink = max(0.0, min(1.0, float(max_shrink)))
        self._max_boost = max(1.0, float(max_boost))
        self._apply = bool(apply)
        self._vote_history_s = float(vote_history_s)
        # Phase H — fast EWMA for within-session regime adaptation.
        self._halflife_fast = max(1, int(fast_halflife_resolved))
        self._min_samples_fast = max(1, int(fast_min_samples))
        self._regime_shift_threshold = max(0.0, float(regime_shift_threshold))

        # Per-tribe state: {"n": int, "correct": int, "ewma": float, "ewma_fast": float}.
        # `ewma` lives in [0,1]; 0.5 = chance. Start at 0.5 (neutral).
        self._skill: dict[str, dict] = {}

        # Regime-conditional skill. Same shape as _skill, nested under
        # (tribe_id, regime_label). When enabled, `skill_multiplier` prefers
        # the per-regime EWMA over the global one once min_samples per
        # regime has been crossed — resolves "trend-tribe skilled in trend
        # bleeds conviction in chop" without waiting for the fast EWMA to
        # converge across hundreds of anchors.
        self._skill_by_regime: dict[str, dict[str, dict]] = {}
        self._regime_conditional = bool(enable_regime_conditional)
        self._regime_min_samples = max(1, int(regime_min_samples))

        # ── Task 3: Conviction ε — don't score unfalsifiable micro-votes.
        # A vote with |conv| < 0.05 is functionally an abstention: it barely
        # moves the pool, and a "hit" on it is dominated by noise (even a
        # coin-flip tribe will look 50% at tiny conviction). Scoring these
        # lets passive_whale (emit 0.02 votes constantly) and options_hedger
        # (emit 0.03 votes) coast to the top of the leaderboard not on edge
        # but on statistical floor. Skip them: you must bet meaningfully to
        # earn a meaningful score.
        self._conviction_epsilon = max(0.0, float(conviction_epsilon))

        # ── Task 1 / 4b: per-(tribe, horizon_bucket) skill EWMA.
        # The council's best 1-step predictor (microstructure_prober) is
        # often the council's worst 30-step predictor, and vice-versa.
        # Collapsing all horizons into one scalar punishes specialists.
        # Horizons are bucketed into three bands (short ≤60s, medium
        # 60–300s, long >300s); each (tribe, bucket) gets its own EWMA.
        # `skill_multiplier(tid, horizon_s=H)` returns the bucket's
        # multiplier when the bucket has ≥horizon_min_samples resolutions,
        # falling back to the global scalar otherwise.
        self._skill_by_horizon: dict[str, dict[str, dict]] = {}
        self._horizon_min_samples = max(1, int(horizon_min_samples))

        # Vote log so we can retrospectively score past ticks when
        # forecasts mature. Keyed by tick_ts (epoch seconds) → {"regime":
        # str|None, "votes": {tribe_id: (side_int, conviction)}}. Bounded by
        # _vote_history_s (must cover the longest anchor horizon).
        self._votes_at_tick: "OrderedDict[float, dict]" = OrderedDict()

        self._load()

    # --- ingest ------------------------------------------------------

    def record_votes(
        self,
        tick_ts: float,
        votes: Iterable[Vote],
        regime_label: str | None = None,
    ) -> None:
        """Snapshot this tick's votes so resolved anchors can score them.
        Call once per tick, with the per-tribe votes before aggregation.
        `regime_label` (e.g. "chop", "cascade_up") is stamped so resolved
        anchors feed both the global and the per-regime EWMA."""
        per: dict[str, tuple[int, float]] = {}
        for v in votes:
            if v.side is Side.BUY:
                side = 1
            elif v.side is Side.SELL:
                side = -1
            else:
                side = 0
            per[v.tribe_id] = (side, float(v.conviction))
        self._votes_at_tick[float(tick_ts)] = {
            "regime": regime_label,
            "votes": per,
        }
        cutoff = float(tick_ts) - self._vote_history_s
        while self._votes_at_tick:
            oldest_ts = next(iter(self._votes_at_tick))
            if oldest_ts >= cutoff:
                break
            self._votes_at_tick.popitem(last=False)

    @staticmethod
    def _horizon_bucket(horizon_s: float) -> str:
        """Map a resolved anchor's horizon to one of three skill buckets.
        Short/medium/long bands chosen so each covers a ~5× range and
        roughly aligns with the council's reactive / thematic / structural
        tribes. microstructure_prober dominates short; basis_arb and
        passive_whale dominate long."""
        h = float(horizon_s)
        if h <= 60.0:
            return "short"
        if h <= 300.0:
            return "medium"
        return "long"

    def apply_resolved(self, resolved: Iterable[Resolved]) -> None:
        """Score every tribe that voted at the anchor's origin tick against
        the realized direction. Updates both slow and fast EWMAs (global),
        the per-regime EWMA for the label stamped at vote time, AND the
        per-(tribe, horizon_bucket) EWMA for specialist accounting."""
        changed = False
        alpha = 1.0 - 0.5 ** (1.0 / float(self._halflife))
        alpha_fast = 1.0 - 0.5 ** (1.0 / float(self._halflife_fast))
        eps = self._conviction_epsilon
        for rec in resolved:
            snap = self._votes_at_tick.get(rec.tick_ts)
            if not snap:
                continue
            votes = snap["votes"] if isinstance(snap, dict) and "votes" in snap else snap
            regime_label = snap.get("regime") if isinstance(snap, dict) else None
            if rec.realized_move_bps > 0:
                realized = 1
            elif rec.realized_move_bps < 0:
                realized = -1
            else:
                continue  # no realized direction; skip
            bucket = self._horizon_bucket(rec.horizon_s)
            for tid, (side, conv) in votes.items():
                if side == 0:
                    continue  # abstain — no bet, no score
                # Task 3: drop unfalsifiable micro-votes. A 0.02-conviction
                # vote is a whisper — scoring it as a full bet-or-miss
                # inflates the hit-rate of tribes whose "strategy" is to
                # only-ever-nudge.
                if abs(conv) < eps:
                    continue
                hit = 1.0 if side == realized else 0.0
                weighted = 0.5 + (hit - 0.5) * max(0.0, min(1.0, conv))
                s = self._skill.setdefault(
                    tid, {"n": 0, "correct": 0, "ewma": 0.5, "ewma_fast": 0.5}
                )
                if "ewma_fast" not in s:
                    s["ewma_fast"] = s.get("ewma", 0.5)
                s["ewma"] = s["ewma"] + alpha * (weighted - s["ewma"])
                s["ewma_fast"] = s["ewma_fast"] + alpha_fast * (weighted - s["ewma_fast"])
                s["n"] += 1
                s["correct"] += int(hit)
                changed = True
                # Per-regime EWMA. Uses the same alpha so a regime's recent
                # performance weighs identically to the global recency.
                if regime_label:
                    rs = self._skill_by_regime.setdefault(tid, {}).setdefault(
                        regime_label, {"n": 0, "correct": 0, "ewma": 0.5}
                    )
                    rs["ewma"] = rs["ewma"] + alpha * (weighted - rs["ewma"])
                    rs["n"] += 1
                    rs["correct"] += int(hit)
                # Task 1 / 4b: per-horizon-bucket EWMA.
                hs = self._skill_by_horizon.setdefault(tid, {}).setdefault(
                    bucket, {"n": 0, "correct": 0, "ewma": 0.5, "ewma_fast": 0.5}
                )
                hs["ewma"] = hs["ewma"] + alpha * (weighted - hs["ewma"])
                hs["ewma_fast"] = hs["ewma_fast"] + alpha_fast * (weighted - hs["ewma_fast"])
                hs["n"] += 1
                hs["correct"] += int(hit)
        if changed:
            self._save()

    # --- query -------------------------------------------------------

    def _ewma_to_multiplier(self, ewma: float) -> float:
        """Piecewise linear map: 0 → max_shrink, 0.5 → 1.0, 1 → max_boost."""
        x = max(0.0, min(1.0, float(ewma)))
        if x >= 0.5:
            return 1.0 + (x - 0.5) * 2.0 * (self._max_boost - 1.0)
        return self._max_shrink + x * 2.0 * (1.0 - self._max_shrink)

    def skill_multiplier(
        self,
        tribe_id: str,
        regime_label: str | None = None,
        horizon_s: float | None = None,
    ) -> float:
        """Multiplier in [max_shrink, max_boost] for this tribe's conviction.

        Resolution order (first match wins):
          1. Per-horizon-bucket EWMA (if horizon_s given and n ≥ horizon_min_samples)
          2. Per-regime EWMA (if enabled, label provided, n ≥ regime_min_samples)
          3. Fast EWMA override (if |fast − slow| ≥ regime_shift_threshold)
          4. Slow EWMA (the long-run stable estimate)
          5. 1.0 (too few samples, or apply=False)
        """
        if not self._apply:
            return 1.0
        # 1 — per-horizon specialist skill. Takes precedence over regime
        # conditioning because horizon-specialization is a stronger signal
        # than regime-specialization (a microstructure tribe is always
        # good at short-h regardless of regime label).
        if horizon_s is not None:
            bucket = self._horizon_bucket(horizon_s)
            hs = self._skill_by_horizon.get(tribe_id, {}).get(bucket)
            if hs and hs["n"] >= self._horizon_min_samples:
                slow_h = float(hs["ewma"])
                fast_h = float(hs.get("ewma_fast", slow_h))
                if hs["n"] >= max(self._min_samples_fast, self._horizon_min_samples) and \
                        abs(fast_h - slow_h) >= self._regime_shift_threshold:
                    return self._ewma_to_multiplier(fast_h)
                return self._ewma_to_multiplier(slow_h)
        # 2 — per-regime.
        if self._regime_conditional and regime_label:
            rs = self._skill_by_regime.get(tribe_id, {}).get(regime_label)
            if rs and rs["n"] >= self._regime_min_samples:
                return self._ewma_to_multiplier(float(rs["ewma"]))
        s = self._skill.get(tribe_id)
        if not s or s["n"] < self._min_samples:
            return 1.0
        slow = float(s["ewma"])
        fast = float(s.get("ewma_fast", slow))
        # 3 — fast override.
        if s["n"] >= self._min_samples_fast and abs(fast - slow) >= self._regime_shift_threshold:
            return self._ewma_to_multiplier(fast)
        # 4 — slow fallback.
        return self._ewma_to_multiplier(slow)

    def is_contrarian_candidate(
        self,
        tribe_id: str,
        *,
        hit_rate_threshold: float = 0.45,
        min_samples: int = 50,
    ) -> bool:
        """True when this tribe's recent-window hit-rate has fallen far enough
        below chance that flipping its vote is net-positive — Task 2's
        'TA Cemetery' inversion trigger.

        A tribe that is reliably wrong 55%+ of the time is as useful as one
        that is reliably right 55% of the time: invert its conviction and
        you've bought a new predictor for free. Uses the fast EWMA because
        we want the rolling-window behavior (not lifetime average) — a
        tribe that USED to work but has broken down this regime should flip,
        and a tribe that's rediscovered its edge should un-flip.

        The threshold should be well below 0.5 (we're gating on 'consistently
        worse than chance', not 'not-above-chance') to avoid flipping noisy
        50/50 tribes on a hot-hand coin-flip streak.
        """
        s = self._skill.get(tribe_id)
        if not s:
            return False
        n = int(s.get("n", 0))
        if n < max(min_samples, self._min_samples_fast):
            return False
        ewma_fast = float(s.get("ewma_fast", s.get("ewma", 0.5)))
        return ewma_fast < float(hit_rate_threshold)

    def apply_skepticism_prior(
        self,
        skepticism_by_tribe: dict[str, float],
        *,
        max_swing: float = 0.2,
        overwrite_existing: bool = False,
    ) -> int:
        """Set each tribe's starting EWMA from its adversarial-replay
        skepticism score (Phase 14).

        `skepticism ∈ [0, 1]` — fraction of spoof fixtures where the tribe
        voted AGAINST the bait. Maps linearly into a starting EWMA:

            skepticism 0.0 → ewma 0.5 - max_swing  (chronically fooled)
            skepticism 0.5 → ewma 0.5              (no signal)
            skepticism 1.0 → ewma 0.5 + max_swing  (consistently skeptical)

        By default tribes with `n > 0` are left untouched — the prior is
        an *initialization*, not a runtime override; live calibration data
        always wins once it exists. Pass `overwrite_existing=True` to
        reset live samples too.

        Returns the number of tribes whose skill state was set or updated."""
        swing = max(0.0, min(0.5, float(max_swing)))
        n_changed = 0
        for tid, score in skepticism_by_tribe.items():
            s = max(0.0, min(1.0, float(score)))
            ewma_prior = 0.5 + (s - 0.5) * 2.0 * swing
            existing = self._skill.get(tid)
            if existing and existing.get("n", 0) > 0 and not overwrite_existing:
                continue
            self._skill[tid] = {
                "n": existing.get("n", 0) if existing else 0,
                "correct": existing.get("correct", 0) if existing else 0,
                "ewma": ewma_prior,
                "ewma_fast": ewma_prior,
            }
            n_changed += 1
        if n_changed:
            self._save()
        return n_changed

    def regime_shift_alerts(self) -> list[dict]:
        """List tribes currently in a detected regime shift (fast-slow
        divergence beyond threshold). Sorted by magnitude. Useful for UI
        surfacing and debugging."""
        out: list[dict] = []
        for tid, s in self._skill.items():
            if s["n"] < self._min_samples_fast:
                continue
            slow = float(s["ewma"])
            fast = float(s.get("ewma_fast", slow))
            diff = fast - slow
            if abs(diff) >= self._regime_shift_threshold:
                out.append({
                    "tribe_id": tid,
                    "ewma_slow": slow,
                    "ewma_fast": fast,
                    "divergence": diff,
                    "n": int(s["n"]),
                })
        out.sort(key=lambda r: -abs(r["divergence"]))
        return out

    def snapshot(self) -> dict:
        rows = []
        for tid, s in sorted(self._skill.items()):
            slow = float(s["ewma"])
            fast = float(s.get("ewma_fast", slow))
            diff = fast - slow
            shifted = (
                s["n"] >= self._min_samples_fast
                and abs(diff) >= self._regime_shift_threshold
            )
            regime_rows = []
            for reg, rs in sorted(self._skill_by_regime.get(tid, {}).items()):
                regime_rows.append({
                    "regime": reg,
                    "n": int(rs["n"]),
                    "ewma_hit": float(rs["ewma"]),
                    "raw_hit_rate": (rs["correct"] / rs["n"]) if rs["n"] else None,
                    "active": (
                        self._regime_conditional
                        and rs["n"] >= self._regime_min_samples
                    ),
                })
            horizon_rows = []
            for bucket in ("short", "medium", "long"):
                hs = self._skill_by_horizon.get(tid, {}).get(bucket)
                if not hs:
                    continue
                horizon_rows.append({
                    "bucket": bucket,
                    "n": int(hs["n"]),
                    "ewma_hit": float(hs["ewma"]),
                    "ewma_hit_fast": float(hs.get("ewma_fast", hs["ewma"])),
                    "raw_hit_rate": (hs["correct"] / hs["n"]) if hs["n"] else None,
                    "active": hs["n"] >= self._horizon_min_samples,
                })
            rows.append({
                "tribe_id": tid,
                "n": s["n"],
                "correct": s["correct"],
                "raw_hit_rate": (s["correct"] / s["n"]) if s["n"] else None,
                "ewma_hit": slow,
                "ewma_hit_fast": fast,
                "divergence": diff,
                "regime_shift": shifted,
                "multiplier": self.skill_multiplier(tid),
                "by_regime": regime_rows,
                "by_horizon": horizon_rows,
            })
        return {
            "apply": self._apply,
            "halflife_resolved": self._halflife,
            "halflife_resolved_fast": self._halflife_fast,
            "min_samples": self._min_samples,
            "min_samples_fast": self._min_samples_fast,
            "regime_shift_threshold": self._regime_shift_threshold,
            "max_shrink": self._max_shrink,
            "max_boost": self._max_boost,
            "regime_conditional": self._regime_conditional,
            "regime_min_samples": self._regime_min_samples,
            "tribes": rows,
        }

    # --- persistence -------------------------------------------------

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "skill": self._skill,
            "skill_by_regime": self._skill_by_regime,
            "skill_by_horizon": self._skill_by_horizon,
        }
        try:
            self._path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        except OSError as e:
            logger.warning("[calibrator] could not persist skill file: {}", e)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = orjson.loads(self._path.read_bytes())
        except Exception as e:
            logger.warning("[calibrator] skill file unreadable, ignoring: {}", e)
            return
        skill = data.get("skill") if isinstance(data, dict) else None
        if not isinstance(skill, dict):
            return
        for tid, s in skill.items():
            if not isinstance(s, dict):
                continue
            n = int(s.get("n", 0))
            # Invariant: ewma is meaningful only when n > 0. An entry with
            # n=0 but ewma≠0.5 is stale state from a previous build; reset
            # to neutral so it doesn't drag a brand-new tribe toward max_shrink
            # the moment it crosses min_samples.
            ewma = float(s.get("ewma", 0.5)) if n > 0 else 0.5
            # Backfill ewma_fast from ewma when loading pre-Phase-H state.
            ewma_fast = float(s.get("ewma_fast", ewma)) if n > 0 else 0.5
            self._skill[tid] = {
                "n": n,
                "correct": int(s.get("correct", 0)),
                "ewma": ewma,
                "ewma_fast": ewma_fast,
            }
        # Regime-conditional skill (backfill-friendly — older files lack this).
        regime = data.get("skill_by_regime") if isinstance(data, dict) else None
        if isinstance(regime, dict):
            for tid, by_reg in regime.items():
                if not isinstance(by_reg, dict):
                    continue
                clean: dict[str, dict] = {}
                for label, rs in by_reg.items():
                    if not isinstance(rs, dict):
                        continue
                    rn = int(rs.get("n", 0))
                    rewma = float(rs.get("ewma", 0.5)) if rn > 0 else 0.5
                    clean[str(label)] = {
                        "n": rn,
                        "correct": int(rs.get("correct", 0)),
                        "ewma": rewma,
                    }
                if clean:
                    self._skill_by_regime[tid] = clean
        # Horizon-bucketed skill (Task 1/4b — also backfill-friendly).
        horizon = data.get("skill_by_horizon") if isinstance(data, dict) else None
        if isinstance(horizon, dict):
            for tid, by_h in horizon.items():
                if not isinstance(by_h, dict):
                    continue
                clean_h: dict[str, dict] = {}
                for bucket, hs in by_h.items():
                    if not isinstance(hs, dict):
                        continue
                    hn = int(hs.get("n", 0))
                    hewma = float(hs.get("ewma", 0.5)) if hn > 0 else 0.5
                    hewma_fast = float(hs.get("ewma_fast", hewma)) if hn > 0 else 0.5
                    clean_h[str(bucket)] = {
                        "n": hn,
                        "correct": int(hs.get("correct", 0)),
                        "ewma": hewma,
                        "ewma_fast": hewma_fast,
                    }
                if clean_h:
                    self._skill_by_horizon[tid] = clean_h
        logger.info("[calibrator] loaded skill overlay for {} tribes from {}",
                    len(self._skill), self._path.name)
