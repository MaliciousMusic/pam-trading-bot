"""Paper executor — simulates fills with fees + slippage across a portfolio
of simultaneously-open positions.

Exit model: every tick the council's live forecast is evaluated against every
open position via `evaluate_exits()`. The base rule is a sign-flip beyond the
configured dead-zone; three optional layers stack on top:

  1. **Conviction-magnitude** — weak flips (conf < strong_flip_conf) must
     persist for `weak_flip_confirm_ticks` before closing; strong flips close
     immediately. Streaks reset when the council realigns.
  2. **Profit ratchet** — once a position's peak unrealized reaches
     `ratchet_arm_bps`, it closes on a retrace of `ratchet_retrace_frac` of
     that peak. Pure trailing protection — no static TP.
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
    ) -> None:
        self._equity = starting_equity_usd
        self._start_equity = starting_equity_usd
        self._positions: list[Position] = []
        self.fee_rate_bps = fee_rate_bps
        self.slippage_bps = slippage_bps
        self.max_concurrent_positions = max_concurrent_positions
        self.max_total_notional_pct = max_total_notional_pct
        self.max_hold_seconds = max_hold_seconds
        self.exit_dead_zone_bps = exit_dead_zone_bps
        self.ledger = ledger or Ledger()
        self.trades_completed = 0
        self.wins = 0
        self._next_id = 1
        self._recent_trades: list[dict] = []   # last 60 closed, newest last

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

    def _fill_price(self, mark: float, taker_side: Side) -> float:
        sign = 1 if taker_side is Side.BUY else -1
        return mark * (1 + sign * self.slippage_bps / 10_000.0)

    def _fee(self, notional_usd: float) -> float:
        return notional_usd * self.fee_rate_bps / 10_000.0

    def _capacity_check(self, size_usd: float) -> str | None:
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
        fill = self._fill_price(mark, side)
        fee = self._fee(intent.size_usd)
        self._equity -= fee
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
        fill = self._fill_price(mark, exit_side)
        if p.side is Side.BUY:
            pnl_usd = p.size_usd * (fill - p.entry_price) / p.entry_price
        else:
            pnl_usd = p.size_usd * (p.entry_price - fill) / p.entry_price
        fee = self._fee(p.size_usd)
        self._equity += pnl_usd - fee
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
