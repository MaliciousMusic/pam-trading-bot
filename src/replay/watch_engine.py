"""Watch-mode replay engine — paced virtual clock + WebSocket broadcast.

WatchEngine mirrors SprintEngine's tick logic exactly but:
  - publishes WebSocket tick messages (same shape as the live engine) instead
    of writing JSONL — UI components receive the same data structure and
    render without knowing they're watching a replay
  - sleeps `tick_seconds / speed` wall seconds between ticks for pacing
  - sends `replay_state` messages (banner data only — tick handlers ignore them)

WatchController manages the lifecycle of a single active watch run:
start / set_speed / toggle_pause / stop.  One controller per server instance.

Multi-variant mode (Phase 7): WatchController.start() accepts an optional
`variants` list.  Each variant runs as a concurrent asyncio task sharing the
same UIBroadcaster; tick messages are tagged with `variant_id` so the UI can
route them to mini-panes.  A conductor coroutine sends one aggregate
replay_state per second so the amber banner shows overall progress.

Architectural constraint (from codebase design): the UI's `onTick` handler
must not change.  When variant_id is absent the message routes to onTick
exactly as before.  The only replay-aware component is the amber banner, which
subscribes exclusively to `replay_state` messages.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from loguru import logger

from ..aggregator.voting import aggregate_votes
from ..core.types import Event, Side
from ..decision.engine import DecisionEngine
from ..execution.ledger import Ledger
from ..execution.paper import PaperExecutor
from ..projector.chess import ChessProjector
from ..regime.classifier import RegimeClassifier
from ..tribes.market_state import MarketState
from ..tribes.tribe import Tribe
from ..ui.broadcaster import UIBroadcaster
from .feed import build_event_stream

_REPLAY_STATE_EVERY = 50   # broadcast replay_state every N ticks
_STARTING_EQUITY = 5_000.0


class WatchEngine:
    """Async tick driver that broadcasts to a UIBroadcaster at a controlled speed.

    The tick message shape is identical to TradingEngine._on_tick so all
    existing UI handlers work without modification.  The ONLY new message
    type is `replay_state`, consumed exclusively by the amber banner."""

    def __init__(
        self,
        tribes: list[Tribe],
        state: MarketState,
        projector: ChessProjector,
        decision: DecisionEngine,
        executor: PaperExecutor,
        regime_classifier: RegimeClassifier,
        tick_seconds: int,
        broadcaster: UIBroadcaster,
        run_id: str,
        day: str,
        speed: float = 10.0,
        total_ticks_est: int = 28_800,
        variant_id: str | None = None,
        emit_replay_state: bool = True,
    ) -> None:
        self.tribes = tribes
        self.state = state
        self.projector = projector
        self.decision = decision
        self.executor = executor
        self.regime_classifier = regime_classifier
        self.tick_seconds = int(tick_seconds)
        self.broadcaster = broadcaster
        self.run_id = run_id
        self.day = day
        self.speed = speed
        self.total_ticks_est = total_ticks_est
        self.variant_id = variant_id
        self._emit_replay_state = emit_replay_state

        self._paused = False
        self._ticks = 0
        self._events_consumed = 0
        self._recent_trades: deque[dict] = deque(maxlen=20)
        self._start_equity = executor.equity_usd

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        return self._paused

    async def run(self, events: Iterable[Event]) -> dict:
        if self._emit_replay_state:
            await self.broadcaster.publish(self._replay_state_msg())

        last_tick_ts: datetime | None = None
        last_ts: datetime | None = None

        for ev in events:
            # Pause loop — yields to event loop every 100ms while paused
            while self._paused:
                await asyncio.sleep(0.1)

            self._events_consumed += 1
            last_ts = ev.ts
            self.state.ingest(ev)

            if last_tick_ts is None:
                last_tick_ts = ev.ts
                continue

            elapsed = (ev.ts - last_tick_ts).total_seconds()
            while elapsed >= self.tick_seconds:
                last_tick_ts = last_tick_ts + timedelta(seconds=self.tick_seconds)
                await self._tick(last_tick_ts)
                elapsed = (ev.ts - last_tick_ts).total_seconds()

        # Force-close open positions at end of day
        if last_ts is not None and self.state.last_price:
            for p in list(self.executor.positions):
                self.executor._close_position(p, self.state.last_price, last_ts, "replay end")
            self.executor._positions = []

        if self._emit_replay_state:
            await self.broadcaster.publish({**self._replay_state_msg(), "active": False})
        return {
            "ticks": self._ticks,
            "events_consumed": self._events_consumed,
            "end_equity_usd": self.executor.equity_usd,
        }

    async def _tick(self, ts: datetime) -> None:
        # Pace: sleep before doing work so a cancel during sleep is clean
        sleep_s = self.tick_seconds / max(self.speed, 0.1)
        await asyncio.sleep(sleep_s)

        self._ticks += 1
        if self.state.last_price is None:
            return

        raw_votes = [t.vote(ts, self.state) for t in self.tribes]
        regime = self.regime_classifier.classify(self.state)
        self.projector.regime_multiplier = regime.multiplier
        agg = aggregate_votes(raw_votes, ts)
        forecast = self.projector.project(self.state)

        if hasattr(self.executor, "apply_funding"):
            self.executor.apply_funding(
                self.state.last_funding_rate or 0.0,
                float(self.tick_seconds),
            )
        self.executor.evaluate_exits(
            ts, self.state.last_price, forecast, regime_label=regime.label,
        )
        self.executor.mark_to_market(ts, self.state.last_price)

        intent = self.decision.decide(
            ts, forecast, self.state, equity_usd=self.executor.equity_usd,
        )
        if intent.action != "hold":
            self.executor.execute(intent, self.state)

        positions = self.executor.positions
        recent_trades = list(getattr(self.executor, "recent_trades", []))

        await self.broadcaster.publish({
            "type": "tick",
            "variant_id": self.variant_id,
            "tick": self._ticks,
            "ts": ts.astimezone(timezone.utc).isoformat(),
            "price": self.state.last_price,
            "funding_rate": self.state.last_funding_rate,
            "net_pressure": agg.net_pressure,
            "uncertainty": agg.uncertainty,
            "expected_move_bps": forecast.expected_move_bps,
            "confidence": forecast.confidence,
            "predictions": [],
            "forecast_steps": [
                {"net_pressure": s.net_pressure, "uncertainty": s.uncertainty}
                for s in forecast.steps
            ],
            "forecast_steps_bps": forecast.steps_bps,
            "forecast_sigma_bps": forecast.steps_sigma_bps,
            "forecast_sample_paths_bps": forecast.sample_paths_bps,
            "votes": [
                {"tribe_id": v.tribe_id, "side": v.side.value,
                 "conviction": v.conviction, "weight": v.weight, "reason": v.reason}
                for v in raw_votes
            ],
            "positions": [
                {
                    "id": p.id,
                    "side": p.side.value,
                    "size_usd": p.size_usd,
                    "entry_price": p.entry_price,
                    "entry_ts": p.entry_ts.astimezone(timezone.utc).isoformat(),
                    "tp_price": p.tp_price,
                    "sl_price": p.sl_price,
                    "expected_move_bps": p.expected_move_bps,
                    "confidence": p.confidence,
                }
                for p in positions
            ],
            "total_notional_usd": self.executor.total_notional_usd,
            "equity_usd": self.executor.equity_usd,
            "starting_equity_usd": self._start_equity,
            "trades": getattr(self.executor, "trades_completed", 0),
            "wins": getattr(self.executor, "wins", 0),
            "recent_trades": recent_trades,
            "action": intent.action,
            "reason": intent.reason,
            "backscore": None,
            "calibration": None,
            "kernel": None,
            "regime": {"label": regime.label.value, "multiplier": regime.multiplier},
        })

        if self._emit_replay_state and self._ticks % _REPLAY_STATE_EVERY == 0:
            await self.broadcaster.publish(self._replay_state_msg())

    def _replay_state_msg(self) -> dict:
        pct = (self._ticks / max(self.total_ticks_est, 1)) * 100
        return {
            "type": "replay_state",
            "active": True,
            "run_id": self.run_id,
            "day": self.day,
            "speed": self.speed,
            "tick": self._ticks,
            "total_ticks_est": self.total_ticks_est,
            "progress_pct": min(pct, 100.0),
            "paused": self._paused,
        }


async def _conduct(
    engines: list[WatchEngine],
    broadcaster: UIBroadcaster,
    run_id: str,
    day: str,
) -> None:
    """Aggregate replay_state broadcaster for multi-variant runs.

    Fires every second; reads _ticks from each engine and publishes one
    combined replay_state so the amber banner shows overall progress.
    The individual engines have emit_replay_state=False when this runs."""
    while True:
        await asyncio.sleep(1.0)
        if not engines:
            return
        ticks = [e._ticks for e in engines]
        total_est = [e.total_ticks_est for e in engines]
        avg_pct = sum(t / max(te, 1) * 100 for t, te in zip(ticks, total_est)) / len(engines)
        await broadcaster.publish({
            "type": "replay_state",
            "active": True,
            "run_id": run_id,
            "day": day,
            "speed": engines[0].speed,
            "tick": sum(ticks) // len(engines),
            "total_ticks_est": max(total_est),
            "progress_pct": min(avg_pct, 100.0),
            "paused": engines[0]._paused,
            "variant_count": len(engines),
        })


class WatchController:
    """Owns the single active watch session for the server.

    Called by POST /api/replay/* handlers.  Thread-safe within a single
    asyncio event loop (no locking needed — all callers are async tasks).

    Single-variant: behaves exactly as before Phase 7 — one WatchEngine,
    replay_state emitted by the engine itself.

    Multi-variant: N WatchEngine tasks run concurrently, each tagged with
    variant_id and emit_replay_state=False.  A _conduct() coroutine sends
    one aggregate replay_state per second."""

    def __init__(self, broadcaster: UIBroadcaster, cache_root: Path) -> None:
        self._broadcaster = broadcaster
        self._cache_root = Path(cache_root)
        self._task: asyncio.Task | None = None
        self._engines: list[WatchEngine] = []

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        day: date,
        speed: float = 10.0,
        synth_rate: int = 60,
        symbol: str = "BTCUSDT",
        seed: int = 42,
        bars: list[dict] | None = None,
        funding: list[dict] | None = None,
        variants: list[dict] | None = None,
    ) -> str:
        """Start a new watch run, cancelling any active session.

        `variants` is an optional list of dicts with keys synth_rate, seed,
        and optionally variant_id.  When >1 variant, multi-variant mode is
        used: N concurrent engines + conductor.  When absent or single-element,
        falls back to single-variant behaviour (variant_id=None on tick msgs).
        Returns the run_id."""
        if self.active:
            await self.stop()

        from ..core.config import PROJECT_ROOT, settings
        from .harness import _build_stack

        # Normalise variants list
        if not variants:
            variants = [{"synth_rate": synth_rate, "seed": seed}]
        multi = len(variants) > 1

        if bars is None or funding is None:
            from .kline_client import CachedHistoricalClient
            client = CachedHistoricalClient(cache_root=self._cache_root, symbol=symbol)
            bars = bars or await client.get_day_klines(day)
            funding = funding or await client.get_day_funding(day)

        cfg = settings()
        run_id = f"watch_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"
        tick_seconds = int(cfg["clock"]["tick_seconds"])
        total_ticks_est = len(bars) * 60 // tick_seconds

        engines_events: list[tuple[WatchEngine, Iterable[Event]]] = []
        for v in variants:
            vrate = int(v.get("synth_rate", synth_rate))
            vseed = int(v.get("seed", seed))
            vid = str(v.get("variant_id", f"synth_{vrate}_seed_{vseed}"))
            ledger_dir = (
                PROJECT_ROOT / "data" / "replays" / run_id / "ledger" / vid
                if multi else
                PROJECT_ROOT / "data" / "replays" / run_id / "ledger"
            )
            tribes, state, projector, decision, executor, regime_clf = _build_stack(
                cfg, ledger_dir=ledger_dir, rng_seed=vseed,
            )
            engine = WatchEngine(
                tribes=tribes, state=state, projector=projector,
                decision=decision, executor=executor,
                regime_classifier=regime_clf,
                tick_seconds=tick_seconds,
                broadcaster=self._broadcaster,
                run_id=run_id, day=day.isoformat(),
                speed=speed, total_ticks_est=total_ticks_est,
                variant_id=vid if multi else None,
                emit_replay_state=not multi,
            )
            evs = build_event_stream(bars, funding, synth_rate=vrate, symbol=symbol)
            engines_events.append((engine, evs))

        self._engines = [e for e, _ in engines_events]

        async def _run() -> None:
            try:
                if multi:
                    engine_tasks = [
                        asyncio.create_task(e.run(evs), name=f"watch_{vid}")
                        for (e, evs), vid in zip(
                            engines_events,
                            [v.get("variant_id", f"synth_{v.get('synth_rate',synth_rate)}_seed_{v.get('seed',seed)}")
                             for v in variants],
                        )
                    ]
                    conductor = asyncio.create_task(
                        _conduct(self._engines, self._broadcaster, run_id, day.isoformat()),
                        name=f"conductor_{run_id}",
                    )
                    try:
                        await asyncio.gather(*engine_tasks)
                    finally:
                        conductor.cancel()
                        await asyncio.gather(conductor, return_exceptions=True)
                    await self._broadcaster.publish({"type": "replay_state", "active": False})
                    logger.info("[watch] {} multi finished  n_variants={}", run_id, len(variants))
                else:
                    e, evs = engines_events[0]
                    result = await e.run(evs)
                    logger.info("[watch] {} finished  ticks={} Δeq={:+.2f}",
                                run_id, result["ticks"],
                                result["end_equity_usd"] - _STARTING_EQUITY)
            except asyncio.CancelledError:
                await self._broadcaster.publish({"type": "replay_state", "active": False})
                logger.info("[watch] {} cancelled", run_id)
            except Exception as exc:
                await self._broadcaster.publish({"type": "replay_state", "active": False})
                logger.exception("[watch] {} error: {}", run_id, exc)
            finally:
                self._engines.clear()
                self._task = None

        self._task = asyncio.create_task(_run(), name=f"watch_{run_id}")
        logger.info("[watch] started {}  n_variants={}  speed={}x", run_id, len(variants), speed)
        return run_id

    def set_speed(self, speed: float) -> None:
        for e in self._engines:
            e.speed = max(0.1, float(speed))

    def toggle_pause(self) -> bool:
        if not self._engines:
            return False
        paused = self._engines[0].toggle_pause()
        for e in self._engines[1:]:
            e._paused = paused
        return paused

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._engines.clear()
        await self._broadcaster.publish({"type": "replay_state", "active": False})
