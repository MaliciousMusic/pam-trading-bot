"""Live pulse test -- minimal stack, dedicated ledger dir, time-boxed.

Purpose: prove (or disprove) that the 3 replay-mute tribes
(microstructure_prober, put_call_fader, options_dealer_gamma) vote when
their data channels actually deliver. The live-mode replacement for what
replay can't simulate.

Wiring (strictly the minimum to produce a ledger with tick/vote rows):
  - BinancePerpsWS (@aggTrade, @forceOrder, @markPrice@1s, @depth20@100ms)
  - DeribitOptionsPoller (kind=options_stats every 120s)
  - load_all() tribes + ChessProjector + DecisionEngine + PaperExecutor
  - Ledger writes to data/ledger_pulse/YYYY-MM-DD.jsonl so it does NOT
    collide with the production ledger under data/ledger/.

Everything else the full bot would spin up (UI, shadow ledgers, calibrator,
regime, audit scheduler, HTF poller, Reddit/RSS, cross-venue OI, Bybit/OKX
trades) is deliberately OFF -- we want a small, fast, clean pulse ledger.

Usage:
    python -m scripts.run_live_pulse --seconds 300          # 5-minute default
    python -m scripts.run_live_pulse --seconds 60           # 1 minute
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

from src.core.bus import EventBus
from src.core.config import PROJECT_ROOT, data_sources, settings
from src.core.engine import TradingEngine
from src.decision.engine import DecisionEngine
from src.execution.ledger import Ledger
from src.execution.paper import PaperExecutor
from src.ingress.binance_ws import BinancePerpsWS
from src.ingress.deribit_options import DeribitOptionsPoller
from src.projector.chess import ChessProjector
from src.tribes.loader import load_all
from src.tribes.market_state import MarketState


PULSE_LEDGER_DIR = PROJECT_ROOT / "data" / "ledger_pulse"


async def _run(duration_s: float) -> int:
    cfg = settings()
    sources_cfg = data_sources()["sources"]
    binance_cfg = sources_cfg["binance_perps_ws"]
    deribit_cfg = sources_cfg.get("deribit_options") or {}

    tribes = load_all()
    if not tribes:
        print("[pulse] no tribes loaded, aborting")
        return 2

    bus = EventBus()
    state = MarketState()

    PULSE_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(directory=PULSE_LEDGER_DIR)

    exec_cfg = cfg.get("execution", {}) or {}
    executor = PaperExecutor(
        starting_equity_usd=float(cfg.get("base_capital_usd", 50_000)),
        fee_rate_bps=float(exec_cfg.get("fee_rate_bps", 4.0)),
        slippage_bps=float(exec_cfg.get("slippage_bps", 1.0)),
        ledger=ledger,
    )
    proj_cfg = cfg.get("projector", {}) or {}
    projector = ChessProjector(
        tribes=tribes,
        steps=int(cfg["clock"]["projector_steps"]),
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        bps_per_unit_pressure=float(proj_cfg["bps_per_unit_pressure"]),
        confidence_decay=float(proj_cfg["confidence_decay"]),
    )
    decision = DecisionEngine(cfg)

    engine = TradingEngine(
        bus=bus, tribes=tribes, state=state,
        projector=projector, decision=decision,
        executor=executor, ledger=ledger,
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
    )

    binance = BinancePerpsWS(bus, streams=binance_cfg["streams"])

    coros = [binance.run(), engine.run()]
    if deribit_cfg.get("enabled"):
        coros.append(DeribitOptionsPoller(
            bus,
            currency=str(deribit_cfg.get("currency", "BTC")),
            poll_seconds=int(deribit_cfg.get("poll_seconds", 120)),
            retry_seconds=int(deribit_cfg.get("retry_seconds", 60)),
        ).run())
    else:
        print("[pulse] WARN: deribit_options disabled in config -- options tribes will stay mute")

    print(f"[pulse] starting {len(tribes)} tribes, duration {duration_s:.0f}s, "
          f"ledger at {PULSE_LEDGER_DIR}")
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                               timeout=duration_s)
    except asyncio.TimeoutError:
        # Expected exit path: we hit the time box. Cancel everything cleanly.
        pass
    finally:
        for t in tasks:
            t.cancel()
        # Drain cancellations so background tasks don't leak.
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print(f"[pulse] done. Ledger rows written to {PULSE_LEDGER_DIR}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=300.0,
                    help="Wall-clock duration of the pulse test (default: 300s / 5min)")
    args = ap.parse_args()

    # Quieten the INFO stream so the pulse output stays readable.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    try:
        return asyncio.run(_run(args.seconds))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
