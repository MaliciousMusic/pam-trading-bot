"""Executor interface. Paper and live executors both honor this contract."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.types import Position, TradeIntent
from ..tribes.market_state import MarketState


class Executor(ABC):
    @property
    @abstractmethod
    def equity_usd(self) -> float: ...

    @property
    @abstractmethod
    def position(self) -> Position: ...

    @abstractmethod
    def execute(self, intent: TradeIntent, state: MarketState) -> None: ...
