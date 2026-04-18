"""Decision engine — Forecast → entry-or-skip TradeIntent.

In the portfolio model the decision engine ONLY evaluates entries. Every
open position carries its own TP/SL brackets that the executor enforces
mark-to-market. The tick loop therefore looks like:

    1) executor.mark_to_market(price)   — close anything that hit bracket
    2) votes → forecast
    3) decision.decide(forecast, ...)   — maybe open one NEW position
    4) executor.execute(intent)         — stack it into the portfolio

Entry gating (all configurable via settings.yaml > decision):
  - forecast confidence ≥ min_confidence
  - last-step uncertainty ≤ max_uncertainty
  - |expected_move_bps| ≥ round_trip_fee_bps + fee_safety_margin_bps
      (we refuse trades that lose even if the forecast is right — fees
      alone would eat the move)

At entry we compute:
  - size_usd = equity × size_pct(conf) × leverage        (notional)
  - TP = entry × (1 ± expected_move_bps/10000)           (the forecast)
  - SL = entry × (1 ∓ max(|expected_move|×sl_ratio, sl_min_bps)/10000)
"""
from __future__ import annotations

from datetime import datetime

from ..core.types import Forecast, Side, TradeIntent
from ..tribes.market_state import MarketState


class DecisionEngine:
    def __init__(self, settings: dict) -> None:
        d = settings.get("decision", {})
        self.min_expected_move_bps = float(d.get("min_expected_move_bps", 8.0))
        self.min_confidence = float(d.get("min_confidence", 0.15))
        self.max_uncertainty = float(d.get("max_uncertainty", 0.90))
        self.fee_safety_margin_bps = float(d.get("fee_safety_margin_bps", 2.0))
        self.sl_ratio = float(d.get("sl_ratio", 0.7))
        self.sl_min_bps = float(d.get("sl_min_bps", 20.0))

        r = settings.get("risk", {})
        self.max_hold_seconds = int(r.get("max_hold_seconds", 600))

        s = settings.get("sizing", {})
        self.size_min_pct = float(s.get("min_pct_equity", 0.5))
        self.size_max_pct = float(s.get("max_pct_equity", 4.0))
        self.size_curve = str(s.get("curve", "linear")).lower()

        e = settings.get("execution", {})
        self.fee_rate_bps = float(e.get("fee_rate_bps", 5.0))
        self.slippage_bps = float(e.get("slippage_bps", 2.0))
        # Round-trip = entry + exit. Each leg pays fee + slippage.
        self.round_trip_bps = 2.0 * (self.fee_rate_bps + self.slippage_bps)

        self.leverage = float(settings.get("leverage", 1.0))

    def decide(
        self,
        ts: datetime,
        forecast: Forecast,
        state: MarketState,
        equity_usd: float,
    ) -> TradeIntent:
        price = state.last_price or 0.0
        emove = forecast.expected_move_bps
        conf = forecast.confidence
        unc = forecast.steps[-1].uncertainty if forecast.steps else 1.0

        # Fee-aware move floor. No trade opens unless the forecast clears
        # the round-trip cost with margin — even a correct forecast below
        # this threshold loses money after fees.
        net_floor = max(self.min_expected_move_bps,
                        self.round_trip_bps + self.fee_safety_margin_bps)

        if conf < self.min_confidence:
            return self._hold(ts, price, emove, conf, f"conf {conf:.2f} below floor")
        if unc > self.max_uncertainty:
            return self._hold(ts, price, emove, conf, f"uncertainty {unc:.2f} too high")
        if abs(emove) < net_floor:
            return self._hold(ts, price, emove, conf,
                              f"emove {emove:+.1f}bps below net floor {net_floor:.1f}bps")
        if price <= 0:
            return self._hold(ts, price, emove, conf, "no mark price")

        side = Side.BUY if emove > 0 else Side.SELL
        pct = self._size_pct(conf)
        # Notional = equity × pct × leverage. Leverage turns a $5k equity
        # into a meaningful notional per trade; fees and P&L scale with it.
        size_usd = equity_usd * pct / 100.0 * self.leverage
        horizon = min(self.max_hold_seconds, max(30, forecast.horizon_seconds))

        # TP at the forecasted move; SL at sl_ratio × |emove| (floored).
        tp_bps = abs(emove)
        sl_bps = max(abs(emove) * self.sl_ratio, self.sl_min_bps)
        if side is Side.BUY:
            tp_price = price * (1 + tp_bps / 10_000.0)
            sl_price = price * (1 - sl_bps / 10_000.0)
            action = "open_long"
        else:
            tp_price = price * (1 - tp_bps / 10_000.0)
            sl_price = price * (1 + sl_bps / 10_000.0)
            action = "open_short"

        reason = (f"emove {emove:+.1f}bps conf={conf:.2f} unc={unc:.2f} "
                  f"size={pct:.2f}%×{self.leverage:.0f}x({self.size_curve}) "
                  f"TP={tp_bps:.0f}bps SL={sl_bps:.0f}bps hz={horizon}s")
        return TradeIntent(
            ts=ts, action=action, size_usd=size_usd, price_ref=price,
            reason=reason,
            expected_move_bps=emove, confidence=conf,
            tp_price=tp_price, sl_price=sl_price, horizon_seconds=horizon,
        )

    def _size_pct(self, conf: float) -> float:
        """Map forecast confidence → % of equity per trade.
        At conf = min_confidence, use `min_pct_equity`. At conf = 1.0, use
        `max_pct_equity`. `linear` is proportional; `quadratic` weights big
        confidence much more. Clamped at the bracket ends."""
        lo = self.min_confidence
        if conf <= lo:
            return self.size_min_pct
        if conf >= 1.0:
            return self.size_max_pct
        t = (conf - lo) / max(1.0 - lo, 1e-9)
        if self.size_curve == "quadratic":
            t = t * t
        return self.size_min_pct + (self.size_max_pct - self.size_min_pct) * t

    @staticmethod
    def _hold(ts, price, emove, conf, reason) -> TradeIntent:
        return TradeIntent(ts=ts, action="hold", size_usd=0.0, price_ref=price,
                           reason=reason, expected_move_bps=emove, confidence=conf)
