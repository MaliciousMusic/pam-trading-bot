"""Core data types shared across modules.

Every module that speaks on the bus or returns results uses these shapes.
Add fields only when a tribe or downstream module genuinely needs them —
the types are a contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    ABSTAIN = "abstain"


@dataclass(slots=True)
class Event:
    """A normalized market-relevant happening placed on the bus.

    `tags` is the attention-filter surface: tribes subscribe by tag.
    `severity` is a coarse 0..1 estimate of how big the stimulus is, used
    for sampling and for tribes whose attention threshold is high (pros
    see everything, noobs only react above some severity).
    """
    ts: datetime
    source: str          # e.g. "binance.aggTrade"
    kind: str            # e.g. "trade", "liquidation", "funding", "news"
    symbol: str | None
    payload: dict[str, Any]
    tags: set[str] = field(default_factory=set)
    severity: float = 0.0

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(timezone.utc)


@dataclass(slots=True)
class Vote:
    """A single tribe's decision for a tick."""
    tribe_id: str
    side: Side
    conviction: float    # 0..1
    horizon_seconds: int # how long the tribe expects to hold
    weight: float        # the tribe's current aggregate weight (flow share)
    reason: str = ""     # short string for audit


@dataclass(slots=True)
class Aggregate:
    """Weighted synthesis of all tribe votes for a single tick."""
    ts: datetime
    net_pressure: float  # -1..+1, signed by side, weighted by conviction*weight
    buy_mass: float
    sell_mass: float
    abstain_mass: float
    vote_count: int
    uncertainty: float   # disagreement measure


@dataclass(slots=True)
class Forecast:
    """Output of the chess projector for a single decision tick."""
    ts: datetime
    steps: list[Aggregate]       # per-step forward aggregates
    expected_move_bps: float     # cumulative expected move, basis points
    confidence: float            # decays with depth
    horizon_seconds: int


@dataclass(slots=True)
class Position:
    """Open position tracked by the executor. `side=None` means flat."""
    side: Side | None
    size_usd: float = 0.0
    entry_price: float = 0.0
    entry_ts: datetime | None = None
    horizon_seconds: int = 0

    @property
    def is_flat(self) -> bool:
        return self.side is None


@dataclass(slots=True)
class TradeIntent:
    """Decision-layer output handed to the executor."""
    ts: datetime
    action: str                  # "open_long" | "open_short" | "close" | "hold"
    size_usd: float
    price_ref: float
    reason: str
    expected_move_bps: float
    confidence: float
