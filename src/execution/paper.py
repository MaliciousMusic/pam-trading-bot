"""Paper executor — simulates fills with fees + slippage across a portfolio
of simultaneously-open positions.

Exit model: every tick the council's live forecast is evaluated against every
open position via `evaluate_exits()`. The base rule is a sign-flip beyond the
configured dead-zone; four optional layers stack on top:

  1. **Conviction-magnitude** — weak flips (conf < strong_flip_conf) must
     persist for `weak_flip_confirm_ticks` before closing; strong flips close
     immediately. Streaks reset when the council realigns.
  2. **Profit ratchet** — once a position's peak unrealized reaches
     `ratchet_arm_bps`, it closes on a retrace of `ratchet_retrace_frac` of
     that peak. Arm threshold must clear round-trip fees or the close books
     a net loss.
  2b. **Forecast-target TP** — close when unrealized reaches the move the
     council promised at entry (|emove| × conf × forecast_tp_factor).
     Suppressed when the target is below round-trip fees. This is the only
     layer that takes profit at the top; the ratchet just protects the way
     down.
  3. **Regime-aware dead zone + edge exhaustion** — dead zone scales by
     regime (cascade tighter, chop wider); if remaining forecasted move in
     the position's direction no longer covers fees, close.

`mark_to_market()` remains as a hard backstop for `max_hold_seconds`.

Fees and slippage are charged on both entry and exit of every position.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from ..core.types import Position, Side, TradeIntent
from ..tribes.market_state import MarketState
from .base import Executor
from .ledger import Ledger


class PaperExecutor(Executor):
    def __init__(
        self,
        starting_equity_usd: float,
        fee_rate_bps: float = 5.0,
        slippage_bps: float = 2.0,
        max_concurrent_positions: int = 20,
        max_total_notional_pct: float = 500.0,
        max_hold_seconds: int = 600,
        exit_dead_zone_bps: float = 0.5,
        ledger: Ledger | None = None,
        *,
        exit_strategy: dict | None = None,
        max_drawdown_from_peak_pct: float = 0.0,
        max_daily_loss_pct: float = 0.0,
        slippage_size_k: float = 0.0,
        slippage_ref_notional_usd: float = 100_000.0,
    ) -> None:
        self._equity = starting_equity_usd
        self._start_equity = starting_equity_usd
        self._positions: list[Position] = []
        self.fee_rate_bps = fee_rate_bps
        self.slippage_bps = slippage_bps
        self.slippage_size_k = max(0.0, float(slippage_size_k))
        self.slippage_ref_notional_usd = max(1.0, float(slippage_ref_notional_usd))
        self.max_concurrent_positions = max_concurrent_positions
        self.max_total_notional_pct = max_total_notional_pct
        self.max_hold_seconds = max_hold_seconds
        self.exit_dead_zone_bps = exit_dead_zone_bps
        self.ledger = ledger or Ledger()
        self.trades_completed = 0
        self.wins = 0
        self._next_id = 1
        self._recent_trades: list[dict] = []   # last 60 closed, newest last

        # Drawdown circuit breaker. Two halts, either trips:
        #   1. equity <= session_peak × (1 - max_drawdown_from_peak_pct/100)
        #   2. equity <= day_anchor × (1 - max_daily_loss_pct/100)
        # While halted, `_capacity_check` rejects new entries; exits and
        # mark-to-market continue unaffected so we don't trap open risk.
        # Daily halt clears on UTC date roll (anchor re-captured). Peak
        # halt stays until equity recovers back to the halt level — any
        # recovery is authentic, not a rounding flap.
        # Both thresholds default to 0.0 = disabled.
        self.max_drawdown_from_peak_pct = max(0.0, float(max_drawdown_from_peak_pct))
        self.max_daily_loss_pct = max(0.0, float(max_daily_loss_pct))
        self._peak_equity = starting_equity_usd
        self._day_anchor_date = None       # type: ignore[assignment]
        self._day_anchor_equity = starting_equity_usd
        self._halt_reason: str | None = None

        # Multi-layer exit overlay. All layers opt-in via settings; neutral
        # defaults preserve the sign-flip-only behavior.
        es = exit_strategy or {}
        self.strong_flip_conf = float(es.get("strong_flip_conf", 0.25))
        self.weak_flip_confirm_ticks = max(1, int(es.get("weak_flip_confirm_ticks", 2)))
        self.ratchet_arm_bps = float(es.get("ratchet_arm_bps", 0.0))
        self.ratchet_retrace_frac = float(es.get("ratchet_retrace_frac", 1.0))
        self.dead_zone_regime_mult: dict[str, float] = dict(
            es.get("dead_zone_regime_mult", {}) or {}
        )
        self.edge_exhausted_factor = float(es.get("edge_exhausted_factor", 0.0))
        self.edge_exhausted_min_age_s = float(es.get("edge_exhausted_min_age_s", 20.0))
        self.forecast_tp_factor = float(es.get("forecast_tp_factor", 0.0))
        self.forecast_tp_min_bps = float(es.get("forecast_tp_min_bps", 0.0))
        # Shape-aware exit — close if the position has been held past the
        # forecasted favorable-peak step by `shape_grace_seconds`, but only
        # once it's in the green (never turn this into a stop-loss). 0 = off.
        self.shape_grace_seconds = float(es.get("shape_grace_seconds", 0.0))
        # Per-position state for layered exits. Keyed by Position.id; purged
        # on close. Dict lookups are O(1) and we never have more than
        # max_concurrent_positions entries, so the memory story is bounded.
        self._flip_streak: dict[str, int] = {}
        self._peak_bps: dict[str, float] = {}

    # -- Executor interface ---------------------------------------------

    @property
    def recent_trades(self) -> list[dict]:
        return self._recent_trades

    @property
    def equity_usd(self) -> float:
        return self._equity

    @property
    def positions(self) -> list[Position]:
        return self._positions

    def evaluate_exits(
        self,
        ts: datetime,
        price: float,
        forecast,
        regime_label: str | None = None,
    ) -> None:
        """Close positions the council no longer supports.

        Layered rule — any trigger closes the position:
          • sign-flip beyond regime-scaled dead zone, gated by
            conviction-magnitude (weak flips need a confirm streak)
          • profit ratchet (peak retrace after arming)
          • edge exhausted (remaining forecasted move can't cover fees)

        Positions that stay aligned with a healthy edge keep running; the
        max_hold backstop in `mark_to_market` is the last line of defense.
        """
        if price is None or price <= 0 or not self._positions:
            return
        emove = forecast.expected_move_bps
        conf = getattr(forecast, "confidence", 0.0)
        dz = self.exit_dead_zone_bps * float(
            self.dead_zone_regime_mult.get(regime_label or "", 1.0)
        )
        # Round-trip fee budget (notional bps) — both sides pay slippage + fee.
        rt_fees_bps = 2.0 * (self.fee_rate_bps + self.slippage_bps)

        survivors: list[Position] = []
        for p in self._positions:
            reason = self._exit_reason(p, price, ts, emove, conf, dz, rt_fees_bps)
            if reason is not None:
                self._close_position(p, price, ts, reason)
            else:
                survivors.append(p)
        self._positions = survivors

    def _exit_reason(
        self,
        p: Position,
        price: float,
        ts: datetime,
        emove: float,
        conf: float,
        dz: float,
        rt_fees_bps: float,
    ) -> str | None:
        """Return a close-reason string if any exit layer fires, else None."""
        side_sign = 1.0 if p.side is Side.BUY else -1.0

        # Layer 1 — sign flip beyond the (regime-scaled) dead zone, gated by
        # flip confidence. Weak flips must persist to close.
        flipped = (p.side is Side.BUY and emove <= -dz) or (
            p.side is Side.SELL and emove >= dz
        )
        if flipped:
            if conf >= self.strong_flip_conf:
                self._flip_streak.pop(p.id, None)
                direction = "short" if p.side is Side.BUY else "long"
                return f"strong flip {direction} emove={emove:+.1f}bps conf={conf:.2f}"
            streak = self._flip_streak.get(p.id, 0) + 1
            self._flip_streak[p.id] = streak
            if streak >= self.weak_flip_confirm_ticks:
                self._flip_streak.pop(p.id, None)
                return (f"confirmed weak flip ×{streak} emove={emove:+.1f}bps "
                        f"conf={conf:.2f}")
        else:
            # Council realigned (or within dead zone) — drop any streak so a
            # future weak flip starts fresh rather than resuming a stale count.
            self._flip_streak.pop(p.id, None)

        # Current unrealized in bps of notional (signed — positive = winning).
        unreal_bps = side_sign * (price - p.entry_price) / p.entry_price * 1e4

        # Layer 2 — profit ratchet. Arms once peak unrealized crosses
        # `ratchet_arm_bps`, then trails at `retrace_frac` of the peak.
        if self.ratchet_arm_bps > 0 and self.ratchet_retrace_frac < 1.0:
            peak = max(self._peak_bps.get(p.id, 0.0), unreal_bps)
            self._peak_bps[p.id] = peak
            if peak >= self.ratchet_arm_bps:
                trigger = peak * (1.0 - self.ratchet_retrace_frac)
                if unreal_bps <= trigger:
                    return (f"ratchet retrace peak={peak:+.1f}bps "
                            f"now={unreal_bps:+.1f}bps")

        # Layer 2b — forecast-target take-profit. Close when the position
        # reaches the move the council promised at entry (|emove| × conf),
        # gated below fees so we never TP into a net loss. This turns a
        # realized forecast into a clean profit-take instead of letting the
        # ratchet give half of it back on the way down.
        if self.forecast_tp_factor > 0.0 and p.expected_move_bps != 0.0:
            target = abs(p.expected_move_bps) * p.confidence * self.forecast_tp_factor
            target = max(target, self.forecast_tp_min_bps)
            if target >= rt_fees_bps and unreal_bps >= target:
                return (f"forecast target hit unreal={unreal_bps:+.1f}bps "
                        f"target={target:.1f}bps")

        # Layer 2c — shape-aware exit. The decision engine stamped each
        # position with the step at which the forecast peaked in its favor;
        # once we've been held past that peak by a grace window AND are in
        # the green, scalp the move instead of riding it back down the hill.
        if (self.shape_grace_seconds > 0.0 and p.peak_favorable_s > 0
                and unreal_bps > 0):
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held_s >= p.peak_favorable_s + self.shape_grace_seconds:
                return (f"past forecast peak held={held_s:.0f}s "
                        f"peak@{p.peak_favorable_s}s unreal={unreal_bps:+.1f}bps")

        # Layer 3b — edge exhausted. If the remaining forecasted move in the
        # position's favor can't cover fees, close even though the council is
        # nominally still aligned. Aged-in check avoids killing fresh entries.
        if self.edge_exhausted_factor > 0.0:
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held_s >= self.edge_exhausted_min_age_s:
                remaining = side_sign * emove
                threshold = rt_fees_bps * self.edge_exhausted_factor
                if remaining < threshold:
                    return (f"edge exhausted remaining={remaining:+.1f}bps "
                            f"< fees×{self.edge_exhausted_factor:.2f}={threshold:.1f}bps")
        return None

    def apply_funding(self, funding_rate: float, tick_seconds: float) -> None:
        """Accrue perpetual-futures funding for every open position.

        Longs pay when funding_rate > 0 (bulls pay bears), shorts receive.
        Shorts pay when funding_rate < 0 (bears pay bulls), longs receive.
        Binance epochs are 8 hours (28 800 s); we prorate per tick.

            payment = size_usd * funding_rate * (tick_seconds / 28_800)
        """
        if not self._positions or funding_rate == 0.0:
            return
        total_paid = 0.0
        for p in self._positions:
            sign = 1.0 if p.side is Side.BUY else -1.0
            payment = p.size_usd * funding_rate * sign * (tick_seconds / 28_800.0)
            self._equity -= payment
            total_paid += payment
        if abs(total_paid) > 0.01:
            logger.debug(
                "FUNDING  net=${:+.4f}  rate={:.6f}  open={}",
                total_paid, funding_rate, len(self._positions),
            )
        self._refresh_risk_state()

    def mark_to_market(self, ts: datetime, price: float) -> None:
        """Hard backstop: close positions that exceeded max_hold_seconds."""
        if price is None or price <= 0 or not self._positions:
            return
        survivors: list[Position] = []
        for p in self._positions:
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0
            if self.max_hold_seconds and held_s >= self.max_hold_seconds:
                self._close_position(p, price, ts, f"max hold {int(held_s)}s")
            else:
                survivors.append(p)
        self._positions = survivors

    def execute(self, intent: TradeIntent, state: MarketState) -> None:
        price = state.last_price
        if price is None or price <= 0:
            return
        if intent.action == "open_long":
            self._open(Side.BUY, intent, price)
        elif intent.action == "open_short":
            self._open(Side.SELL, intent, price)
        elif intent.action == "close":
            ts = intent.ts
            for p in list(self._positions):
                self._close_position(p, price, ts, intent.reason or "manual")
            self._positions = []
        # "hold" → nothing

    # -- internals -------------------------------------------------------

    def _fill_price(self, mark: float, taker_side: Side, size_usd: float = 0.0) -> float:
        """Size-scaled slippage. Baseline is `slippage_bps`; each increment of
        `slippage_ref_notional_usd` adds another `slippage_bps × slippage_size_k`
        of adverse slippage. With `slippage_size_k=0` this collapses to the
        flat-bps model for backward compat."""
        sign = 1 if taker_side is Side.BUY else -1
        eff_bps = self.slippage_bps
        if self.slippage_size_k > 0 and size_usd > 0:
            eff_bps *= 1.0 + self.slippage_size_k * (size_usd / self.slippage_ref_notional_usd)
        return mark * (1 + sign * eff_bps / 10_000.0)

    def _fee(self, notional_usd: float) -> float:
        return notional_usd * self.fee_rate_bps / 10_000.0

    def _refresh_risk_state(self, ts: datetime | None = None) -> None:
        """Update session peak + daily anchor, evaluate halts.

        Called before every capacity check and on every equity-changing
        event (open fee deduction, close PnL, funding). Cheap — three
        comparisons and a date check.
        """
        # Ratchet session peak.
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        # Daily anchor — set on first call, reset when UTC date changes.
        try:
            today = (ts or datetime.utcnow()).date()
        except Exception:
            today = None
        if today is not None and today != self._day_anchor_date:
            self._day_anchor_date = today
            self._day_anchor_equity = self._equity
            # Daily halts clear with the day. Peak halts survive unless
            # equity has recovered; re-evaluate below.
            if self._halt_reason and self._halt_reason.startswith("daily"):
                self._halt_reason = None

        # Peak-drawdown halt. Arm once equity falls below the trigger;
        # stay halted until equity recovers back above the trigger (no
        # hysteresis band — the peak itself ratchets, so "above the old
        # trigger" is a genuine recovery).
        if self.max_drawdown_from_peak_pct > 0:
            trigger = self._peak_equity * (1.0 - self.max_drawdown_from_peak_pct / 100.0)
            if self._equity <= trigger:
                self._halt_reason = (
                    f"peak drawdown ${self._equity:,.2f} <= "
                    f"${trigger:,.2f} (peak ${self._peak_equity:,.2f} "
                    f"- {self.max_drawdown_from_peak_pct:.1f}%)"
                )
            elif self._halt_reason and self._halt_reason.startswith("peak"):
                self._halt_reason = None

        # Daily-loss halt evaluated after peak so a same-tick recovery
        # on the peak halt doesn't mask an independent daily breach.
        if self.max_daily_loss_pct > 0 and self._day_anchor_equity > 0:
            trigger = self._day_anchor_equity * (1.0 - self.max_daily_loss_pct / 100.0)
            if self._equity <= trigger and (
                self._halt_reason is None or self._halt_reason.startswith("peak")
            ):
                self._halt_reason = (
                    f"daily loss ${self._equity:,.2f} <= ${trigger:,.2f} "
                    f"(open ${self._day_anchor_equity:,.2f} "
                    f"- {self.max_daily_loss_pct:.1f}%)"
                )

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def _capacity_check(self, size_usd: float) -> str | None:
        self._refresh_risk_state()
        if self._halt_reason is not None:
            return f"halted: {self._halt_reason}"
        if len(self._positions) >= self.max_concurrent_positions:
            return f"portfolio full ({len(self._positions)}/{self.max_concurrent_positions})"
        cap = self._equity * self.max_total_notional_pct / 100.0
        if self.total_notional_usd + size_usd > cap:
            return (f"notional cap: {self.total_notional_usd + size_usd:,.0f} > "
                    f"{cap:,.0f} ({self.max_total_notional_pct:.0f}% of equity)")
        return None

    def _open(self, side: Side, intent: TradeIntent, mark: float) -> None:
        reject = self._capacity_check(intent.size_usd)
        if reject is not None:
            logger.info("SKIP  {:<5} ${:>8.2f}  {}", side.value.upper(),
                        intent.size_usd, reject)
            self._log({
                "event": "skip", "ts": intent.ts, "side": side.value,
                "size_usd": intent.size_usd, "reason": reject,
            })
            return
        fill = self._fill_price(mark, side, intent.size_usd)
        fee = self._fee(intent.size_usd)
        self._equity -= fee
        self._refresh_risk_state(intent.ts)
        pid = f"t{self._next_id:04d}"
        self._next_id += 1
        pos = Position(
            id=pid,
            side=side,
            size_usd=intent.size_usd,
            entry_price=fill,
            entry_ts=intent.ts,
            tp_price=0.0,
            sl_price=0.0,
            horizon_seconds=intent.horizon_seconds,
            expected_move_bps=intent.expected_move_bps,
            confidence=intent.confidence,
            peak_favorable_s=intent.peak_favorable_s,
        )
        self._positions.append(pos)
        logger.info(
            "OPEN  {} {:<5} ${:>8.2f} @ {:.2f}  emove={:+.1f}bps conf={:.2f}"
            "  fee=${:.2f}  open={} eq=${:.2f}",
            pid, side.value.upper(), intent.size_usd, fill,
            intent.expected_move_bps, intent.confidence, fee,
            len(self._positions), self._equity,
        )
        self._log({
            "event": "open", "ts": intent.ts, "position_id": pid, "side": side.value,
            "size_usd": intent.size_usd, "fill_price": fill, "mark_price": mark,
            "fee_usd": fee, "equity_usd": self._equity, "reason": intent.reason,
            "expected_move_bps": intent.expected_move_bps, "confidence": intent.confidence,
            "open_positions": len(self._positions),
        })

    def _close_position(self, p: Position, mark: float, ts: datetime, reason: str) -> None:
        # Scrub per-position layer state before the close goes through so
        # these dicts never outlive the position they describe.
        self._flip_streak.pop(p.id, None)
        self._peak_bps.pop(p.id, None)
        exit_side = Side.SELL if p.side is Side.BUY else Side.BUY
        fill = self._fill_price(mark, exit_side, p.size_usd)
        if p.side is Side.BUY:
            pnl_usd = p.size_usd * (fill - p.entry_price) / p.entry_price
        else:
            pnl_usd = p.size_usd * (p.entry_price - fill) / p.entry_price
        fee = self._fee(p.size_usd)
        self._equity += pnl_usd - fee
        self._refresh_risk_state(ts)
        self.trades_completed += 1
        net = pnl_usd - fee
        held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0
        if net > 0:
            self.wins += 1
        logger.info(
            "CLOSE {} {:<5} ${:>8.2f} @ {:.2f}  pnl=${:+.2f}  fee=${:.2f}  "
            "net=${:+.2f}  held={:.0f}s  eq=${:.2f}  ({})",
            p.id, p.side.value.upper(), p.size_usd, fill, pnl_usd, fee,
            net, held_s, self._equity, reason,
        )
        self._recent_trades.append({
            "id": p.id,
            "side": p.side.value,
            "size_usd": p.size_usd,
            "entry_price": p.entry_price,
            "exit_price": fill,
            "held_s": round(held_s),
            "reason": reason,
            "pnl_usd": round(pnl_usd, 4),
            "fee_usd": round(fee, 4),
            "net_usd": round(net, 4),
            "fee_killed": bool(pnl_usd > 0 and net <= 0),
            "expected_move_bps": p.expected_move_bps,
            "confidence": round(p.confidence, 3),
        })
        if len(self._recent_trades) > 60:
            self._recent_trades.pop(0)
        self._log({
            "event": "close", "ts": ts, "position_id": p.id, "side": p.side.value,
            "size_usd": p.size_usd, "entry_price": p.entry_price, "fill_price": fill,
            "mark_price": mark, "pnl_usd": pnl_usd, "fee_usd": fee,
            "net_usd": net, "held_seconds": held_s,
            "equity_usd": self._equity, "reason": reason,
            "trades_completed": self.trades_completed, "wins": self.wins,
            "open_positions": len(self._positions) - 1,
        })

    def _log(self, rec: dict[str, Any]) -> None:
        self.ledger.write(rec)
