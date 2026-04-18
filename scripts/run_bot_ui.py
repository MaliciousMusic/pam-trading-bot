"""Full paper-trading run with the live UI dashboard.

    python -m scripts.run_bot_ui

Then open http://127.0.0.1:8765 in a browser.
"""
from __future__ import annotations

import asyncio

import uvicorn
from loguru import logger

from src.audit.backscore import ForecastAuditor
from src.audit.calibrator import TribeCalibrator
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
from src.ingress.rss_feeds import CURATED_FEEDS, RSSFeedPoller
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
        max_concurrent_positions=int(cfg["risk"]["max_concurrent_positions"]),
        max_total_notional_pct=float(cfg["risk"]["max_total_notional_pct"]),
        max_hold_seconds=int(cfg["risk"]["max_hold_seconds"]),
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
    auditor = ForecastAuditor()
    # Audit→calibration closed loop. `apply=False` by default: measure
    # per-tribe skill from resolved anchors, surface it via /api/calibration,
    # but don't scale votes yet. Flip the yaml flag once there's enough
    # signal (≥ min_samples resolved anchors per tribe — default 30).
    cal_cfg = cfg.get("calibration", {}) or {}
    calibrator = TribeCalibrator(
        halflife_resolved=int(cal_cfg.get("halflife_resolved", 500)),
        min_samples=int(cal_cfg.get("min_samples", 30)),
        max_shrink=float(cal_cfg.get("max_shrink", 0.6)),
        max_boost=float(cal_cfg.get("max_boost", 1.4)),
        apply=bool(cal_cfg.get("apply", False)),
    )

    engine = TradingEngine(
        bus=bus, tribes=tribes, state=state,
        projector=projector, decision=decision,
        executor=executor, ledger=ledger,
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        broadcaster=broadcaster,
        auditor=auditor,
        calibrator=calibrator,
    )

    client = BinancePerpsWS(bus, streams=ds["streams"])
    ui_cfg = {
        "min_confidence": float(cfg["decision"]["min_confidence"]),
        "min_expected_move_bps": float(cfg["decision"]["min_expected_move_bps"]),
        "max_uncertainty": float(cfg["decision"]["max_uncertainty"]),
        "size_min_pct": float(cfg["sizing"]["min_pct_equity"]),
        "size_max_pct": float(cfg["sizing"]["max_pct_equity"]),
        "sizing_curve": str(cfg["sizing"]["curve"]),
        "leverage": float(cfg.get("leverage", 1.0)),
        "max_concurrent_positions": int(cfg["risk"]["max_concurrent_positions"]),
        "max_total_notional_pct": float(cfg["risk"]["max_total_notional_pct"]),
        "sl_ratio": float(cfg["decision"].get("sl_ratio", 0.7)),
        "sl_min_bps": float(cfg["decision"].get("sl_min_bps", 20.0)),
        "bps_per_unit_pressure": float(cfg["projector"]["bps_per_unit_pressure"]),
        "projector_steps": int(cfg["clock"]["projector_steps"]),
        "tick_seconds": int(cfg["clock"]["tick_seconds"]),
    }
    app = create_app(broadcaster, _tribe_meta(tribes), ui_cfg,
                     auditor=auditor, calibrator=calibrator)

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
    # Curated free OSINT feeds — regulation (SEC/Fed/CFTC), crypto news,
    # influencer substacks + Nitter. Each feed is polled politely; dead
    # feeds log a warning and the poller continues with the rest.
    rss_cfg = sources_cfg.get("rss_news", {})
    if rss_cfg.get("enabled"):
        coros.append(RSSFeedPoller(
            bus, CURATED_FEEDS,
            poll_seconds=int(rss_cfg.get("poll_seconds", 180)),
            retry_seconds=int(rss_cfg.get("retry_seconds", 120)),
        ).run())

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
