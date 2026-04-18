"""TradingEngine — orchestrates a single tick:

    collect events → MarketState absorbs → every `tick_seconds`:
      1) each Tribe votes → per-tick Aggregate (logged)
      2) ChessProjector runs N-step forward sim → Forecast
      3) DecisionEngine outputs a TradeIntent
      4) Executor applies the intent (paper or live)
      5) Ledger records the tick (votes + price + forecast summary) and the fill

This is the single point of truth for the tick loop. Scripts shouldn't
reimplement it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from datetime import timezone

from ..aggregator.voting import aggregate_votes
from ..core.bus import EventBus
from ..core.types import Event, Side
from ..decision.engine import DecisionEngine
from ..execution.base import Executor
from ..execution.ledger import Ledger
from ..projector.chess import ChessProjector
from ..tribes.market_state import MarketState
from ..tribes.tribe import Tribe

try:
    from ..ui.broadcaster import UIBroadcaster  # type: ignore
except Exception:  # pragma: no cover — UI is optional
    UIBroadcaster = None  # type: ignore


class TradingEngine:
    def __init__(
        self,
        bus: EventBus,
        tribes: list[Tribe],
        state: MarketState,
        projector: ChessProjector,
        decision: DecisionEngine,
        executor: Executor,
        ledger: Ledger,
        tick_seconds: int,
        broadcaster: "UIBroadcaster | None" = None,
    ) -> None:
        self.bus = bus
        self.tribes = tribes
        self.state = state
        self.projector = projector
        self.decision = decision
        self.executor = executor
        self.ledger = ledger
        self.tick_seconds = tick_seconds
        self.broadcaster = broadcaster
        self._tick_count = 0

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._market_state_feeder()),
            asyncio.create_task(self._tick_loop()),
        ]
        if self.broadcaster is not None:
            tasks.append(asyncio.create_task(self._event_ui_feeder()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def _market_state_feeder(self) -> None:
        q = self.bus.subscribe()
        while True:
            ev: Event = await q.get()
            self.state.ingest(ev)

    async def _event_ui_feeder(self) -> None:
        """Relay high-signal events to the UI feed (liquidations always,
        trades/funding only when severity is high)."""
        q = self.bus.subscribe()
        while True:
            ev: Event = await q.get()
            if ev.kind == "trade" and ev.severity < 0.55:
                continue
            if ev.kind == "funding" and ev.severity < 0.25:
                continue
            await self.broadcaster.publish({
                "type": "event",
                "ts": ev.ts.astimezone(timezone.utc).isoformat(),
                "kind": ev.kind,
                "source": ev.source,
                "payload": ev.payload,
                "tags": list(ev.tags),
                "severity": ev.severity,
            })

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            self._tick_count += 1
            await self._on_tick()

    async def _on_tick(self) -> None:
        ts = Event.now_utc()
        if self.state.last_price is None:
            logger.info("[tick {:>4}] waiting for market data...", self._tick_count)
            return

        votes = [t.vote(ts, self.state) for t in self.tribes]
        agg = aggregate_votes(votes, ts)
        forecast = self.projector.project(self.state)
        intent = self.decision.decide(ts, forecast, self.executor.position, self.state,
                                      equity_usd=self.executor.equity_usd)
        pos = self.executor.position
        logger.info(
            "[tick {:>4}] px={:.2f} net={:+.3f} unc={:.2f} emove={:+.1f}bps conf={:.2f} "
            "pos={} eq=${:.2f} | {} — {}",
            self._tick_count, self.state.last_price, agg.net_pressure, agg.uncertainty,
            forecast.expected_move_bps, forecast.confidence,
            "FLAT" if pos.is_flat else pos.side.value.upper(),
            self.executor.equity_usd,
            intent.action, intent.reason,
        )

        # Tick record for daily audit
        self.ledger.write({
            "event": "tick", "ts": ts, "price": self.state.last_price,
            "funding_rate": self.state.last_funding_rate,
            "net_pressure": agg.net_pressure, "uncertainty": agg.uncertainty,
            "expected_move_bps": forecast.expected_move_bps, "confidence": forecast.confidence,
            "votes": [
                {"tribe_id": v.tribe_id, "side": v.side.value,
                 "conviction": v.conviction, "weight": v.weight, "reason": v.reason}
                for v in votes
            ],
        })

        if intent.action != "hold":
            self.executor.execute(intent, self.state)

        if self.broadcaster is not None:
            pos = self.executor.position
            await self.broadcaster.publish({
                "type": "tick",
                "tick": self._tick_count,
                "ts": ts.astimezone(timezone.utc).isoformat(),
                "price": self.state.last_price,
                "funding_rate": self.state.last_funding_rate,
                "net_pressure": agg.net_pressure,
                "uncertainty": agg.uncertainty,
                "expected_move_bps": forecast.expected_move_bps,
                "confidence": forecast.confidence,
                "forecast_steps": [
                    {"net_pressure": s.net_pressure, "uncertainty": s.uncertainty}
                    for s in forecast.steps
                ],
                "votes": [
                    {"tribe_id": v.tribe_id, "side": v.side.value,
                     "conviction": v.conviction, "weight": v.weight, "reason": v.reason}
                    for v in votes
                ],
                "position": None if pos.is_flat else {
                    "side": pos.side.value,
                    "size_usd": pos.size_usd,
                    "entry_price": pos.entry_price,
                },
                "equity_usd": self.executor.equity_usd,
                "trades": getattr(self.executor, "trades_completed", 0),
                "wins": getattr(self.executor, "wins", 0),
                "action": intent.action,
                "reason": intent.reason,
            })
