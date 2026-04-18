"""Decision engine — Forecast → entry-or-skip TradeIntent.

In the portfolio model the decision engine ONLY evaluates entries. Every
open position carries its own TP/SL brackets that the executor enforces
mark-to-market. The tick loop therefore looks like:

    1) executor.mark_to_market(price)   — close anything that hit bracket
    2) votes → forecast
    3) decision.decide(forecast, ...)   — open a NEW position iff the
                                          council is not flat
    4) executor.execute(intent)         — stack it into the portfolio

Council-driven entry: the council's signed forecast IS the trading signal.
No confidence / uncertainty / emove-floor / fee-floor gates — every
non-flat council projection opens a trade. Bad predictions surface in
backscore (MAE / hit-rate per horizon) and in the per-tribe skill EWMA,
not as pre-filtered silence. If the council is mis-balanced or a tribe is
missing context, the fix is to audit + retune, not to add gates that hide
the problem.

Per-trade size scales with forecast confidence so trades in noisy regimes
cost less; TP at the forecasted move; SL at sl_ratio × |emove| floored at
sl_min_bps. Risk caps (max_concurrent_positions, max_total_notional_pct)
live in the executor — those are portfolio-level, not signal gates.
"""
from __future__ import annotations

from datetime import datetime

from ..core.types import Forecast, Side, TradeIntent
from ..tribes.market_state import MarketState


class DecisionEngine:
    def __init__(self, settings: dict) -> None:
        d = settings.get("decision", {})
        self.sl_ratio = float(d.get("sl_ratio", 0.7))
        self.sl_min_bps = float(d.get("sl_min_bps", 20.0))

        r = settings.get("risk", {})
        self.max_hold_seconds = int(r.get("max_hold_seconds", 600))

        s = settings.get("sizing", {})
        self.size_min_pct = float(s.get("min_pct_equity", 0.5))
        self.size_max_pct = float(s.get("max_pct_equity", 4.0))
        self.size_curve = str(s.get("curve", "linear")).lower()

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

        # The only non-council gate: we need a live mark to price the
        # entry + brackets against. Everything else is the council's call.
        if price <= 0:
            return self._hold(ts, price, emove, conf, "no mark price")
        if emove == 0:
            return self._hold(ts, price, emove, conf, "council flat")

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

        reason = (f"emove {emove:+.1f}bps conf={conf:.2f} "
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
        conf ∈ [0, 1] maps linearly (or quadratically) between
        `size_min_pct` and `size_max_pct`. No gate — even conf=0 opens a
        minimum-size trade so noisy regimes trade small, not silent."""
        c = max(0.0, min(1.0, conf))
        if self.size_curve == "quadratic":
            c = c * c
        return self.size_min_pct + (self.size_max_pct - self.size_min_pct) * c

    @staticmethod
    def _hold(ts, price, emove, conf, reason) -> TradeIntent:
        return TradeIntent(ts=ts, action="hold", size_usd=0.0, price_ref=price,
                           reason=reason, expected_move_bps=emove, confidence=conf)
