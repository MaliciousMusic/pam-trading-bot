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
        # Sigma-aware sizing — shrink the trade when the projector's terminal
        # uncertainty fan is wide (unpredictable regime), leave it alone when
        # sigma is tight (trend regime). Factor = clamp(ref/sigma, floor, 1.0).
        # `ref_bps` is the sigma below which no shrink is applied; `floor` is
        # the worst-case shrink so chaotic conditions still leave a scout
        # position on the table instead of silencing the bot.
        self.sigma_ref_bps = float(d.get("sigma_ref_bps", 40.0))
        self.sigma_shrink_floor = float(d.get("sigma_shrink_floor", 0.35))

        # Higher-timeframe trend gate. When the council's direction opposes
        # the 1h EMA-spread bias, shrink size by up to `htf_counter_strength`
        # scaled by |trend_score|. Aligned entries are untouched — HTF is a
        # brake for counter-trend trades, not a gas pedal for with-trend ones.
        # `htf_max_age_s` (default 2h) treats stale polls as no-signal.
        # Set `htf_counter_strength=0.0` to disable.
        htf = settings.get("htf", {}) or {}
        self.htf_counter_strength = max(0.0, min(1.0, float(htf.get("counter_strength", 0.0))))
        self.htf_max_age_s = float(htf.get("max_age_seconds", 7200.0))

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
        sigma_mult = self._sigma_multiplier(forecast)
        # Directional-agreement booster from per-tribe MC paths (see
        # ChessProjector). Multiplier in [0.5, 1.0]: fully-aligned paths
        # trade normally; 50/50 splits halve the size. Missing → 1.0.
        agree_mult = 1.0
        if getattr(forecast, "directional_agreement", None) is not None:
            a = float(forecast.directional_agreement)
            # Map [0.5, 1.0] → [0.5, 1.0] linearly; below 0.5 shouldn't
            # happen because we'd have flipped direction, but clamp anyway.
            agree_mult = max(0.5, min(1.0, a))
        htf_mult = self._htf_multiplier(ts, state, side)
        # Notional = equity × pct × leverage × sigma × agree × htf.
        size_usd = (equity_usd * pct / 100.0 * self.leverage
                    * sigma_mult * agree_mult * htf_mult)
        horizon = min(self.max_hold_seconds, max(30, forecast.horizon_seconds))
        action = "open_long" if side is Side.BUY else "open_short"
        peak_s = self._peak_favorable_seconds(forecast, side)

        reason = (f"emove {emove:+.1f}bps conf={conf:.2f} "
                  f"size={pct:.2f}%×{self.leverage:.0f}x({self.size_curve}) "
                  f"σ×{sigma_mult:.2f} agree×{agree_mult:.2f} htf×{htf_mult:.2f} "
                  f"hz={horizon}s")
        return TradeIntent(
            ts=ts, action=action, size_usd=size_usd, price_ref=price,
            reason=reason, expected_move_bps=emove, confidence=conf,
            horizon_seconds=horizon, peak_favorable_s=peak_s,
        )

    def _peak_favorable_seconds(self, forecast: Forecast, side: Side) -> int:
        """Seconds from entry at which the projected curve peaks in the
        trade's favor. For a long, the argmax of steps_bps; for a short, the
        argmin. Returns 0 when the curve is missing or the peak is at step 0
        (nothing to scalp to)."""
        curve = getattr(forecast, "steps_bps", None)
        if not curve:
            return 0
        if side is Side.BUY:
            idx = max(range(len(curve)), key=lambda i: curve[i])
        else:
            idx = min(range(len(curve)), key=lambda i: curve[i])
        if idx <= 0:
            return 0
        # steps are 1-indexed in time: step i is (i+1)*tick_seconds from entry.
        step_seconds = forecast.horizon_seconds / max(1, len(curve))
        return int((idx + 1) * step_seconds)

    def _htf_multiplier(self, ts: datetime, state: MarketState, side: Side) -> float:
        """Shrink counter-trend entries by up to `htf_counter_strength`.
        Aligned entries get 1.0. Score is ignored if stale or if the gate
        is disabled (counter_strength=0). Formula for counter-trend:
            mult = 1 - counter_strength * |score|
        |score|=1.0 & strength=0.6 → 0.4× size on a full-opposition trade.
        """
        if self.htf_counter_strength <= 0:
            return 1.0
        score = float(getattr(state, "htf_trend_score", 0.0) or 0.0)
        if score == 0.0:
            return 1.0
        htf_ts = getattr(state, "htf_trend_ts", None)
        if htf_ts is not None and self.htf_max_age_s > 0:
            try:
                age_s = (ts - htf_ts).total_seconds()
            except Exception:
                age_s = 0.0
            if age_s > self.htf_max_age_s:
                return 1.0
        # Aligned if score and side agree in sign.
        aligned = (side is Side.BUY and score > 0) or (side is Side.SELL and score < 0)
        if aligned:
            return 1.0
        shrink = self.htf_counter_strength * min(1.0, abs(score))
        return max(0.0, 1.0 - shrink)

    def _sigma_multiplier(self, forecast: Forecast) -> float:
        """Shrink the position when the projector's terminal sigma is wider
        than `sigma_ref_bps`. Returns 1.0 when sigma ≤ ref, `sigma_shrink_floor`
        when sigma is so wide that the trade should be nearly silent. Falls
        back to 1.0 when the forecast lacks a sigma curve (older callers)."""
        sigma_list = getattr(forecast, "steps_sigma_bps", None)
        if not sigma_list:
            return 1.0
        sigma_terminal = float(sigma_list[-1])
        if sigma_terminal <= 0 or self.sigma_ref_bps <= 0:
            return 1.0
        raw = self.sigma_ref_bps / sigma_terminal
        return max(self.sigma_shrink_floor, min(1.0, raw))

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
