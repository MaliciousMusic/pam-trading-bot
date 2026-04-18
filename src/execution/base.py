"""Executor interface. Paper and live executors both honor this contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..core.types import Position, TradeIntent
from ..tribes.market_state import MarketState


class Executor(ABC):
    @property
    @abstractmethod
    def equity_usd(self) -> float: ...

    @property
    @abstractmethod
    def positions(self) -> list[Position]:
        """All currently open positions (portfolio)."""

    @property
    def total_notional_usd(self) -> float:
        return sum(p.size_usd for p in self.positions)

    @abstractmethod
    def mark_to_market(self, ts: datetime, price: float) -> None:
        """Auto-close any position whose TP, SL, or horizon backstop triggered
        at or before the given mark. Called once per tick before decision."""

    @abstractmethod
    def execute(self, intent: TradeIntent, state: MarketState) -> None: ...
