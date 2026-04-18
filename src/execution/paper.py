"""Paper executor — simulates fills with configurable fees and slippage.

We charge both fee and slippage on entry AND exit. Slippage direction is
realistic (buy fills above mark, sell fills below). This is NOT a
backtest-grade matching engine — just enough realism to keep us honest
while the brain is being tuned.
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
        ledger: Ledger | None = None,
    ) -> None:
        self._equity = starting_equity_usd
        self._start_equity = starting_equity_usd
        self._position = Position(side=None)
        self.fee_rate_bps = fee_rate_bps
        self.slippage_bps = slippage_bps
        self.ledger = ledger or Ledger()
        self.trades_completed = 0
        self.wins = 0

    # -- Executor interface ---------------------------------------------

    @property
    def equity_usd(self) -> float:
        return self._equity

    @property
    def position(self) -> Position:
        return self._position

    def execute(self, intent: TradeIntent, state: MarketState) -> None:
        price = state.last_price
        if price is None or price <= 0:
            return
        if intent.action == "open_long":
            self._open(Side.BUY, intent, price)
        elif intent.action == "open_short":
            self._open(Side.SELL, intent, price)
        elif intent.action == "close":
            self._close(intent, price)
        # "hold" → nothing

    # -- internals -------------------------------------------------------

    def _fill_price(self, mark: float, taker_side: Side) -> float:
        sign = 1 if taker_side is Side.BUY else -1
        return mark * (1 + sign * self.slippage_bps / 10_000.0)

    def _fee(self, notional_usd: float) -> float:
        return notional_usd * self.fee_rate_bps / 10_000.0

    def _open(self, side: Side, intent: TradeIntent, mark: float) -> None:
        if not self._position.is_flat:
            return
        fill = self._fill_price(mark, side)
        fee = self._fee(intent.size_usd)
        self._equity -= fee
        self._position = Position(
            side=side,
            size_usd=intent.size_usd,
            entry_price=fill,
            entry_ts=intent.ts,
            horizon_seconds=0,  # decision engine already set horizon; record elsewhere if needed
        )
        logger.info(
            "OPEN  {:<5} ${:>8.2f} @ {:.2f}  fee=${:.2f}  equity=${:.2f}  reason={}",
            side.value.upper(), intent.size_usd, fill, fee, self._equity, intent.reason,
        )
        self._log({
            "event": "open", "ts": intent.ts, "side": side.value,
            "size_usd": intent.size_usd, "fill_price": fill, "mark_price": mark,
            "fee_usd": fee, "equity_usd": self._equity, "reason": intent.reason,
            "expected_move_bps": intent.expected_move_bps, "confidence": intent.confidence,
        })

    def _close(self, intent: TradeIntent, mark: float) -> None:
        p = self._position
        if p.is_flat or p.side is None:
            return
        exit_side = Side.SELL if p.side is Side.BUY else Side.BUY
        fill = self._fill_price(mark, exit_side)
        # Linear USD PnL since size is dollar-notional.
        if p.side is Side.BUY:
            pnl_usd = p.size_usd * (fill - p.entry_price) / p.entry_price
        else:
            pnl_usd = p.size_usd * (p.entry_price - fill) / p.entry_price
        fee = self._fee(p.size_usd)
        self._equity += pnl_usd - fee
        self.trades_completed += 1
        if pnl_usd - fee > 0:
            self.wins += 1
        logger.info(
            "CLOSE {:<5} ${:>8.2f} @ {:.2f}  pnl=${:+.2f}  fee=${:.2f}  equity=${:.2f}  reason={}",
            p.side.value.upper(), p.size_usd, fill, pnl_usd, fee, self._equity, intent.reason,
        )
        self._log({
            "event": "close", "ts": intent.ts, "side": p.side.value,
            "size_usd": p.size_usd, "entry_price": p.entry_price, "fill_price": fill,
            "mark_price": mark, "pnl_usd": pnl_usd, "fee_usd": fee,
            "equity_usd": self._equity, "reason": intent.reason,
            "trades_completed": self.trades_completed, "wins": self.wins,
        })
        self._position = Position(side=None)

    def _log(self, rec: dict[str, Any]) -> None:
        self.ledger.write(rec)
