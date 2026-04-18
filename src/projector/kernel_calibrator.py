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

import orjson
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
    halflife_resolved : int
        EWMA halflife in resolved anchors. Controls how fast the gain
        adapts — smaller = more responsive (and noisier).
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
        min_samples: int = 50,
        window: int = 2000,
        apply: bool = True,
    ) -> None:
        self.dir = directory or DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path = self.dir / "projector_gain.json"
        self._initial_gain = max(MIN_GAIN, min(MAX_GAIN, float(initial_gain)))
        self._halflife = max(1, int(halflife_resolved))
        self._min_samples = max(1, int(min_samples))
        self._window = max(50, int(window))
        self._apply = bool(apply)

        self._pairs: deque[tuple[float, float]] = deque(maxlen=self._window)
        self._gain: float = self._initial_gain
        self._n_seen: int = 0
        self._last_correction: float = 1.0
        self._load()

    # --- ingest -------------------------------------------------------

    def apply_resolved(self, resolved: Iterable[Resolved]) -> None:
        """Accumulate informative pairs, refit if we're past min_samples."""
        added = 0
        for rec in resolved:
            if abs(rec.expected_move_bps) < MIN_PRED_BPS:
                continue
            self._pairs.append((rec.expected_move_bps, rec.realized_move_bps))
            self._n_seen += 1
            added += 1
        if added and self._n_seen >= self._min_samples:
            self._refit()
            self._save()

    def _refit(self) -> None:
        """Robust median of realized/predicted over the window, EWMA-smoothed."""
        if not self._pairs:
            return
        ratios = [r / p for p, r in self._pairs if p != 0]
        if not ratios:
            return
        ratios.sort()
        mid = len(ratios) // 2
        correction = (
            ratios[mid] if len(ratios) % 2 == 1
            else 0.5 * (ratios[mid - 1] + ratios[mid])
        )
        # Huge one-sided corrections usually mean the window straddled a
        # regime switch — trim so the EWMA can catch up without a giant step.
        correction = max(0.25, min(4.0, float(correction)))
        self._last_correction = correction

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
            "min_gain": MIN_GAIN,
            "max_gain": MAX_GAIN,
        }

    # --- persistence --------------------------------------------------

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "gain": self._gain,
            "n": self._n_seen,
            "last_correction": self._last_correction,
        }
        try:
            self._path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        except OSError as e:
            logger.warning("[kernel-cal] could not persist gain: {}", e)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = orjson.loads(self._path.read_bytes())
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
        logger.info("[kernel-cal] resumed gain={:.2f} from {} samples",
                    self._gain, self._n_seen)
