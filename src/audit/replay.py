"""Event-tape replayer + batch ReplayEngine.

Reads a day's recorded tape from `data/events/YYYY-MM-DD.jsonl` and
drives a freshly-constructed engine stack through the same session the
live bot saw — no sleeping, no network, no UI. Used by the tournament
to score variant councils on identical market conditions.

Design:
  - `load_events(path)` streams Events chronologically from disk.
  - `ReplayEngine.run(events)` consumes the stream, feeds MarketState,
    triggers `_tick()` at each tick-boundary, and closes all positions
    at tape end. Returns a per-variant result dict ready for the
    leaderboard.

Why a separate engine instead of re-using TradingEngine: the live
engine is async and socket-bound. Replays are synchronous and offline.
Sharing code would force either ugly branching or awkward async
scaffolding for no gain. The ~80 lines of overlap are trivial to keep
aligned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..core import serde
from ..aggregator.voting import aggregate_votes
from ..core.config import PROJECT_ROOT
from ..core.types import Event, Side
from ..decision.engine import DecisionEngine
from ..execution.paper import PaperExecutor
from ..projector.chess import ChessProjector
from ..regime.classifier import RegimeClassifier
from ..tribes.market_state import MarketState
from ..tribes.tribe import Tribe


EVENTS_DIR = PROJECT_ROOT / "data" / "events"


def load_events(path: Path) -> Iterator[Event]:
    """Yield Event objects in chronological order from a daily tape."""
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = serde.loads(line)
            except Exception:
                continue
            ts_str = rec.get("ts")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            yield Event(
                ts=ts,
                source=str(rec.get("source", "")),
                kind=str(rec.get("kind", "")),
                symbol=rec.get("symbol"),
                payload=rec.get("payload") or {},
                tags=set(rec.get("tags") or ()),
                severity=float(rec.get("severity") or 0.0),
            )


class ReplayEngine:
    """Synchronous, offline engine for tournament scoring.

    Constructs nothing — pass in pre-built tribes/projector/decision/
    executor. That keeps the factory logic in the tournament runner
    (which needs to build fresh stacks per variant anyway).
    """

    def __init__(
        self,
        tribes: list[Tribe],
        state: MarketState,
        projector: ChessProjector,
        decision: DecisionEngine,
        executor: PaperExecutor,
        tick_seconds: int,
        regime_classifier: RegimeClassifier | None = None,
    ) -> None:
        self.tribes = tribes
        self.state = state
        self.projector = projector
        self.decision = decision
        self.executor = executor
        self.tick_seconds = tick_seconds
        self.regime_classifier = regime_classifier
        self.start_equity = executor.equity_usd
        self.ticks = 0

    def run(self, events: Iterable[Event]) -> dict:
        """Drive the engine through every event in the tape. Returns a
        summary dict (equity, trades, etc.) usable directly as a
        leaderboard row."""
        last_tick_ts: datetime | None = None
        last_ts: datetime | None = None
        for ev in events:
            last_ts = ev.ts
            self.state.ingest(ev)
            if last_tick_ts is None:
                last_tick_ts = ev.ts
                continue
            elapsed = (ev.ts - last_tick_ts).total_seconds()
            # Catch up: a single event may cross multiple tick boundaries
            # if the tape has gaps (market pause, network hiccup). Fire a
            # tick for each boundary so the simulated clock can't drift.
            while elapsed >= self.tick_seconds:
                last_tick_ts = last_tick_ts.replace() + _seconds(self.tick_seconds)
                self._tick(last_tick_ts)
                elapsed = (ev.ts - last_tick_ts).total_seconds()
        # Force-close any remaining positions at the final mark so equity
        # reflects a realized book, not unrealized PnL.
        if last_ts is not None and self.state.last_price:
            self._force_close_all(last_ts, self.state.last_price)
        end_equity = self.executor.equity_usd
        return {
            "ticks": self.ticks,
            "start_equity_usd": self.start_equity,
            "end_equity_usd": end_equity,
            "equity_delta_usd": end_equity - self.start_equity,
            "equity_delta_pct": (
                0.0 if self.start_equity == 0
                else (end_equity - self.start_equity) / self.start_equity
            ),
            "trades": self.executor.trades_completed,
            "wins": self.executor.wins,
            "win_rate": (
                0.0 if self.executor.trades_completed == 0
                else self.executor.wins / self.executor.trades_completed
            ),
        }

    def _tick(self, ts: datetime) -> None:
        self.ticks += 1
        if self.state.last_price is None:
            return
        votes = [t.vote(ts, self.state) for t in self.tribes]
        aggregate_votes(votes, ts)  # side-effect-free; kept for parity
        regime_label: str | None = None
        if self.regime_classifier is not None:
            regime_state = self.regime_classifier.classify(self.state)
            self.projector.regime_multiplier = regime_state.multiplier
            regime_label = regime_state.label.value
        forecast = self.projector.project(self.state)
        if hasattr(self.executor, "apply_funding"):
            self.executor.apply_funding(
                self.state.last_funding_rate or 0.0,
                float(self.tick_seconds),
            )
        self.executor.evaluate_exits(
            ts, self.state.last_price, forecast, regime_label=regime_label,
        )
        self.executor.mark_to_market(ts, self.state.last_price)
        intent = self.decision.decide(
            ts, forecast, self.state, equity_usd=self.executor.equity_usd,
        )
        if intent.action != "hold":
            self.executor.execute(intent, self.state)

    def _force_close_all(self, ts: datetime, price: float) -> None:
        for p in list(self.executor.positions):
            self.executor._close_position(p, price, ts, "replay end")
        self.executor._positions = []


def _seconds(s: int):
    from datetime import timedelta
    return timedelta(seconds=s)
