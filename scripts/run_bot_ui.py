"""Full paper-trading run with the live UI dashboard.

    python -m scripts.run_bot_ui

Then open http://127.0.0.1:8765 in a browser.
"""
from __future__ import annotations

import asyncio

import uvicorn
from loguru import logger

from src.audit.scheduler import DailyAuditScheduler
from src.core.bus import EventBus
from src.core.config import data_sources, settings
from src.core.engine import TradingEngine
from src.decision.engine import DecisionEngine
from src.execution.ledger import Ledger
from src.execution.paper import PaperExecutor
from src.ingress.binance_ws import BinancePerpsWS
from src.ingress.fgi import FGIPoller
from src.ingress.reddit_rss import RedditRSSPoller
from src.projector.chess import ChessProjector
from src.tribes.loader import load_all
from src.tribes.market_state import MarketState
from src.ui.broadcaster import UIBroadcaster
from src.ui.server import create_app


def _tribe_meta(tribes):
    return [
        {
            "id": t.spec.id,
            "name": t.spec.name,
            "description": t.spec.description,
            "weight": t.spec.flow.weight,
            "capital_band_usd": t.spec.demographics.capital_band_usd,
            "hardware_tier": t.spec.hardware.tier,
            "strategy_family": t.spec.strategy.family,
            "timezone_label": t.spec.circadian.timezone_label,
        }
        for t in tribes
    ]


async def main() -> None:
    cfg = settings()
    sources_cfg = data_sources()["sources"]
    ds = sources_cfg["binance_perps_ws"]

    tribes = load_all()
    if not tribes:
        logger.error("no tribes loaded; aborting")
        return

    bus = EventBus()
    state = MarketState()
    ledger = Ledger()
    broadcaster = UIBroadcaster()

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
        broadcaster=broadcaster,
    )

    client = BinancePerpsWS(bus, streams=ds["streams"])
    ui_cfg = {
        "min_confidence": float(cfg["decision"]["min_confidence"]),
        "min_expected_move_bps": float(cfg["decision"]["min_expected_move_bps"]),
        "max_uncertainty": float(cfg["decision"]["max_uncertainty"]),
        "size_min_pct": float(cfg["sizing"]["min_pct_equity"]),
        "size_max_pct": float(cfg["sizing"]["max_pct_equity"]),
        "sizing_curve": str(cfg["sizing"]["curve"]),
        "bps_per_unit_pressure": float(cfg["projector"]["bps_per_unit_pressure"]),
        "projector_steps": int(cfg["clock"]["projector_steps"]),
        "tick_seconds": int(cfg["clock"]["tick_seconds"]),
    }
    app = create_app(broadcaster, _tribe_meta(tribes), ui_cfg)

    host = cfg.get("ui", {}).get("host", "127.0.0.1")
    port = int(cfg.get("ui", {}).get("port", 8765))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    # Tasks to gather. Core trading loop + exchange stream always on.
    coros = [client.run(), engine.run(), server.serve()]

    # Sentiment feeds (free) — wake up the news_reactor tribe.
    if sources_cfg.get("fear_greed_index", {}).get("enabled"):
        coros.append(FGIPoller(bus).run())
    if sources_cfg.get("reddit_public", {}).get("enabled"):
        subs = sources_cfg["reddit_public"].get("subs")
        coros.append(RedditRSSPoller(bus, subs=subs).run())

    # Daily audit scheduler — fires once at 00:00 UTC; generates hypotheses
    # (atomic single-param mutations) and writes child variants. The parent
    # keeps trading uninterrupted.
    tribe_specs = [t.spec for t in tribes]
    audit_horizon = int(cfg.get("audit", {}).get("horizon_seconds", 60))
    coros.append(DailyAuditScheduler(horizon_seconds=audit_horizon).run(tribe_specs))

    logger.info("mode={} symbol={} equity=${:.2f}  UI at http://{}:{}",
                cfg["mode"], cfg["symbol"], executor.equity_usd, host, port)
    logger.info("[boot] {} background tasks: exchange WS, trading engine, UI server, "
                "audit scheduler + {} sentiment feed(s)",
                len(coros), len(coros) - 4)
    await asyncio.gather(*coros)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
