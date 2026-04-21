"""Per-tick JSONL writer for sprint-mode decision logs.

One row per tick — everything the summary report and the multi-variant
comparison need to reconstruct the council's behavior without re-running.

`tribe_disagreement` is pre-computed (std-dev of signed convictions across
non-abstaining tribes) because the 30-day summary's top-disagreement-bars
query is the only expensive operation over this file — amortizing it
saves re-scanning every vote array for every sort.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core import serde
from ..core.types import Forecast, Vote


def _disagreement(votes: list[Vote]) -> float:
    """Population std-dev of signed convictions across non-abstaining tribes.
    0.0 when everyone agrees (or abstains); grows as tribes vote opposite sides
    with similar conviction."""
    signed: list[float] = []
    for v in votes:
        if v.side.value == "buy":
            signed.append(v.conviction)
        elif v.side.value == "sell":
            signed.append(-v.conviction)
    if len(signed) < 2:
        return 0.0
    mean = sum(signed) / len(signed)
    var = sum((s - mean) ** 2 for s in signed) / len(signed)
    return math.sqrt(var)


class DecisionLogWriter:
    """Owns the decisions.jsonl file handle for a single sprint run.
    Uses serde.dumps (msgspec-backed) line-by-line; lightweight enough that
    per-tick writes add negligible overhead vs the engine work.

    Open via `with`; flushes on exit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("wb")
        self._count = 0

    def __enter__(self) -> "DecisionLogWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()

    @property
    def count(self) -> int:
        return self._count

    def write_tick(
        self,
        *,
        tick: int,
        ts: datetime,
        price: float,
        regime_label: str | None,
        votes: list[Vote],
        net_pressure: float,
        uncertainty: float,
        forecast: Forecast,
        action: str,
        action_reason: str,
        equity_usd: float,
        positions_open: int,
    ) -> None:
        row = {
            "tick": tick,
            "ts": ts.astimezone(timezone.utc).isoformat(),
            "price": price,
            "regime": regime_label,
            "votes": [
                {
                    "tribe_id": v.tribe_id,
                    "side": v.side.value,
                    "conviction": v.conviction,
                    "reason": v.reason,
                }
                for v in votes
            ],
            "net_pressure": net_pressure,
            "uncertainty": uncertainty,
            "expected_move_bps": forecast.expected_move_bps,
            "confidence": forecast.confidence,
            "action": action,
            "action_reason": action_reason,
            "equity_usd": equity_usd,
            "positions_open": positions_open,
            "tribe_disagreement": _disagreement(votes),
        }
        self._f.write(serde.dumps(row))
        self._f.write(b"\n")
        self._count += 1
