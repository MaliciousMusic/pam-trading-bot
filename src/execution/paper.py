"""Paper executor — simulates fills with fees + slippage across a portfolio
of simultaneously-open positions.

Exit model: every tick the council's live forecast is evaluated against every
open position via `evaluate_exits()`. A position is closed when the council's
expected_move_bps flips against it beyond a small dead-zone (default 0.5 bps),
preventing whipsaw from near-zero oscillations. `mark_to_market()` remains as
a hard backstop that closes positions exceeding `max_hold_seconds`.

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

    # -- Executor interface ---------------------------------------------

    @property
    def equity_usd(self) -> float:
        return self._equity

    @property
    def positions(self) -> list[Position]:
        return self._positions

    def evaluate_exits(self, ts: datetime, price: float, forecast) -> None:
        """Close positions the council no longer supports.

        Exit rule (applied per position each tick):
          LONG  → close if emove ≤ -dead_zone (council turned negative)
          SHORT → close if emove ≥ +dead_zone (council turned positive)

        The dead zone prevents churning when the forecast oscillates around
        zero. Positions that stay aligned keep running indefinitely — the
        council is the only exit signal besides the max_hold backstop.
        """
        if price is None or price <= 0 or not self._positions:
            return
        emove = forecast.expected_move_bps
        dz = self.exit_dead_zone_bps
        survivors: list[Position] = []
        for p in self._positions:
            if p.side is Side.BUY and emove <= -dz:
                self._close_position(p, price, ts,
                                     f"council flip short emove={emove:+.1f}bps")
            elif p.side is Side.SELL and emove >= dz:
                self._close_position(p, price, ts,
                                     f"council flip long emove={emove:+.1f}bps")
            else:
                survivors.append(p)
        self._positions = survivors

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
