"""Replay harness — wires a replay run from kline cache to decision log.

Sprint mode (phase 2) lives here as `run_sprint()`. Watch mode (phase 5+)
will add a paced async wrapper sharing the same engine stack.

SprintEngine is a dedicated synchronous driver — it mirrors the live
`TradingEngine._on_tick` logic but consumes a pre-built event stream and
writes per-tick decision rows instead of broadcasting over WebSocket.
Kept separate from `src/audit/replay.py:ReplayEngine` because the
tournament's replay contract is frozen (it scores variants on a saved
event tape); this driver's contract is different (cold-start, per-tick
log, regime-aware exits). Overlap is small and isolated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

import orjson
from loguru import logger

from ..aggregator.voting import aggregate_votes
from ..core.config import PROJECT_ROOT, settings
from ..core.types import Event, Side
from ..decision.engine import DecisionEngine
from ..execution.ledger import Ledger
from ..execution.paper import PaperExecutor
from ..projector.chess import ChessProjector
from ..regime.classifier import RegimeClassifier
from ..tribes.loader import load_all
from ..tribes.market_state import MarketState
from ..tribes.tribe import Tribe
from .decision_log import DecisionLogWriter
from .feed import build_event_stream
from .kline_client import CachedHistoricalClient


@dataclass(slots=True)
class SprintResult:
    run_id: str
    variant_id: str
    day: str
    seed: int
    synth_rate: int
    ticks: int
    events_consumed: int
    start_equity_usd: float
    end_equity_usd: float
    equity_delta_usd: float
    trades: int
    wins: int
    decisions_path: str
    manifest_path: str


class SprintEngine:
    """Synchronous driver that replays an event stream through a full
    tribes→council→decision stack and writes a per-tick log row.

    Mirrors live engine tick logic (regime-aware exits, funding accrual)
    except for the UI broadcaster and the async machinery. No auditor /
    calibrator / kernel_calibrator in phase 2 — they ship in a later
    phase when the summary report needs them for skill-shift metrics.
    """

    def __init__(
        self,
        tribes: list[Tribe],
        state: MarketState,
        projector: ChessProjector,
        decision: DecisionEngine,
        executor: PaperExecutor,
        regime_classifier: RegimeClassifier,
        tick_seconds: int,
        log_writer: DecisionLogWriter,
    ) -> None:
        self.tribes = tribes
        self.state = state
        self.projector = projector
        self.decision = decision
        self.executor = executor
        self.regime_classifier = regime_classifier
        self.tick_seconds = int(tick_seconds)
        self.log_writer = log_writer
        self.start_equity = executor.equity_usd
        self.ticks = 0
        self.events_consumed = 0

    def run(self, events: Iterable[Event]) -> dict:
        last_tick_ts: datetime | None = None
        last_ts: datetime | None = None
        for ev in events:
            self.events_consumed += 1
            last_ts = ev.ts
            self.state.ingest(ev)
            if last_tick_ts is None:
                last_tick_ts = ev.ts
                continue
            elapsed = (ev.ts - last_tick_ts).total_seconds()
            while elapsed >= self.tick_seconds:
                last_tick_ts = last_tick_ts + timedelta(seconds=self.tick_seconds)
                self._tick(last_tick_ts)
                elapsed = (ev.ts - last_tick_ts).total_seconds()
        # Force-close any remaining positions so equity reflects realized book.
        if last_ts is not None and self.state.last_price:
            for p in list(self.executor.positions):
                self.executor._close_position(p, self.state.last_price, last_ts, "sprint end")
            self.executor._positions = []
        return {
            "ticks": self.ticks,
            "events_consumed": self.events_consumed,
            "start_equity_usd": self.start_equity,
            "end_equity_usd": self.executor.equity_usd,
            "equity_delta_usd": self.executor.equity_usd - self.start_equity,
            "trades": self.executor.trades_completed,
            "wins": self.executor.wins,
        }

    def _tick(self, ts: datetime) -> None:
        self.ticks += 1
        if self.state.last_price is None:
            return

        raw_votes = [t.vote(ts, self.state) for t in self.tribes]

        regime = self.regime_classifier.classify(self.state)
        self.projector.regime_multiplier = regime.multiplier
        regime_label = regime.label

        agg = aggregate_votes(raw_votes, ts)
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

        self.log_writer.write_tick(
            tick=self.ticks,
            ts=ts,
            price=self.state.last_price,
            regime_label=regime_label,
            votes=raw_votes,
            net_pressure=agg.net_pressure,
            uncertainty=agg.uncertainty,
            forecast=forecast,
            action=intent.action,
            action_reason=intent.reason,
            equity_usd=self.executor.equity_usd,
            positions_open=len(self.executor.positions),
        )

        if intent.action != "hold":
            self.executor.execute(intent, self.state)


def _build_stack(
    cfg: dict,
    ledger_dir: Path,
    rng_seed: int,
    starting_equity_usd: float | None = None,
    tribes_dir: Path | None = None,
) -> tuple[list[Tribe], MarketState, ChessProjector, DecisionEngine,
           PaperExecutor, RegimeClassifier]:
    tribes = load_all(rng_seed=rng_seed, tribes_dir=tribes_dir)
    if not tribes:
        raise RuntimeError(
            f"no tribes loaded — check {tribes_dir or 'config/tribes/'}/*.yaml"
        )
    state = MarketState()
    projector = ChessProjector(
        tribes=tribes,
        steps=int(cfg["clock"]["projector_steps"]),
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        bps_per_unit_pressure=float(cfg["projector"]["bps_per_unit_pressure"]),
        confidence_decay=float(cfg["projector"]["confidence_decay"]),
        rng_seed=rng_seed,
    )
    decision = DecisionEngine(cfg)
    ledger = Ledger(directory=ledger_dir)
    _start_eq = (starting_equity_usd if starting_equity_usd is not None
                 else float(cfg.get("base_capital_usd", 5000)))
    _risk = cfg.get("risk", {}) or {}
    _exec = cfg.get("execution", {}) or {}
    executor = PaperExecutor(
        starting_equity_usd=_start_eq,
        fee_rate_bps=float(_exec.get("fee_rate_bps", 5.0)),
        slippage_bps=float(_exec.get("slippage_bps", 2.0)),
        max_concurrent_positions=int(_risk.get("max_concurrent_positions", 20)),
        max_total_notional_pct=float(_risk.get("max_total_notional_pct", 500)),
        max_hold_seconds=int(_risk.get("max_hold_seconds", 600)),
        exit_dead_zone_bps=float(cfg["decision"].get("exit_dead_zone_bps", 0.5)),
        ledger=ledger,
        exit_strategy=cfg.get("exit_strategy", {}) or {},
        slippage_size_k=float(_exec.get("slippage_size_k", 0.0)),
        slippage_ref_notional_usd=float(_exec.get("slippage_ref_notional_usd", 100_000)),
        max_drawdown_from_peak_pct=float(_risk.get("max_drawdown_from_peak_pct", 0.0)),
        max_daily_loss_pct=float(_risk.get("max_daily_loss_pct", 0.0)),
        max_session_dd_pct=float(_risk.get("max_session_dd_pct", 0.0)),
        max_position_loss_pct=float(_risk.get("max_position_loss_pct", 0.0)),
        black_swan_sigma=float(_risk.get("black_swan_sigma", 0.0)),
        black_swan_window_s=float(_risk.get("black_swan_window_s", 30.0)),
        black_swan_sigma_window_s=float(_risk.get("black_swan_sigma_window_s", 300.0)),
        black_swan_cooldown_s=float(_risk.get("black_swan_cooldown_s", 600.0)),
        slippage_cap_bps=float(_exec.get("slippage_cap_bps", 30.0)),
        book_depth_frac_cap=float(_exec.get("book_depth_frac_cap", 0.25)),
        half_spread_bps=float(_exec.get("half_spread_bps", 0.5)),
    )
    regime_classifier = RegimeClassifier(cfg.get("regime", {}) or {})
    return tribes, state, projector, decision, executor, regime_classifier


async def run_sprint(
    *,
    day: date,
    synth_rate: int,
    seed: int = 42,
    run_id: str | None = None,
    variant_id: str | None = None,
    outputs_root: Path | None = None,
    cache_root: Path | None = None,
    symbol: str = "BTCUSDT",
    bars: list[dict] | None = None,
    funding: list[dict] | None = None,
    starting_equity_usd: float | None = None,
    tribes_dir: Path | None = None,
) -> SprintResult:
    """Run one sprint-mode replay of `day` at the given `synth_rate`.

    All output lives under `outputs_root / run_id / variants / variant_id / `:
      - decisions.jsonl   (per-tick log)
      - manifest.json     (inputs for reproducibility)
      - ledger/           (executor's per-trade audit)

    Same `(day, seed, synth_rate)` → byte-identical `decisions.jsonl`.
    That's the phase-2 exit criterion."""
    cfg = settings()
    repl = cfg.get("replay", {}) or {}

    if run_id is None:
        run_id = f"sprint_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"
    if variant_id is None:
        variant_id = f"synth_{synth_rate}"
    if outputs_root is None:
        outputs_root = PROJECT_ROOT / repl.get("outputs_dir", "data/replays")

    run_dir = Path(outputs_root) / run_id / "variants" / variant_id
    run_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = run_dir / "decisions.jsonl"
    manifest_path = run_dir / "manifest.json"
    ledger_dir = run_dir / "ledger"

    if bars is None or funding is None:
        cache_dir = cache_root if cache_root is not None else (
            PROJECT_ROOT / repl.get("cache_dir", "data")
        )
        client = CachedHistoricalClient(cache_root=cache_dir, symbol=symbol)
        logger.info("[sprint] fetching {} 1m klines + funding for {}", symbol, day.isoformat())
        if bars is None:
            bars = await client.get_day_klines(day)
        if funding is None:
            funding = await client.get_day_funding(day)
    logger.info("[sprint] loaded {} bars + {} funding events", len(bars), len(funding))

    tribes, state, projector, decision, executor, regime_classifier = _build_stack(
        cfg, ledger_dir=ledger_dir, rng_seed=seed,
        starting_equity_usd=starting_equity_usd,
        tribes_dir=tribes_dir,
    )
    logger.info("[sprint] loaded {} tribes (seed={})", len(tribes), seed)

    with DecisionLogWriter(decisions_path) as writer:
        engine = SprintEngine(
            tribes=tribes, state=state, projector=projector,
            decision=decision, executor=executor,
            regime_classifier=regime_classifier,
            tick_seconds=int(cfg["clock"]["tick_seconds"]),
            log_writer=writer,
        )
        events = build_event_stream(bars, funding, synth_rate=synth_rate, symbol=symbol)
        summary = engine.run(events)

    result = SprintResult(
        run_id=run_id,
        variant_id=variant_id,
        day=day.isoformat(),
        seed=seed,
        synth_rate=synth_rate,
        ticks=summary["ticks"],
        events_consumed=summary["events_consumed"],
        start_equity_usd=summary["start_equity_usd"],
        end_equity_usd=summary["end_equity_usd"],
        equity_delta_usd=summary["equity_delta_usd"],
        trades=summary["trades"],
        wins=summary["wins"],
        decisions_path=str(decisions_path),
        manifest_path=str(manifest_path),
    )

    manifest = {
        "run_id": run_id,
        "variant_id": variant_id,
        "day": day.isoformat(),
        "symbol": symbol,
        "seed": seed,
        "synth_rate": synth_rate,
        "tick_seconds": int(cfg["clock"]["tick_seconds"]),
        "bars": len(bars),
        "funding_events": len(funding),
        "ticks": summary["ticks"],
        "events_consumed": summary["events_consumed"],
        "start_equity_usd": summary["start_equity_usd"],
        "end_equity_usd": summary["end_equity_usd"],
        "equity_delta_usd": summary["equity_delta_usd"],
        "trades": summary["trades"],
        "wins": summary["wins"],
        "tribes_dir": str(tribes_dir) if tribes_dir is not None else None,
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
    }
    manifest_path.write_bytes(orjson.dumps(manifest, option=orjson.OPT_INDENT_2))

    logger.info(
        "[sprint] done  ticks={} trades={} Δeq=${:+.2f}  → {}",
        result.ticks, result.trades, result.equity_delta_usd, decisions_path,
    )
    return result


async def run_watch(
    *,
    day: date,
    broadcaster,
    speed: float = 10.0,
    synth_rate: int = 60,
    seed: int = 42,
    symbol: str = "BTCUSDT",
    run_id: str | None = None,
    cache_root: Path | None = None,
    bars: list[dict] | None = None,
    funding: list[dict] | None = None,
) -> None:
    """Run a watch-mode replay, broadcasting each tick to `broadcaster`.

    Equivalent to run_sprint but paced at `speed × real-time` instead of
    maximum throughput. Use WatchController.start() for server integration
    (handles lifecycle + cancellation); call this directly for scripted
    sessions."""
    from .watch_engine import WatchEngine

    cfg = settings()
    repl = cfg.get("replay", {}) or {}
    if run_id is None:
        run_id = f"watch_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"

    if bars is None or funding is None:
        cache_dir = cache_root if cache_root is not None else (
            PROJECT_ROOT / repl.get("cache_dir", "data")
        )
        client = CachedHistoricalClient(cache_root=cache_dir, symbol=symbol)
        if bars is None:
            bars = await client.get_day_klines(day)
        if funding is None:
            funding = await client.get_day_funding(day)

    ledger_dir = PROJECT_ROOT / "data" / "replays" / run_id / "ledger"
    tribes, state, projector, decision, executor, regime_classifier = _build_stack(
        cfg, ledger_dir=ledger_dir, rng_seed=seed,
    )
    tick_seconds = int(cfg["clock"]["tick_seconds"])
    total_ticks_est = len(bars) * 60 // tick_seconds

    engine = WatchEngine(
        tribes=tribes, state=state, projector=projector,
        decision=decision, executor=executor,
        regime_classifier=regime_classifier,
        tick_seconds=tick_seconds,
        broadcaster=broadcaster,
        run_id=run_id, day=day.isoformat(),
        speed=speed, total_ticks_est=total_ticks_est,
    )
    events = build_event_stream(bars, funding, synth_rate=synth_rate, symbol=symbol)
    await engine.run(events)
