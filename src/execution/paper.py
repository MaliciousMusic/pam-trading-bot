"""Paper executor — simulates fills with fees + slippage across a portfolio
of simultaneously-open positions.

Each `execute()` call opens a NEW position (long or short) with its own
take-profit and stop-loss brackets. `mark_to_market()` runs every tick
before the decision engine votes, and auto-closes every open position
whose TP, SL, or max-hold backstop has triggered against the current
mark. Stacked positions in either direction are supported; this is the
paper-model equivalent of Binance perps Hedge Mode.

Fees and slippage are charged on BOTH entry and exit of every individual
position. The decision layer embeds the round-trip cost into the entry
gate, so a trade that gets opened already has an edge net of fees if the
forecast is right. If the forecast is wrong the SL bounds the loss.
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

    def mark_to_market(self, ts: datetime, price: float) -> None:
        """Scan every open position; close any that hit TP, SL, or max-hold."""
        if price is None or price <= 0 or not self._positions:
            return
        survivors: list[Position] = []
        for p in self._positions:
            reason = self._trigger(p, price, ts)
            if reason is None:
                survivors.append(p)
            else:
                self._close_position(p, price, ts, reason)
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
            # Legacy manual unwind — closes all positions at mark.
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

    def _trigger(self, p: Position, mark: float, ts: datetime) -> str | None:
        """Return a close-reason string if the position should close, else None."""
        held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0
        if self.max_hold_seconds and held_s >= self.max_hold_seconds:
            return f"max hold {int(held_s)}s"
        if p.side is Side.BUY:
            if mark >= p.tp_price:
                return f"TP hit @ {p.tp_price:.2f}"
            if mark <= p.sl_price:
                return f"SL hit @ {p.sl_price:.2f}"
        else:  # SELL
            if mark <= p.tp_price:
                return f"TP hit @ {p.tp_price:.2f}"
            if mark >= p.sl_price:
                return f"SL hit @ {p.sl_price:.2f}"
        return None

    def _capacity_check(self, size_usd: float) -> str | None:
        """Return a rejection reason, or None if the new trade fits."""
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
            tp_price=intent.tp_price,
            sl_price=intent.sl_price,
            horizon_seconds=intent.horizon_seconds,
            expected_move_bps=intent.expected_move_bps,
            confidence=intent.confidence,
        )
        self._positions.append(pos)
        logger.info(
            "OPEN  {} {:<5} ${:>8.2f} @ {:.2f}  TP={:.2f} SL={:.2f}  fee=${:.2f}  "
            "open={} eq=${:.2f}",
            pid, side.value.upper(), intent.size_usd, fill,
            intent.tp_price, intent.sl_price, fee,
            len(self._positions), self._equity,
        )
        self._log({
            "event": "open", "ts": intent.ts, "position_id": pid, "side": side.value,
            "size_usd": intent.size_usd, "fill_price": fill, "mark_price": mark,
            "tp_price": intent.tp_price, "sl_price": intent.sl_price,
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
        if net > 0:
            self.wins += 1
        logger.info(
            "CLOSE {} {:<5} ${:>8.2f} @ {:.2f}  pnl=${:+.2f}  fee=${:.2f}  "
            "net=${:+.2f} eq=${:.2f}  ({})",
            p.id, p.side.value.upper(), p.size_usd, fill, pnl_usd, fee,
            net, self._equity, reason,
        )
        self._log({
            "event": "close", "ts": ts, "position_id": p.id, "side": p.side.value,
            "size_usd": p.size_usd, "entry_price": p.entry_price, "fill_price": fill,
            "mark_price": mark, "pnl_usd": pnl_usd, "fee_usd": fee,
            "net_usd": net, "equity_usd": self._equity, "reason": reason,
            "trades_completed": self.trades_completed, "wins": self.wins,
            "open_positions": len(self._positions) - 1,
        })

    def _log(self, rec: dict[str, Any]) -> None:
        self.ledger.write(rec)
