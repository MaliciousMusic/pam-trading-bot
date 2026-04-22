"""Projector kernel-gain fitter.

The chess projector maps aggregated tribe pressure to price moves via a
single scalar `bps_per_unit_pressure`. That constant was hand-picked; this
module fits it from realized backscore anchors and slowly nudges it live so
"the council said +15bps" stops meaning an arbitrary amount.

Flow:
  1. As the auditor resolves anchors it hands us the matured records.
  2. We keep (predicted_bps, realized_bps) pairs for anchors whose
     prediction magnitude is big enough to be informative.
  3. Each new batch → robust median of `realized / predicted` over the
     trailing window. That ratio IS the correction factor for the kernel
     gain (anchor math is `predicted = net_pressure × gain`, so the
     correction folds straight into the gain).
  4. The correction is applied to the live gain through an EWMA so one
     noisy window can't swing the kernel hard. Clamped to [MIN, MAX] so
     even a pathological fit stays bounded.

Persistence: `data/calibration/projector_gain.json` survives restarts so
the calibration resumes where it left off.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..core import serde
from loguru import logger

from ..audit.backscore import Resolved
from ..core.config import PROJECT_ROOT


DEFAULT_DIR = PROJECT_ROOT / "data" / "calibration"

# Ignore anchors whose prediction is too small — the ratio is dominated by
# noise and would pull the median around.
MIN_PRED_BPS = 0.5
# Hard bounds on the gain. Stops a pathological fit from silencing or
# blowing up forecasts. 1–40 bps per unit pressure spans "market was dead"
# to "cascade tape".
MIN_GAIN = 1.0
MAX_GAIN = 40.0


class KernelCalibrator:
    """Live fitter for ChessProjector.bps_per_unit_pressure.

    Parameters
    ----------
    initial_gain : float
        Starting value, typically from `settings.projector.bps_per_unit_pressure`.
    halflife_resolved_s : float | None
        **Preferred, time-based halflife in wallclock seconds.** Phase 2.5:
        when set, the EWMA decays by `0.5 ** (dt_s / halflife_s)` per
        refit, decoupling the calibration speed from tick cadence.
    halflife_resolved : int
        Legacy count-based halflife in resolved anchors. Used when
        `halflife_resolved_s` is None.
    min_samples : int
        Don't modify the gain until this many usable anchors have been
        seen. Guards against first-session swings.
    window : int
        Size of the trailing ratio window used in the median fit.
    apply : bool
        False → measure only, callers must ignore `current_gain()`.
        True  → callers should read `current_gain()` each tick and push
        it into the projector.
    """

    def __init__(
        self,
        *,
        initial_gain: float,
        directory: Path | None = None,
        halflife_resolved: int = 300,
        halflife_resolved_s: float | None = None,
        min_samples: int = 50,
        window: int = 2000,
        apply: bool = True,
    ) -> None:
        self.dir = directory or DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path = self.dir / "projector_gain.json"
        self._initial_gain = max(MIN_GAIN, min(MAX_GAIN, float(initial_gain)))
        self._halflife = max(1, int(halflife_resolved))
        self._halflife_s = (
            float(halflife_resolved_s) if halflife_resolved_s is not None else None
        )
        self._time_based = self._halflife_s is not None and self._halflife_s > 0.0
        self._min_samples = max(1, int(min_samples))
        self._window = max(50, int(window))
        self._apply = bool(apply)

        self._pairs: deque[tuple[float, float]] = deque(maxlen=self._window)
        self._gain: float = self._initial_gain
        self._n_seen: int = 0
        self._last_correction: float = 1.0
        self._last_refit_ts: float | None = None
        # WS3 H2 Option 1a diagnostics. `last_dropped_fraction` is the
        # share of non-zero pairs in the window whose (predicted, realized)
        # signs disagreed at the most recent refit — this doubles as a
        # cross-check on the ForecastAuditor's directional-hit rate (they
        # should match within the MIN_PRED_BPS filter). The quartile
        # triple describes the *correct-direction* ratio distribution the
        # fit target actually saw, so operators can see magnitude bias
        # independently of the clamp at `correction`.
        self._last_dropped_fraction: float = 0.0
        self._last_correct_ratio_p25: float = 1.0
        self._last_correct_ratio_p50: float = 1.0
        self._last_correct_ratio_p75: float = 1.0
        self._load()

    # --- ingest -------------------------------------------------------

    def apply_resolved(self, resolved: Iterable[Resolved]) -> None:
        """Accumulate informative pairs, refit if we're past min_samples."""
        added = 0
        last_resolve_ts: float | None = None
        for rec in resolved:
            if abs(rec.expected_move_bps) < MIN_PRED_BPS:
                continue
            self._pairs.append((rec.expected_move_bps, rec.realized_move_bps))
            self._n_seen += 1
            added += 1
            last_resolve_ts = float(rec.realized_ts)
        if added and self._n_seen >= self._min_samples:
            self._refit(last_resolve_ts)
            self._save()

    def _refit(self, now_ts: float | None = None) -> None:
        """Robust median of realized/predicted over the window, EWMA-smoothed.

        Phase 2.5: when `halflife_resolved_s` is set, `alpha` comes from the
        wallclock gap between refits so the smoothing speed stays
        independent of tick cadence.

        WS3 H2 Option 1a: the fit target is computed over the
        **correct-direction** subset only. A signed-ratio median
        conflates directional accuracy with magnitude bias — when the
        council's hit rate is near 50 %, the raw median sits near zero
        and the clamp at `max(0.25, …)` silently pins the gain at
        `MIN_GAIN`, producing a fit target that is a clamp artifact
        rather than a measurement (see
        `research/phase11_diagnostic/WS3_H2_path_audit.md §4`). The
        calibrator fits *scale*; direction is the job of
        `TribeCalibrator` and the conf formula. Dropping wrong-direction
        samples keeps the scale fit on the metric it's actually
        measuring, and surfaces the dropped fraction as a diagnostic
        that cross-checks the auditor's directional-hit rate."""
        if not self._pairs:
            return
        # Non-zero predicted pairs gate the denominator. Zero-predicted
        # pairs were already filtered upstream via `MIN_PRED_BPS`, but
        # keep the guard defensive in case that threshold drops in a
        # later experiment.
        nonzero_pairs = [(p, r) for p, r in self._pairs if p != 0]
        total_nonzero = len(nonzero_pairs)
        if total_nonzero == 0:
            return
        # Option 1a filter. `(p > 0) == (r > 0)` is true when predicted
        # and realized moves share a sign. Zero-realized rows (r == 0)
        # collapse to `False == (p > 0)` which agrees only when p <= 0;
        # at p > 0 they drop out, at p < 0 they pass. Both cases
        # contribute a ratio of 0 to the correct-direction distribution,
        # which is harmless (a true flat realized means our magnitude
        # prediction was wrong by 100 %, and pulling the median toward 0
        # from above is the correct direction for the fit).
        ratios = [r / p for p, r in nonzero_pairs if (p > 0) == (r > 0)]
        self._last_dropped_fraction = 1.0 - len(ratios) / total_nonzero
        if not ratios:
            # Pathological: no correct-direction samples in window. Leave
            # the gain untouched — the calibrator has no information to
            # update on.
            self._last_correction = 1.0
            return
        ratios.sort()
        mid = len(ratios) // 2
        correction = (
            ratios[mid] if len(ratios) % 2 == 1
            else 0.5 * (ratios[mid - 1] + ratios[mid])
        )
        # Quartile diagnostics over the correct-direction subset so
        # operators can read magnitude bias independently of the clamp
        # below. Guard against tiny windows where p25/p75 indexing would
        # collapse onto the same element as the median.
        n = len(ratios)
        self._last_correct_ratio_p25 = ratios[max(0, n // 4)]
        self._last_correct_ratio_p50 = correction
        self._last_correct_ratio_p75 = ratios[min(n - 1, (3 * n) // 4)]
        # Huge one-sided corrections usually mean the window straddled a
        # regime switch — trim so the EWMA can catch up without a giant
        # step. Post-Option-1a this clamp should rarely bind; when it
        # does, the pre-clamp value is visible in `_last_correct_ratio_p50`.
        correction = max(0.25, min(4.0, float(correction)))
        self._last_correction = correction

        if self._time_based and now_ts is not None:
            if self._last_refit_ts is None:
                dt = 1.0  # cold-start: 1s equivalent to match legacy move size
            else:
                dt = max(0.0, float(now_ts) - float(self._last_refit_ts))
            self._last_refit_ts = float(now_ts)
            alpha = 1.0 - 0.5 ** (dt / float(self._halflife_s))
        else:
            alpha = 1.0 - 0.5 ** (1.0 / float(self._halflife))
        target = max(MIN_GAIN, min(MAX_GAIN, self._gain * correction))
        self._gain = self._gain + alpha * (target - self._gain)
        self._gain = max(MIN_GAIN, min(MAX_GAIN, self._gain))

    # --- query --------------------------------------------------------

    def current_gain(self) -> float:
        return self._gain if self._apply else self._initial_gain

    def snapshot(self) -> dict:
        return {
            "apply": self._apply,
            "initial_gain": self._initial_gain,
            "current_gain": self._gain,
            "last_correction": self._last_correction,
            "samples": self._n_seen,
            "window_pairs": len(self._pairs),
            "min_samples": self._min_samples,
            "halflife_resolved": self._halflife,
            # Phase 2.5 — expose the time-based halflife (None when the
            # count-based fallback is active).
            "halflife_resolved_s": self._halflife_s,
            "time_based": self._time_based,
            "min_gain": MIN_GAIN,
            "max_gain": MAX_GAIN,
            # WS3 H2 Option 1a diagnostics. `dropped_fraction` cross-checks
            # the ForecastAuditor's directional-hit rate — they should
            # match within the MIN_PRED_BPS filter. The correct-direction
            # quartile triple shows magnitude bias pre-clamp.
            "last_dropped_fraction": self._last_dropped_fraction,
            "last_correct_ratio_p25": self._last_correct_ratio_p25,
            "last_correct_ratio_p50": self._last_correct_ratio_p50,
            "last_correct_ratio_p75": self._last_correct_ratio_p75,
        }

    # --- persistence --------------------------------------------------

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "gain": self._gain,
            "n": self._n_seen,
            "last_correction": self._last_correction,
            "last_refit_ts": self._last_refit_ts,
            # WS3 H2 Option 1a — persist the dropped fraction so a
            # restart continues to report a meaningful diagnostic number
            # before the first post-restart refit runs. The quartile
            # triple is not persisted (it gets recomputed on first refit
            # and operators reading the disk file don't need pre-refit
            # values).
            "last_dropped_fraction": self._last_dropped_fraction,
        }
        try:
            self._path.write_bytes(serde.dumps_indent(payload))
        except OSError as e:
            logger.warning("[kernel-cal] could not persist gain: {}", e)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = serde.loads(self._path.read_bytes())
        except Exception as e:
            logger.warning("[kernel-cal] gain file unreadable: {}", e)
            return
        gain = data.get("gain")
        if isinstance(gain, (int, float)):
            self._gain = max(MIN_GAIN, min(MAX_GAIN, float(gain)))
        if isinstance(data.get("n"), int):
            self._n_seen = int(data["n"])
        if isinstance(data.get("last_correction"), (int, float)):
            self._last_correction = float(data["last_correction"])
        last_refit = data.get("last_refit_ts")
        if isinstance(last_refit, (int, float)):
            self._last_refit_ts = float(last_refit)
        dropped = data.get("last_dropped_fraction")
        if isinstance(dropped, (int, float)):
            self._last_dropped_fraction = float(dropped)
        logger.info(
            "[kernel-cal] resumed gain={:.2f} from {} samples "
            "(last dropped-fraction={:.2%})",
            self._gain, self._n_seen, self._last_dropped_fraction,
        )
