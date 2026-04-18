"""Decision engine — Forecast + current Position → TradeIntent.

Gating rules (all configurable via settings.yaml > decision):
  - expected move must exceed `min_expected_move_bps` (cover fees + slippage).
  - forecast confidence must exceed `min_confidence`.
  - last-step uncertainty must be below `max_uncertainty`.
  - position reversals: if we're long but forecast flips strongly short
    (or vice versa), close. Re-entry on the next tick if still valid.
  - horizon expiry: close when the position has been held longer than
    its horizon_seconds (set at entry from forecast).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..core.types import Forecast, Position, Side, TradeIntent
from ..tribes.market_state import MarketState


class DecisionEngine:
    def __init__(self, settings: dict) -> None:
        d = settings.get("decision", {})
        self.min_expected_move_bps = float(d.get("min_expected_move_bps", 8.0))
        self.min_confidence = float(d.get("min_confidence", 0.15))
        self.max_uncertainty = float(d.get("max_uncertainty", 0.90))
        self.reversal_factor = float(d.get("reversal_factor", 1.2))

        r = settings.get("risk", {})
        self.min_hold_seconds = int(r.get("min_hold_seconds", 30))
        self.max_hold_seconds = int(r.get("max_hold_seconds", 600))

        s = settings.get("sizing", {})
        self.size_min_pct = float(s.get("min_pct_equity", 0.5))
        self.size_max_pct = float(s.get("max_pct_equity", 4.0))
        self.size_curve = str(s.get("curve", "linear")).lower()

    def decide(
        self,
        ts: datetime,
        forecast: Forecast,
        position: Position,
        state: MarketState,
        equity_usd: float,
    ) -> TradeIntent:
        price = state.last_price or 0.0
        emove = forecast.expected_move_bps
        conf = forecast.confidence
        unc = forecast.steps[-1].uncertainty if forecast.steps else 1.0

        # -- exits first ---------------------------------------------------
        if not position.is_flat:
            held_s = (ts - position.entry_ts).total_seconds() if position.entry_ts else 0
            # Minimum hold window — avoid chatter
            if held_s < self.min_hold_seconds:
                return self._hold(ts, price, emove, conf, "within min-hold window")
            # Max hold expiry
            if held_s >= self.max_hold_seconds:
                return self._close(ts, price, emove, conf, f"max hold {held_s:.0f}s reached")
            # Horizon expiry
            if position.horizon_seconds and held_s >= position.horizon_seconds:
                return self._close(ts, price, emove, conf, f"horizon {position.horizon_seconds}s reached")
            # Strong reversal
            flipped_short = position.side is Side.BUY and emove < -self.min_expected_move_bps * self.reversal_factor
            flipped_long = position.side is Side.SELL and emove > self.min_expected_move_bps * self.reversal_factor
            if (flipped_short or flipped_long) and conf >= self.min_confidence:
                return self._close(ts, price, emove, conf, f"reversal emove={emove:+.1f}bps")
            return self._hold(ts, price, emove, conf, "position running")

        # -- entry gates ---------------------------------------------------
        if conf < self.min_confidence:
            return self._hold(ts, price, emove, conf, f"conf {conf:.2f} below floor")
        if unc > self.max_uncertainty:
            return self._hold(ts, price, emove, conf, f"uncertainty {unc:.2f} too high")
        if abs(emove) < self.min_expected_move_bps:
            return self._hold(ts, price, emove, conf, f"emove {emove:+.1f}bps below threshold")

        pct = self._size_pct(conf)
        size_usd = equity_usd * pct / 100.0
        horizon = min(self.max_hold_seconds, max(self.min_hold_seconds, forecast.horizon_seconds))
        action = "open_long" if emove > 0 else "open_short"
        return TradeIntent(
            ts=ts, action=action, size_usd=size_usd, price_ref=price,
            reason=(f"emove {emove:+.1f}bps conf={conf:.2f} unc={unc:.2f} "
                    f"size={pct:.2f}%({self.size_curve}) horizon={horizon}s"),
            expected_move_bps=emove, confidence=conf,
        )

    def _size_pct(self, conf: float) -> float:
        """Map forecast confidence → % of equity injected into the trade.
        At conf = min_confidence, use `min_pct_equity`. At conf = 1.0, use
        `max_pct_equity`. `linear` is proportional; `quadratic` weights big
        confidence much more than small — together the two curves let us
        ask empirically whether high-conf edges are a linear or convex
        phenomenon. Clamped at the bracket ends."""
        lo = self.min_confidence
        if conf <= lo:
            return self.size_min_pct
        if conf >= 1.0:
            return self.size_max_pct
        t = (conf - lo) / max(1.0 - lo, 1e-9)
        if self.size_curve == "quadratic":
            t = t * t
        return self.size_min_pct + (self.size_max_pct - self.size_min_pct) * t

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _hold(ts, price, emove, conf, reason) -> TradeIntent:
        return TradeIntent(ts=ts, action="hold", size_usd=0.0, price_ref=price,
                           reason=reason, expected_move_bps=emove, confidence=conf)

    @staticmethod
    def _close(ts, price, emove, conf, reason) -> TradeIntent:
        return TradeIntent(ts=ts, action="close", size_usd=0.0, price_ref=price,
                           reason=reason, expected_move_bps=emove, confidence=conf)
