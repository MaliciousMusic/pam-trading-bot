"""Full paper-trading run: ingress → tribes → aggregator → projector → decision → paper executor.

From the project root:
    python -m scripts.run_bot
"""
from __future__ import annotations

import asyncio

from loguru import logger

from src.core.bus import EventBus
from src.core.config import data_sources, settings
from src.core.engine import TradingEngine
from src.decision.engine import DecisionEngine
from src.execution.ledger import Ledger
from src.execution.paper import PaperExecutor
from src.ingress.binance_ws import BinancePerpsWS
from src.ingress.bybit_perps_ws import BybitPerpsWS
from src.projector.chess import ChessProjector
from src.tribes.loader import load_all
from src.tribes.market_state import MarketState


async def main() -> None:
    cfg = settings()
    venue = cfg.get("exchange", "binance_perps")

    tribes = load_all()
    if not tribes:
        logger.error("no tribes loaded; aborting")
        return

    bus = EventBus()
    state = MarketState()
    ledger = Ledger()

    executor = PaperExecutor(
        starting_equity_usd=float(cfg.get("base_capital_usd", 5000)),
        fee_rate_bps=float(cfg["execution"]["fee_rate_bps"]),
        slippage_bps=float(cfg["execution"]["slippage_bps"]),
        ledger=ledger,
    )
    projector = ChessProjector(
        tribes=tribes,
        steps=int(cfg["clock"]["projector_steps"]),
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        bps_per_unit_pressure=float(cfg["projector"]["bps_per_unit_pressure"]),
        confidence_decay=float(cfg["projector"]["confidence_decay"]),
    )
    decision = DecisionEngine(cfg)

    engine = TradingEngine(
        bus=bus, tribes=tribes, state=state,
        projector=projector, decision=decision,
        executor=executor, ledger=ledger,
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
    )

    # Primary-venue client selection. 2026-04-24 venue swap (PLAN.md §5.1.2):
    # Binance USD-M futures geo-blocked; Bybit V5 linear is the replacement.
    if venue == "bybit_perps":
        client = BybitPerpsWS(bus, symbol=cfg["symbol"])
    else:
        ds = data_sources()["sources"]["binance_perps_ws"]
        client = BinancePerpsWS(bus, streams=ds["streams"])
    logger.info("mode={}  venue={}  symbol={}  starting equity ${:.2f}",
                cfg["mode"], venue, cfg["symbol"], executor.equity_usd)
    await asyncio.gather(client.run(), engine.run())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
