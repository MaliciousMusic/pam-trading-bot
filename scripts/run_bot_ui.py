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
from src.audit.tribe_scorer import TribeTradingScorer
from src.execution.shadow import build_shadow_ledgers
from src.core.bus import EventBus
from src.core.config import PROJECT_ROOT, data_sources, settings
from src.core.engine import TradingEngine
from src.decision.engine import DecisionEngine
from src.execution.ledger import Ledger
from src.execution.harden import HardenConfig
from src.execution.paper import PaperExecutor
from src.ingress.binance_ws import BinancePerpsWS
from src.ingress.bybit_perps_ws import BybitPerpsWS
from src.ingress.bybit_ws import BybitPublicWS
from src.ingress.deribit_options import DeribitOptionsPoller
from src.ingress.event_recorder import EventRecorder
from src.ingress.fgi import FGIPoller
from src.ingress.htf_trend_poller import HTFTrendPoller
from src.ingress.oi_poller import OIPoller
from src.ingress.okx_ws import OKXPublicWS
from src.ingress.reddit_rss import RedditRSSPoller
from src.ingress.rss_feeds import CURATED_FEEDS, RSSFeedPoller
from src.ingress.stablecoin_supply import StablecoinSupplyPoller
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
    # Primary venue selector. 2026-04-24 venue swap (PLAN.md §5.1.2):
    # Binance USD-M futures geo-blocked; Bybit V5 linear is the replacement.
    venue = str(cfg.get("exchange", "binance_perps"))

    # Wire the logging block so the yaml actually controls loguru verbosity
    # and sink directory. Prior to this, settings.logging was decorative.
    log_cfg = cfg.get("logging", {}) or {}
    log_level = str(log_cfg.get("level", "INFO")).upper()
    log_dir = log_cfg.get("dir")
    if log_level:
        import sys as _sys
        logger.remove()
        logger.add(_sys.stderr, level=log_level)
        if log_dir:
            from pathlib import Path as _Path
            _Path(log_dir).mkdir(parents=True, exist_ok=True)
            logger.add(f"{log_dir}/bot_{{time:YYYY-MM-DD}}.log",
                       level=log_level, rotation="1 day", retention="14 days")

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
        # Phase 10-Harden — Merciless overlay. `harden:` yaml block is
        # optional; missing → default HardenConfig (enabled=False, bit-
        # for-bit identical to pre-Harden). Tick cadence is plumbed so
        # the latency layer knows when to defer vs. absorb.
        harden=HardenConfig.from_yaml(cfg.get("harden", {}) or {}),
        tick_seconds=float(cfg["clock"].get("tick_seconds", 1)),
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
    # Phase 2.5 — time-based halflives. When `_s` keys are present in the
    # yaml they take precedence over the legacy count-based keys. Pass
    # both through so the calibrator can pick its mode.
    _hl_s = cal_cfg.get("halflife_resolved_s")
    _hl_fast_s = cal_cfg.get("fast_halflife_resolved_s")
    calibrator = TribeCalibrator(
        halflife_resolved=int(cal_cfg.get("halflife_resolved", 500)),
        halflife_resolved_s=(float(_hl_s) if _hl_s is not None else None),
        min_samples=int(cal_cfg.get("min_samples", 30)),
        max_shrink=float(cal_cfg.get("max_shrink", 0.6)),
        max_boost=float(cal_cfg.get("max_boost", 1.4)),
        apply=bool(cal_cfg.get("apply", False)),
        fast_halflife_resolved=int(cal_cfg.get("fast_halflife_resolved", 50)),
        fast_halflife_resolved_s=(float(_hl_fast_s) if _hl_fast_s is not None else None),
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
    _kc_hl_s = kc_cfg.get("halflife_resolved_s")
    kernel_calibrator = KernelCalibrator(
        initial_gain=float(cfg["projector"]["bps_per_unit_pressure"]),
        halflife_resolved=int(kc_cfg.get("halflife_resolved", 300)),
        halflife_resolved_s=(float(_kc_hl_s) if _kc_hl_s is not None else None),
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

    # Per-tribe shadow ledgers + trading scorer. Each archetype gets its own
    # 50k paper account and the scorer folds realized PnL into side-aware
    # EWMAs. The calibrator gets the scorer attached so votes pick up the
    # trading_multiplier automatically each tick.
    sh_cfg = cfg.get("shadow_ledgers", {}) or {}
    shadow_ledgers = build_shadow_ledgers(
        tribes, cfg,
        exit_strategy=cfg.get("exit_strategy", {}) or {},
        risk_cfg=cfg.get("risk", {}) or {},
        decision_cfg=cfg.get("decision", {}) or {},
    )
    tribe_scorer = None
    if shadow_ledgers:
        tribe_scorer = TribeTradingScorer(
            halflife_trades=int(sh_cfg.get("halflife_trades", 300)),
            min_trades_for_multiplier=int(sh_cfg.get("min_trades_for_multiplier", 30)),
            trading_multiplier_max_boost=float(
                sh_cfg.get("trading_multiplier_max_boost", 1.4)),
            trading_multiplier_max_shrink=float(
                sh_cfg.get("trading_multiplier_max_shrink", 0.6)),
        )
        for tid, ledger_sh in shadow_ledgers.items():
            tribe_scorer.seed_start_equity(tid, ledger_sh.starting_equity_usd)
        calibrator.attach_trading_scorer(tribe_scorer)
        # Feed the top-quartile harvest boost through to the projector.
        projector._harvest_top_quartile_boost = float(
            sh_cfg.get("harvest_top_quartile_boost", 2.0))

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
        shadow_ledgers=shadow_ledgers,
        tribe_scorer=tribe_scorer,
    )

    if venue == "bybit_perps":
        client = BybitPerpsWS(bus, symbol=cfg.get("symbol", "BTCUSDT"))
    else:
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
                     executor=executor,
                     tribe_scorer=tribe_scorer,
                     shadow_ledgers=shadow_ledgers)

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

    # Phase G — Deribit BTC options aggregate (OI, P/C ratio, OI-weighted IV).
    deribit_cfg = sources_cfg.get("deribit_options", {}) or {}
    if deribit_cfg.get("enabled"):
        coros.append(DeribitOptionsPoller(
            bus,
            currency=str(deribit_cfg.get("currency", "BTC")),
            poll_seconds=int(deribit_cfg.get("poll_seconds", 120)),
            retry_seconds=int(deribit_cfg.get("retry_seconds", 60)),
        ).run())

    # Phase G — stablecoin supply proxy (USDT+USDC circulating supply from
    # CoinGecko free tier). Change over rolling window = bridge-flow signal.
    stablecoin_cfg = sources_cfg.get("stablecoin_supply", {}) or {}
    if stablecoin_cfg.get("enabled"):
        coros.append(StablecoinSupplyPoller(
            bus,
            poll_seconds=int(stablecoin_cfg.get("poll_seconds", 1800)),
            retry_seconds=int(stablecoin_cfg.get("retry_seconds", 300)),
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

    # When Bybit is the primary venue, skip the cross-venue Bybit feed —
    # we're already subscribed to every BTCUSDT stream we need from the
    # primary client, and BybitPublicWS emits kind="cross_trade" which
    # would double-count same-venue trades in the aggregator's cross-venue
    # signal. Binance-primary keeps the cross-venue Bybit feed on.
    if sources_cfg.get("bybit_ws", {}).get("enabled") and venue != "bybit_perps":
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

    logger.info("mode={} venue={} symbol={} equity=${:.2f}  UI at http://{}:{}",
                cfg["mode"], venue, cfg["symbol"], executor.equity_usd, host, port)
    logger.info("[boot] {} background tasks: exchange WS, trading engine, UI server, "
                "audit scheduler + {} sentiment feed(s)",
                len(coros), len(coros) - 4)
    await asyncio.gather(*coros)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
