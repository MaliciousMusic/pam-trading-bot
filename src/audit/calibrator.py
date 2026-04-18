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

        # Vote log so we can retrospectively score past ticks when
        # forecasts mature. Keyed by tick_ts (epoch seconds) → {tribe_id:
        # (side_int, conviction)}. Bounded by _vote_history_s (must cover
        # the longest anchor horizon).
        self._votes_at_tick: "OrderedDict[float, dict[str, tuple[int, float]]]" = OrderedDict()

        self._load()

    # --- ingest ------------------------------------------------------

    def record_votes(self, tick_ts: float, votes: Iterable[Vote]) -> None:
        """Snapshot this tick's votes so resolved anchors can score them.
        Call once per tick, with the per-tribe votes before aggregation."""
        per: dict[str, tuple[int, float]] = {}
        for v in votes:
            if v.side is Side.BUY:
                side = 1
            elif v.side is Side.SELL:
                side = -1
            else:
                side = 0
            per[v.tribe_id] = (side, float(v.conviction))
        self._votes_at_tick[float(tick_ts)] = per
        cutoff = float(tick_ts) - self._vote_history_s
        while self._votes_at_tick:
            oldest_ts = next(iter(self._votes_at_tick))
            if oldest_ts >= cutoff:
                break
            self._votes_at_tick.popitem(last=False)

    def apply_resolved(self, resolved: Iterable[Resolved]) -> None:
        """Score every tribe that voted at the anchor's origin tick against
        the realized direction. Updates both slow and fast EWMAs, then
        persists."""
        changed = False
        alpha = 1.0 - 0.5 ** (1.0 / float(self._halflife))
        alpha_fast = 1.0 - 0.5 ** (1.0 / float(self._halflife_fast))
        for rec in resolved:
            votes = self._votes_at_tick.get(rec.tick_ts)
            if not votes:
                continue
            if rec.realized_move_bps > 0:
                realized = 1
            elif rec.realized_move_bps < 0:
                realized = -1
            else:
                continue  # no realized direction; skip
            for tid, (side, conv) in votes.items():
                if side == 0:
                    continue  # abstain — no bet, no score
                hit = 1.0 if side == realized else 0.0
                weighted = 0.5 + (hit - 0.5) * max(0.0, min(1.0, conv))
                s = self._skill.setdefault(
                    tid, {"n": 0, "correct": 0, "ewma": 0.5, "ewma_fast": 0.5}
                )
                # Backfill ewma_fast for state loaded from an older format.
                if "ewma_fast" not in s:
                    s["ewma_fast"] = s.get("ewma", 0.5)
                s["ewma"] = s["ewma"] + alpha * (weighted - s["ewma"])
                s["ewma_fast"] = s["ewma_fast"] + alpha_fast * (weighted - s["ewma_fast"])
                s["n"] += 1
                s["correct"] += int(hit)
                changed = True
        if changed:
            self._save()

    # --- query -------------------------------------------------------

    def _ewma_to_multiplier(self, ewma: float) -> float:
        """Piecewise linear map: 0 → max_shrink, 0.5 → 1.0, 1 → max_boost."""
        x = max(0.0, min(1.0, float(ewma)))
        if x >= 0.5:
            return 1.0 + (x - 0.5) * 2.0 * (self._max_boost - 1.0)
        return self._max_shrink + x * 2.0 * (1.0 - self._max_shrink)

    def skill_multiplier(self, tribe_id: str) -> float:
        """Multiplier in [max_shrink, max_boost] to scale this tribe's
        conviction. Returns 1.0 if the overlay is disabled or the tribe
        has fewer than `min_samples` scored anchors.

        Phase H regime-adaptive logic: when the fast EWMA has enough
        samples AND diverges from the slow EWMA by more than
        `regime_shift_threshold`, the fast estimate takes over — the
        tribe is in a changed regime and the stale long-run history is
        no longer informative. Otherwise the slow EWMA (low variance,
        stable) is used."""
        if not self._apply:
            return 1.0
        s = self._skill.get(tribe_id)
        if not s or s["n"] < self._min_samples:
            return 1.0
        slow = float(s["ewma"])
        fast = float(s.get("ewma_fast", slow))
        # Regime-shift override: fast EWMA wins if it has its own sample
        # floor AND diverges from slow by more than threshold.
        if s["n"] >= self._min_samples_fast and abs(fast - slow) >= self._regime_shift_threshold:
            return self._ewma_to_multiplier(fast)
        return self._ewma_to_multiplier(slow)

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
            "tribes": rows,
        }

    # --- persistence -------------------------------------------------

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "skill": self._skill,
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
        logger.info("[calibrator] loaded skill overlay for {} tribes from {}",
                    len(self._skill), self._path.name)
