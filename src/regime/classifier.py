"""Live regime classifier.

Every tick the engine asks: is the market in chop, trend, or cascade?
The answer scales the projector's kernel gain (cascade kernels amplify,
chop kernels dampen) and can later override per-tribe strategy params.

The classifier is intentionally transparent and cheap — a few rolling
statistics over the existing MarketState series, no ML, no blackbox.
Tunable thresholds live in settings.regime.*.

Signals read from MarketState:
  - vol_5m_bps         realized volatility = (max - min) / mid price, 5min window
  - drift_eff_5m       |price_change| / range — directional efficiency in [0,1]
  - signed_drift_5m    signed version (positive = up trend)
  - liq_60s_usd        long + short liquidations over the last minute
  - taker_imbalance    |taker_flow| / max(|taker_flow|_abs, 1)

Regime labels:
  cascade     liq > cascade_liq_usd OR vol jumps above cascade_vol_bps
  trend_up    drift_eff above trend_eff and signed positive
  trend_down  drift_eff above trend_eff and signed negative
  chop        vol < chop_vol_bps OR drift_eff < chop_eff
  normal      everything else

`Regime` is returned by `classify()` with the multiplier already looked
up from settings. The engine pushes `projector.regime_multiplier = r.multiplier`
each tick and the projector folds it into the kernel at step-compute time.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..tribes.market_state import MarketState


class RegimeLabel(str, Enum):
    CHOP = "chop"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CASCADE_UP = "cascade_up"
    CASCADE_DOWN = "cascade_down"
    NORMAL = "normal"


@dataclass(slots=True)
class Regime:
    label: RegimeLabel
    multiplier: float       # scales projector gain this tick
    vol_bps: float          # realized 5min vol, for logging
    drift_eff: float        # directional efficiency in [-1, 1]
    liq_60s_usd: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "label": self.label.value,
            "multiplier": self.multiplier,
            "vol_bps": self.vol_bps,
            "drift_eff": self.drift_eff,
            "liq_60s_usd": self.liq_60s_usd,
            "reason": self.reason,
        }


_DEFAULT_MULTS = {
    RegimeLabel.CHOP:         0.6,
    RegimeLabel.NORMAL:       1.0,
    RegimeLabel.TREND_UP:     1.2,
    RegimeLabel.TREND_DOWN:   1.2,
    RegimeLabel.CASCADE_UP:   1.7,
    RegimeLabel.CASCADE_DOWN: 1.7,
}


class RegimeClassifier:
    """Single-tick regime labeler. Stateless between calls except for the
    thresholds; the classifier reads everything it needs off MarketState."""

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.vol_window_s = float(cfg.get("vol_window_s", 300.0))
        self.liq_window_s = float(cfg.get("liq_window_s", 60.0))
        self.chop_vol_bps = float(cfg.get("chop_vol_bps", 15.0))
        self.cascade_vol_bps = float(cfg.get("cascade_vol_bps", 60.0))
        self.chop_eff = float(cfg.get("chop_eff", 0.30))
        self.trend_eff = float(cfg.get("trend_eff", 0.55))
        self.cascade_liq_usd = float(cfg.get("cascade_liq_usd", 5_000_000.0))
        self.multipliers = {
            label: float(cfg.get("multipliers", {}).get(label.value, default))
            for label, default in _DEFAULT_MULTS.items()
        }

    def classify(self, state: MarketState) -> Regime:
        vol_bps, drift_eff, signed_drift = self._volatility(state)
        liq_usd = (
            state.long_liq_usd(self.liq_window_s)
            + state.short_liq_usd(self.liq_window_s)
        )
        # Cascade takes priority — a tight thin-book unwind is a distinct
        # regime even when drift-efficiency looks low (liquidations are
        # discrete, not smooth).
        if liq_usd >= self.cascade_liq_usd or vol_bps >= self.cascade_vol_bps:
            label = (
                RegimeLabel.CASCADE_UP if signed_drift >= 0
                else RegimeLabel.CASCADE_DOWN
            )
            reason = (f"liq=${liq_usd:,.0f} vol={vol_bps:.1f}bps "
                      f"eff={drift_eff:.2f}")
            return self._make(label, vol_bps, drift_eff, liq_usd, reason)

        if vol_bps <= self.chop_vol_bps or drift_eff <= self.chop_eff:
            reason = f"vol={vol_bps:.1f}bps eff={drift_eff:.2f} → chop"
            return self._make(RegimeLabel.CHOP, vol_bps, drift_eff, liq_usd, reason)

        if drift_eff >= self.trend_eff:
            label = (
                RegimeLabel.TREND_UP if signed_drift >= 0
                else RegimeLabel.TREND_DOWN
            )
            reason = f"vol={vol_bps:.1f}bps eff={drift_eff:.2f} → {label.value}"
            return self._make(label, vol_bps, drift_eff, liq_usd, reason)

        return self._make(
            RegimeLabel.NORMAL, vol_bps, drift_eff, liq_usd,
            f"vol={vol_bps:.1f}bps eff={drift_eff:.2f} → normal",
        )

    def _volatility(self, state: MarketState) -> tuple[float, float, float]:
        mm = state.price_range(self.vol_window_s)
        if mm is None or mm[0] <= 0:
            return 0.0, 0.0, 0.0
        lo, hi = mm
        mid = 0.5 * (lo + hi)
        vol_bps = (hi - lo) / mid * 10_000.0 if mid > 0 else 0.0
        # Directional efficiency: how much of the range was a net move?
        # Near 1.0 = clean trend, near 0.0 = chop / mean reversion.
        signed_drift_bps = state.price_change_bps(self.vol_window_s)
        eff = min(1.0, abs(signed_drift_bps) / vol_bps) if vol_bps > 0 else 0.0
        return vol_bps, eff, signed_drift_bps

    def _make(
        self,
        label: RegimeLabel,
        vol_bps: float,
        drift_eff: float,
        liq_usd: float,
        reason: str,
    ) -> Regime:
        return Regime(
            label=label,
            multiplier=self.multipliers[label],
            vol_bps=vol_bps,
            drift_eff=drift_eff,
            liq_60s_usd=liq_usd,
            reason=reason,
        )
