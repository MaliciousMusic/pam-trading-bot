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
        """Close positions whose max_hold backstop fired. Called once per tick."""

    def evaluate_exits(self, ts: datetime, price: float, forecast) -> None:
        """Close positions that no longer align with the council's live forecast.
        Called each tick BEFORE mark_to_market. Default is a no-op."""

    @abstractmethod
    def execute(self, intent: TradeIntent, state: MarketState) -> None: ...
