"""Decision engine — Forecast → entry-or-skip TradeIntent.

The decision engine only handles ENTRIES. Exits are handled each tick by
the executor's `evaluate_exits()` which closes positions when the council's
live forecast flips against them. The tick loop:

    1) votes → forecast
    2) executor.evaluate_exits(forecast)  — close council-opposed positions
    3) executor.mark_to_market(ts, price) — max_hold backstop
    4) decision.decide(forecast, ...)     — open a NEW position if non-flat
    5) executor.execute(intent)           — stack it into the portfolio

Council-driven entry AND exit: the same forecast signal that opens trades
also decides when to close them. No TP/SL brackets — positions live as
long as the council forecasts in their direction.

Per-trade size scales with forecast confidence so noisy regimes trade small.
Risk caps (max_concurrent_positions, max_total_notional_pct) live in the
executor — portfolio-level, not signal gates.
"""
from __future__ import annotations

from datetime import datetime

from ..core.types import Forecast, Side, TradeIntent
from ..tribes.market_state import MarketState


class DecisionEngine:
    def __init__(self, settings: dict) -> None:
        r = settings.get("risk", {})
        self.max_hold_seconds = int(r.get("max_hold_seconds", 600))

        s = settings.get("sizing", {})
        self.size_min_pct = float(s.get("min_pct_equity", 0.5))
        self.size_max_pct = float(s.get("max_pct_equity", 4.0))
        self.size_curve = str(s.get("curve", "linear")).lower()

        self.leverage = float(settings.get("leverage", 1.0))

        d = settings.get("decision", {})
        self.min_conf = float(d.get("min_conf", 0.10))

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
        if conf < self.min_conf:
            return self._hold(ts, price, emove, conf, f"council incoherent conf={conf:.3f}")

        side = Side.BUY if emove > 0 else Side.SELL
        pct = self._size_pct(conf)
        # Notional = equity × pct × leverage.
        size_usd = equity_usd * pct / 100.0 * self.leverage
        horizon = min(self.max_hold_seconds, max(30, forecast.horizon_seconds))
        action = "open_long" if side is Side.BUY else "open_short"

        reason = (f"emove {emove:+.1f}bps conf={conf:.2f} "
                  f"size={pct:.2f}%×{self.leverage:.0f}x({self.size_curve}) "
                  f"hz={horizon}s")
        return TradeIntent(
            ts=ts, action=action, size_usd=size_usd, price_ref=price,
            reason=reason, expected_move_bps=emove, confidence=conf,
            horizon_seconds=horizon,
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
