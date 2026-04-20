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
from src.core.config import PROJECT_ROOT, data_sources, settings
from src.core.engine import TradingEngine
from src.decision.engine import DecisionEngine
from src.execution.ledger import Ledger
from src.execution.paper import PaperExecutor
from src.ingress.binance_ws import BinancePerpsWS
from src.ingress.bybit_ws import BybitPublicWS
from src.ingress.event_recorder import EventRecorder
from src.ingress.fgi import FGIPoller
from src.ingress.htf_trend_poller import HTFTrendPoller
from src.ingress.oi_poller import OIPoller
from src.ingress.okx_ws import OKXPublicWS
from src.ingress.reddit_rss import RedditRSSPoller
from src.ingress.rss_feeds import CURATED_FEEDS, RSSFeedPoller
from src.projector.chess import ChessProjector
from src.projector.kernel_calibrator import KernelCalibrator
from src.regime.classifier import RegimeClassifier
from src.tribes.loader import load_all
from src.tribes.market_state import MarketState
from src.ui.broadcaster import UIBroadcaster
from src.ui.server import create_app
from src.replay.watch_engine import WatchController


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
    repl_cfg = cfg.get("replay", {}) or {}
    cache_root = PROJECT_ROOT / repl_cfg.get("cache_dir", "data")
    watch_controller = WatchController(broadcaster=broadcaster, cache_root=cache_root)

    exec_cfg = cfg.get("execution", {}) or {}
    risk_cfg = cfg.get("risk", {}) or {}
    executor = PaperExecutor(
        starting_equity_usd=float(cfg.get("base_capital_usd", 5000)),
        fee_rate_bps=float(exec_cfg["fee_rate_bps"]),
        slippage_bps=float(exec_cfg["slippage_bps"]),
        max_concurrent_positions=int(risk_cfg["max_concurrent_positions"]),
        max_total_notional_pct=float(risk_cfg["max_total_notional_pct"]),
        max_hold_seconds=int(risk_cfg["max_hold_seconds"]),
        exit_dead_zone_bps=float(cfg["decision"].get("exit_dead_zone_bps", 0.5)),
        ledger=ledger,
        exit_strategy=cfg.get("exit_strategy", {}) or {},
        max_drawdown_from_peak_pct=float(risk_cfg.get("max_drawdown_from_peak_pct", 0.0)),
        max_daily_loss_pct=float(risk_cfg.get("max_daily_loss_pct", 0.0)),
        slippage_size_k=float(exec_cfg.get("slippage_size_k", 0.0)),
        slippage_ref_notional_usd=float(exec_cfg.get("slippage_ref_notional_usd", 100_000.0)),
        max_session_dd_pct=float(risk_cfg.get("max_session_dd_pct", 0.0)),
        max_position_loss_pct=float(risk_cfg.get("max_position_loss_pct", 0.0)),
        black_swan_sigma=float(risk_cfg.get("black_swan_sigma", 0.0)),
        black_swan_window_s=float(risk_cfg.get("black_swan_window_s", 30.0)),
        black_swan_sigma_window_s=float(risk_cfg.get("black_swan_sigma_window_s", 300.0)),
        black_swan_cooldown_s=float(risk_cfg.get("black_swan_cooldown_s", 180.0)),
        black_swan_min_ref_sigma_bps=float(risk_cfg.get("black_swan_min_ref_sigma_bps", 5.0)),
        black_swan_warmup_s=float(risk_cfg.get("black_swan_warmup_s", 30.0)),
        slippage_cap_bps=float(exec_cfg.get("slippage_cap_bps", 30.0)),
        book_depth_frac_cap=float(exec_cfg.get("book_depth_frac_cap", 0.25)),
        half_spread_bps=float(exec_cfg.get("half_spread_bps", 0.5)),
    )
    auditor = ForecastAuditor()
    proj_cfg = cfg.get("projector", {}) or {}
    projector = ChessProjector(
        tribes=tribes,
        steps=int(cfg["clock"]["projector_steps"]),
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        bps_per_unit_pressure=float(proj_cfg["bps_per_unit_pressure"]),
        confidence_decay=float(proj_cfg["confidence_decay"]),
        auditor=auditor,
        mean_reversion_rate=float(proj_cfg.get("mean_reversion_rate", 0.0)),
        sample_paths_n=int(proj_cfg.get("sample_paths_n", 0)),
        default_horizon_seconds=float(proj_cfg.get("default_horizon_seconds", 600.0)),
        default_sigma_bps=float(proj_cfg.get("default_sigma_bps", 8.0)),
    )
    decision = DecisionEngine(cfg)
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
        fast_halflife_resolved=int(cal_cfg.get("fast_halflife_resolved", 50)),
        fast_min_samples=int(cal_cfg.get("fast_min_samples", 10)),
        regime_shift_threshold=float(cal_cfg.get("regime_shift_threshold", 0.12)),
        enable_regime_conditional=bool(cal_cfg.get("enable_regime_conditional", False)),
        regime_min_samples=int(cal_cfg.get("regime_min_samples", 15)),
    )
    # Phase A — projector gain calibration. Feeds on the same resolved
    # anchors the tribe calibrator consumes; nudges `bps_per_unit_pressure`
    # toward the median realized/predicted ratio. Safe by default
    # (clamped, EWMA-smoothed, refuses to move until min_samples seen).
    kc_cfg = cfg.get("projector_calibration", {}) or {}
    kernel_calibrator = KernelCalibrator(
        initial_gain=float(cfg["projector"]["bps_per_unit_pressure"]),
        halflife_resolved=int(kc_cfg.get("halflife_resolved", 300)),
        min_samples=int(kc_cfg.get("min_samples", 50)),
        window=int(kc_cfg.get("window", 2000)),
        apply=bool(kc_cfg.get("apply", True)),
    )
    # Push any resumed gain onto the projector before the first tick.
    projector.bps_per_unit_pressure = kernel_calibrator.current_gain()
    # Thread the skill calibrator into the projector so forecasts weight
    # each tribe by its measured accuracy (chronically-wrong tribes get
    # damped, consistently-right ones speak louder).
    projector.calibrator = calibrator

    # Phase E — regime overlay. Thresholds and per-label multipliers live
    # in settings.regime; defaults are already calibrated for BTC perps.
    regime_classifier = RegimeClassifier(cfg.get("regime", {}) or {})

    engine = TradingEngine(
        bus=bus, tribes=tribes, state=state,
        projector=projector, decision=decision,
        executor=executor, ledger=ledger,
        tick_seconds=int(cfg["clock"]["tick_seconds"]),
        broadcaster=broadcaster,
        auditor=auditor,
        calibrator=calibrator,
        kernel_calibrator=kernel_calibrator,
        regime_classifier=regime_classifier,
    )

    client = BinancePerpsWS(bus, streams=ds["streams"])
    ui_cfg = {
        "size_min_pct": float(cfg["sizing"]["min_pct_equity"]),
        "size_max_pct": float(cfg["sizing"]["max_pct_equity"]),
        "sizing_curve": str(cfg["sizing"]["curve"]),
        "leverage": float(cfg.get("leverage", 1.0)),
        "max_concurrent_positions": int(cfg["risk"]["max_concurrent_positions"]),
        "max_total_notional_pct": float(cfg["risk"]["max_total_notional_pct"]),
        "exit_dead_zone_bps": float(cfg["decision"].get("exit_dead_zone_bps", 0.5)),
        "bps_per_unit_pressure": float(cfg["projector"]["bps_per_unit_pressure"]),
        "projector_steps": int(cfg["clock"]["projector_steps"]),
        "tick_seconds": int(cfg["clock"]["tick_seconds"]),
    }
    app = create_app(broadcaster, _tribe_meta(tribes), ui_cfg,
                     auditor=auditor, calibrator=calibrator,
                     kernel_calibrator=kernel_calibrator,
                     regime_classifier=regime_classifier, state=state,
                     watch_controller=watch_controller,
                     executor=executor)

    host = cfg.get("ui", {}).get("host", "127.0.0.1")
    port = int(cfg.get("ui", {}).get("port", 8765))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    # Tasks to gather. Core trading loop + exchange stream always on.
    coros = [client.run(), engine.run(), server.serve()]

    # Phase D — event tape recorder for tournament replay. Subscribes to
    # the bus and writes every event to data/events/YYYY-MM-DD.jsonl so
    # tomorrow's tournament can score variants on the same session. Depth
    # events are the bulk of the feed — off by default to keep disk sane;
    # re-enable via settings.recorder.include_depth for full fidelity.
    rec_cfg = cfg.get("recorder", {}) or {}
    skip_kinds = set() if rec_cfg.get("include_depth") else {"book"}
    coros.append(EventRecorder(bus, skip_kinds=skip_kinds).run())

    # Phase F — multi-venue open interest (Binance/Bybit/OKX public REST).
    oi_cfg = sources_cfg.get("multi_venue_oi", {}) or {}
    if oi_cfg.get("enabled"):
        coros.append(OIPoller(
            bus,
            symbol=cfg.get("symbol", "BTCUSDT"),
            poll_seconds=int(oi_cfg.get("poll_seconds", 60)),
        ).run())

    # Phase F — cross-venue public trade streams. Events land as
    # kind="cross_trade" so the execution venue's own flow stays isolated.
    # Higher-timeframe trend poller (1h by default). Feeds the counter-trend
    # shrink in DecisionEngine so the 10-min council doesn't stack shorts
    # into a ripping 4h uptrend. Cheap — one kline pull per poll_seconds.
    htf_cfg = cfg.get("htf", {}) or {}
    if htf_cfg.get("enabled", True):
        coros.append(HTFTrendPoller(
            bus,
            symbol=cfg.get("symbol", "BTCUSDT"),
            timeframe=str(htf_cfg.get("timeframe", "1h")),
            lookback_candles=int(htf_cfg.get("lookback_candles", 50)),
            fast_ema=int(htf_cfg.get("fast_ema", 8)),
            slow_ema=int(htf_cfg.get("slow_ema", 21)),
            poll_seconds=int(htf_cfg.get("poll_seconds", 60)),
        ).run())

    if sources_cfg.get("bybit_ws", {}).get("enabled"):
        coros.append(BybitPublicWS(bus, symbol=cfg.get("symbol", "BTCUSDT")).run())
    if sources_cfg.get("okx_ws", {}).get("enabled"):
        coros.append(OKXPublicWS(bus).run())

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
