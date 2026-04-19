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
from .decision_log import DecisionLogWriter
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
        decisions_path: Path | None = None,
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
        self._decisions_path = decisions_path
        self._log_writer: DecisionLogWriter | None = None

        self._paused = False
        self._ticks = 0
        self._events_consumed = 0
        self._recent_trades: deque[dict] = deque(maxlen=20)
        self._start_equity = executor.equity_usd

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        return self._paused

    async def run(self, events: Iterable[Event]) -> dict:
        if self._decisions_path is not None:
            self._log_writer = DecisionLogWriter(self._decisions_path)
        try:
            return await self._run_inner(events)
        finally:
            if self._log_writer is not None:
                self._log_writer.close()
                self._log_writer = None

    async def _run_inner(self, events: Iterable[Event]) -> dict:
        if self._emit_replay_state:
            await self.broadcaster.publish_replay(self._replay_state_msg())

        last_tick_ts: datetime | None = None
        last_ts: datetime | None = None

        for ev in events:
            # Pause loop — yields to event loop every 100ms while paused
            while self._paused:
                await asyncio.sleep(0.1)

            self._events_consumed += 1
            last_ts = ev.ts
            self.state.ingest(ev)

            # Yield every 50 events so the WS send queue + live engine + any
            # pending HTTP handlers can run. state.ingest is cheap (~µs) but
            # hundreds of consecutive calls between ticks would otherwise
            # starve the event loop for tens of ms at a time.
            if self._events_consumed % 50 == 0:
                await asyncio.sleep(0)

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
            await self.broadcaster.publish_replay({**self._replay_state_msg(), "active": False})
        return {
            "ticks": self._ticks,
            "events_consumed": self._events_consumed,
            "end_equity_usd": self.executor.equity_usd,
        }

    async def _tick(self, ts: datetime) -> None:
        # Pace: sleep before doing work so a cancel during sleep is clean.
        # Clamp sleep to at least 50ms so the event loop stays responsive at
        # high speeds — tribe votes + projector can take 100-500ms CPU each
        # tick; without breathing room, the WS sender and HTTP handlers
        # starve and the UI appears frozen (speed slider, stop button, etc.).
        # Effective speed at 3s ticks is capped at ~60x by this floor.
        sleep_s = max(self.tick_seconds / max(self.speed, 0.1), 0.05)
        await asyncio.sleep(sleep_s)

        self._ticks += 1
        if self.state.last_price is None:
            return

        # Yield between each CPU-heavy step so HTTP handlers and the WS
        # sender can interleave. Without these yields, a single tick blocks
        # the event loop for 100-500ms, which at high replay speeds
        # (cadence < 50ms) freezes the UI.
        raw_votes = []
        for t in self.tribes:
            raw_votes.append(t.vote(ts, self.state))
            await asyncio.sleep(0)
        regime = self.regime_classifier.classify(self.state)
        self.projector.regime_multiplier = regime.multiplier
        agg = aggregate_votes(raw_votes, ts)
        await asyncio.sleep(0)
        forecast = self.projector.project(self.state)
        await asyncio.sleep(0)

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

        if self._log_writer is not None:
            self._log_writer.write_tick(
                tick=self._ticks, ts=ts, price=self.state.last_price,
                regime_label=regime.label.value if regime.label else None,
                votes=raw_votes, net_pressure=agg.net_pressure,
                uncertainty=agg.uncertainty, forecast=forecast,
                action=intent.action, action_reason=intent.reason,
                equity_usd=self.executor.equity_usd,
                positions_open=len(self.executor.positions),
            )

        positions = self.executor.positions
        recent_trades = list(getattr(self.executor, "recent_trades", []))

        await self.broadcaster.publish_replay({
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
            "forecast_sample_paths_tribe_ids": forecast.sample_paths_tribe_ids,
            "projector_weights": dict(getattr(self.projector, "_weights", {}) or {}),
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
            await self.broadcaster.publish_replay(self._replay_state_msg())

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
        await broadcaster.publish_replay({
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
        days: int = 1,
    ) -> str:
        """Start a new watch run, cancelling any active session.

        `variants` is an optional list of dicts with keys synth_rate, seed,
        and optionally variant_id.  When >1 variant, multi-variant mode is
        used: N concurrent engines + conductor.  When absent or single-element,
        falls back to single-variant behaviour (variant_id=None on tick msgs).

        `days` runs that many consecutive UTC days back-to-back inside one
        session, with per-variant equity carry-over. days=1 is the classic
        single-day replay.

        Returns the run_id. The actual data fetch + engine construction happens
        in the background task so this returns instantly."""
        if self.active:
            await self.stop()

        # Mute live-engine ticks so replay prices don't mix with real prices.
        # Also flush the tick buffer so newly-connected clients don't receive
        # stale live prices that would corrupt the replay price baseline (p0).
        self._broadcaster.mute_live = True
        self._broadcaster._tick_buffer.clear()

        from ..core.config import PROJECT_ROOT, settings

        # Normalise variants list
        if not variants:
            variants = [{"synth_rate": synth_rate, "seed": seed}]
        multi = len(variants) > 1
        days = max(1, int(days))

        cfg = settings()
        run_id = f"watch_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"
        tick_seconds = int(cfg["clock"]["tick_seconds"])

        async def _run() -> None:
            from .harness import _build_stack
            from .kline_client import CachedHistoricalClient
            try:
                # Broadcast a "preparing" replay_state immediately so the UI
                # knows the run is active and closes the start modal. Actual
                # ticks flow once bars are fetched.
                await self._broadcaster.publish_replay({
                    "type": "replay_state", "active": True,
                    "run_id": run_id, "day": day.isoformat(),
                    "speed": speed, "tick": 0, "total_ticks_est": 1,
                    "progress_pct": 0.0, "paused": False, "preparing": True,
                })

                client = CachedHistoricalClient(cache_root=self._cache_root, symbol=symbol)

                # Build per-variant carry_equity so multi-day preserves equity
                # across days (same contract as Phase 9 sprint orchestrator).
                def _variant_id(v: dict) -> str:
                    return str(v.get("variant_id",
                        f"synth_{int(v.get('synth_rate',synth_rate))}_seed_{int(v.get('seed',seed))}"))
                carry_equity: dict[str, float] = {}

                for day_idx in range(days):
                    current_day = day + timedelta(days=day_idx)
                    if bars is not None and day_idx == 0:
                        day_bars = bars
                        day_funding = funding or []
                    else:
                        day_bars = await client.get_day_klines(current_day)
                        day_funding = await client.get_day_funding(current_day)

                    total_ticks_est = len(day_bars) * 60 // tick_seconds

                    day_engines_events: list[tuple[WatchEngine, Iterable[Event]]] = []
                    for v in variants:
                        vrate = int(v.get("synth_rate", synth_rate))
                        vseed = int(v.get("seed", seed))
                        vid = _variant_id(v)
                        run_root = PROJECT_ROOT / "data" / "replays" / run_id
                        day_suffix = f"day{day_idx+1}" if days > 1 else ""
                        if multi:
                            ledger_dir = run_root / "ledger" / vid / day_suffix if day_suffix else run_root / "ledger" / vid
                            decisions_path = (run_root / "variants" / vid / f"decisions_{day_suffix}.jsonl"
                                              if day_suffix else run_root / "variants" / vid / "decisions.jsonl")
                        else:
                            ledger_dir = run_root / "ledger" / day_suffix if day_suffix else run_root / "ledger"
                            decisions_path = (run_root / f"decisions_{day_suffix}.jsonl"
                                              if day_suffix else run_root / "decisions.jsonl")
                        decisions_path.parent.mkdir(parents=True, exist_ok=True)
                        starting_eq = carry_equity.get(vid)
                        tribes, state, projector, decision, executor, regime_clf = _build_stack(
                            cfg, ledger_dir=ledger_dir, rng_seed=vseed,
                            starting_equity_usd=starting_eq,
                        )
                        engine = WatchEngine(
                            tribes=tribes, state=state, projector=projector,
                            decision=decision, executor=executor,
                            regime_classifier=regime_clf,
                            tick_seconds=tick_seconds,
                            broadcaster=self._broadcaster,
                            run_id=run_id, day=current_day.isoformat(),
                            speed=speed, total_ticks_est=total_ticks_est,
                            variant_id=vid if multi else None,
                            emit_replay_state=not multi,
                            decisions_path=decisions_path,
                        )
                        evs = build_event_stream(day_bars, day_funding, synth_rate=vrate, symbol=symbol)
                        day_engines_events.append((engine, evs))

                    self._engines = [e for e, _ in day_engines_events]

                    if multi:
                        engine_tasks = [
                            asyncio.create_task(e.run(evs), name=f"watch_{_variant_id(v)}_d{day_idx+1}")
                            for (e, evs), v in zip(day_engines_events, variants)
                        ]
                        conductor = asyncio.create_task(
                            _conduct(self._engines, self._broadcaster, run_id, current_day.isoformat()),
                            name=f"conductor_{run_id}_d{day_idx+1}",
                        )
                        try:
                            results = await asyncio.gather(*engine_tasks)
                        finally:
                            conductor.cancel()
                            await asyncio.gather(conductor, return_exceptions=True)
                        for v, r in zip(variants, results):
                            carry_equity[_variant_id(v)] = r["end_equity_usd"]
                        logger.info("[watch] {} day {}/{} multi done  n_variants={}",
                                    run_id, day_idx+1, days, len(variants))
                    else:
                        e, evs = day_engines_events[0]
                        vid0 = _variant_id(variants[0])
                        day_start_eq = carry_equity.get(vid0, _STARTING_EQUITY)
                        result = await e.run(evs)
                        carry_equity[vid0] = result["end_equity_usd"]
                        logger.info("[watch] {} day {}/{} done  ticks={} Δeq={:+.2f}",
                                    run_id, day_idx+1, days, result["ticks"],
                                    result["end_equity_usd"] - day_start_eq)
            except asyncio.CancelledError:
                logger.info("[watch] {} cancelled", run_id)
            except Exception as exc:
                logger.exception("[watch] {} error: {}", run_id, exc)
            finally:
                # Always restore live ticks and clear replay state, regardless of
                # success/cancel/exception. Previously a successful single-variant
                # finish left mute_live=True, which silently broke the live view.
                self._broadcaster.mute_live = False
                self._broadcaster._tick_buffer.clear()
                try:
                    await self._broadcaster.publish_replay({"type": "replay_state", "active": False})
                except Exception:
                    pass
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
        # Restore live-engine ticks and flush replay prices from the buffer
        # so the live chart starts fresh from the current real price.
        self._broadcaster._tick_buffer.clear()
        self._broadcaster.mute_live = False
        await self._broadcaster.publish_replay({"type": "replay_state", "active": False})
