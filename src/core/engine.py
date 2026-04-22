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
from ..audit.backscore import ForecastAuditor
from ..audit.calibrator import TribeCalibrator
from msgspec.structs import replace
from ..core.bus import EventBus
from ..core.types import Event, Side
from ..decision.engine import DecisionEngine
from ..execution.base import Executor
from ..execution.ledger import Ledger
from ..execution.shadow import TribeShadowLedger
from ..projector.chess import ChessProjector
from ..projector.kernel_calibrator import KernelCalibrator
from ..regime.classifier import RegimeClassifier
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
        auditor: ForecastAuditor | None = None,
        calibrator: TribeCalibrator | None = None,
        kernel_calibrator: KernelCalibrator | None = None,
        regime_classifier: RegimeClassifier | None = None,
        shadow_ledgers: dict[str, TribeShadowLedger] | None = None,
        tribe_scorer=None,
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
        self.auditor = auditor
        self.calibrator = calibrator
        self.kernel_calibrator = kernel_calibrator
        self.regime_classifier = regime_classifier
        self.shadow_ledgers = shadow_ledgers or {}
        self.tribe_scorer = tribe_scorer
        self._tick_count = 0
        self._starting_equity: float = executor.equity_usd
        # Trade-stall warning state. When aggTrade has been silent for more
        # than `_trade_stall_warn_s`, we log once so the operator knows the
        # chart is being kept live by mark-price fallback, not genuine silence.
        self._trade_stall_warn_s: float = 30.0
        self._trade_stall_warned: bool = False
        # Prediction anchors. Each is (label, step_index) where step_index is
        # 1-based position into the projector's per-step forecast (step=1 →
        # +tick_seconds into the future). Used by the UI to stamp a colored
        # dot on the price chart at every tick — gives an auditable trail of
        # what the council expected vs what happened.
        # Horizons all fit within projector_steps × tick_seconds (≤ 10min at
        # defaults). Anchors beyond that were previously silently dropped.
        self._anchor_specs = [
            ("15s",  1),
            ("1m",   max(1, 60  // max(tick_seconds, 1))),
            ("5m",   max(1, 300 // max(tick_seconds, 1))),
            ("10m",  max(1, 600 // max(tick_seconds, 1))),
        ]

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
            # Phase F — cross-venue trade volume is comparable to Binance's,
            # and `oi` ticks every 60s — both would swamp the UI feed. Drop
            # cross_trade entirely (only useful as an aggregate signal) and
            # keep oi gated behind a moderate severity (we emit 0, so never).
            if ev.kind == "cross_trade":
                continue
            if ev.kind == "oi":
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

    def _compute_predictions(self, forecast) -> list[dict]:
        """Project the current mark forward using the forecast's per-step
        pressures and return one record per anchor horizon. Matches the UI's
        own overlay math so the audit trail and the live chart agree."""
        mark = self.state.last_price
        if mark is None or mark <= 0 or not forecast.steps:
            return []
        price = mark
        # Prefer the projector's own cumulative curve when present — it
        # already includes regime scaling, per-tribe horizon decay, and
        # mean-reversion, so anchors on the chart match what the projector
        # actually expected. Fall back to the prior scalar reconstruction
        # when the forecast doesn't carry the enriched field.
        if forecast.steps_bps:
            cumulative_by_step = list(forecast.steps_bps)
        else:
            bps_per_unit = float(getattr(self.projector, "bps_per_unit_pressure", 0.0))
            bps_per_unit *= float(getattr(self.projector, "regime_multiplier", 1.0))
            cumulative_by_step = []
            cum_bps = 0.0
            for s in forecast.steps:
                cum_bps += float(s.net_pressure) * bps_per_unit
                cumulative_by_step.append(cum_bps)
        out: list[dict] = []
        for label, step_idx in self._anchor_specs:
            if step_idx < 1 or step_idx > len(cumulative_by_step):
                continue
            bps_at = cumulative_by_step[step_idx - 1]
            predicted = price * (1.0 + bps_at / 10_000.0)
            out.append({
                "label": label,
                "horizon_steps": step_idx,
                "horizon_seconds": step_idx * self.tick_seconds,
                "predicted_price": predicted,
                "expected_move_bps": bps_at,
            })
        return out

    async def _on_tick(self) -> None:
        ts = Event.now_utc()
        if self.state.last_price is None:
            logger.info("[tick {:>4}] waiting for market data...", self._tick_count)
            return

        # aggTrade stall telemetry. Binance prints hundreds of trades/sec on
        # BTC; a >30s silence is almost always a WS hiccup. Mark-price fallback
        # in MarketState keeps last_price live, but the operator deserves a
        # heads-up so "why is my chart flat?" has a visible answer in the log.
        last_trade_ts = getattr(self.state, "_last_trade_ts", None)
        if last_trade_ts is not None:
            gap_s = ts.timestamp() - last_trade_ts
            if gap_s > self._trade_stall_warn_s:
                if not self._trade_stall_warned:
                    logger.warning(
                        "[tick {:>4}] aggTrade silent for {:.0f}s — chart is on "
                        "mark-price fallback; check upstream WS health",
                        self._tick_count, gap_s,
                    )
                    self._trade_stall_warned = True
            elif self._trade_stall_warned and gap_s < 2.0:
                logger.info("[tick {:>4}] aggTrade resumed", self._tick_count)
                self._trade_stall_warned = False

        # 1) Collect votes + forecast first — exits are council-driven so
        #    we need the forecast before deciding what to close.
        raw_votes = [t.vote(ts, self.state) for t in self.tribes]

        # Regime must be classified BEFORE skill-multiplier and record_votes
        # so the calibrator can key its per-regime EWMA on the label that
        # applied at vote time (not at resolution time — the regime at
        # resolution is a different concept, and what we want to score is
        # "this tribe voted correctly given the regime it was voting IN").
        regime_snapshot: dict | None = None
        regime_label_now: str | None = None
        if self.regime_classifier is not None:
            regime = self.regime_classifier.classify(self.state)
            self.projector.regime_multiplier = regime.multiplier
            regime_snapshot = regime.to_dict()
            regime_label_now = regime_snapshot.get("label")
            self.projector.regime_label = regime_label_now or "normal"

        # Skill overlay: scale each tribe's conviction by its measured
        # skill multiplier before aggregation. Multiplier is 1.0 when the
        # overlay is disabled or the tribe has too few resolved samples,
        # so this is a no-op until there's real data and `apply=True`.
        # Trading overlay: side-aware multiplier from the tribe scorer's
        # shadow-ledger EWMA. Stacks on top of the signal-skill mult so a
        # tribe that predicts direction but bleeds in execution gets muted
        # even when its signal is statistically sound, and vice versa.
        if self.calibrator is not None:
            votes = []
            for v in raw_votes:
                m_skill = self.calibrator.skill_multiplier(v.tribe_id, regime_label_now)
                m_trade = self.calibrator.trading_multiplier(v.tribe_id, v.side)
                m = m_skill * m_trade
                if m != 1.0 and v.side is not Side.ABSTAIN:
                    new_conv = max(0.0, min(1.0, v.conviction * m))
                    # Recompute internal distribution fractions so they stay
                    # consistent with the scaled conviction. act ≈ buy+sell.
                    act_est = v.buy_frac + v.sell_frac
                    if act_est > 1e-9:
                        raw_new = max(0.0, min(1.0, new_conv / act_est))
                        if v.side is Side.BUY:
                            nb = act_est * (0.5 + raw_new / 2.0)
                            ns = act_est * max(0.0, 0.5 - raw_new / 2.0)
                        else:
                            ns = act_est * (0.5 + raw_new / 2.0)
                            nb = act_est * max(0.0, 0.5 - raw_new / 2.0)
                    else:
                        nb, ns = v.buy_frac, v.sell_frac
                    note = f" | skill×{m_skill:.2f} trading_m={m_trade:.2f}"
                    votes.append(replace(v, conviction=new_conv,
                                         buy_frac=nb, sell_frac=ns,
                                         reason=v.reason + note))
                else:
                    votes.append(v)
            # Record the RAW vote (pre-multiplier) as the skill signal —
            # the multiplier is the policy, the raw vote is the data.
            # Regime is stamped so the per-regime EWMA can update.
            self.calibrator.record_votes(
                ts.timestamp(), raw_votes, regime_label=regime_label_now,
            )
        else:
            votes = raw_votes
        agg = aggregate_votes(votes, ts)

        forecast = self.projector.project(self.state)

        # 2a) Funding accrual — deduct before exits so the equity used for
        #     capacity checks is already net of funding costs this tick.
        if hasattr(self.executor, "apply_funding"):
            self.executor.apply_funding(
                self.state.last_funding_rate or 0.0,
                float(self.tick_seconds),
            )

        # WS1 §8 — stash current regime on the executor so every close path
        # (black-swan, halt, flip, max-hold, manual) stamps it on the ledger.
        if hasattr(self.executor, "set_regime_label"):
            self.executor.set_regime_label(regime_label_now)

        # 2b) Phase 10 — black-swan detonation stop. Runs BEFORE forecast-
        #     driven exits because a 5σ move can outpace the projector's
        #     reaction; if this fires, every position is already closed by
        #     the time evaluate_exits sees the book.
        if hasattr(self.executor, "check_black_swan"):
            self.executor.check_black_swan(ts, self.state)

        # 2b.5) Phase 10-Harden — drain latency-spiked pending fills. An
        #       intent decided 5 s ago with a Pareto-drawn 4.5 s latency
        #       should land *this* tick at today's (moved) mark, then face
        #       the same exit logic every other position does. Keeping the
        #       drain before evaluate_exits lets the newly-opened position
        #       participate in this tick's exit evaluation (it won't fire
        #       on a 0-held-seconds position — all layers gate on held_s
        #       or on the flip-streak — but the invariant is clean).
        if hasattr(self.executor, "tick"):
            self.executor.tick(ts, self.state)

        # 2c) Council-driven exits: close positions whose direction no longer
        #     matches the current forecast. Must happen BEFORE the entry
        #     decision so equity/notional caps reflect this tick's reality.
        regime_label = regime_snapshot["label"] if regime_snapshot else None
        self.executor.evaluate_exits(
            ts, self.state.last_price, forecast, regime_label=regime_label,
        )

        # 3) Max-hold backstop (time-based hard cap, independent of forecast).
        self.executor.mark_to_market(ts, self.state.last_price)

        # 3b) Per-tribe shadow ledgers — each archetype runs its own isolated
        #     paper account using its own vote for entries and the council
        #     forecast for the same 6-layer exit engine. Black-swan state is
        #     borrowed from the real executor so shadows freeze in detonations.
        if self.shadow_ledgers:
            bs_active = bool(getattr(self.executor, "black_swan_active", False))
            vote_by_tid = {v.tribe_id: v for v in raw_votes}
            for tid, ledger in self.shadow_ledgers.items():
                vote = vote_by_tid.get(tid)
                if vote is None:
                    continue
                tribe = next((t for t in self.tribes if t.spec.id == tid), None)
                tribe_rng = getattr(tribe, "_rng", None) if tribe else None
                ledger.on_tick(
                    ts, self.state.last_price, vote, forecast,
                    regime_label=regime_label_now,
                    rng=tribe_rng,
                    black_swan_active=bs_active,
                )
            if self.tribe_scorer is not None:
                for ledger in self.shadow_ledgers.values():
                    closed = ledger.drain_pending()
                    if closed:
                        self.tribe_scorer.ingest_closed(closed)
                    self.tribe_scorer.ingest_mark(
                        ledger.tribe_id,
                        ledger.equity_usd,
                        ledger.unrealized_pnl_bps(self.state.last_price),
                        ledger.peak_equity_usd,
                    )

        intent = self.decision.decide(
            ts, forecast, self.state, equity_usd=self.executor.equity_usd,
            regime_label=regime_label,
        )
        open_n = len(self.executor.positions)
        net_pos = sum(
            (1 if p.side.value == "buy" else -1) * p.size_usd
            for p in self.executor.positions
        )
        regime_tag = regime_snapshot["label"] if regime_snapshot else "-"
        logger.info(
            "[tick {:>4}] px={:.2f} net={:+.3f} unc={:.2f} emove={:+.1f}bps conf={:.2f} "
            "reg={} open={} net_exp=${:+.0f} eq=${:.2f} | {} — {}",
            self._tick_count, self.state.last_price, agg.net_pressure, agg.uncertainty,
            forecast.expected_move_bps, forecast.confidence,
            regime_tag,
            open_n, net_pos, self.executor.equity_usd,
            intent.action, intent.reason,
        )

        # Prediction anchors — pin the projector's price expectation at a
        # handful of horizons. The UI turns each into a colored dot at
        # (tick + horizon_steps) on the chart, so the audit scheduler can
        # later replay: where did we think price would be in 5 min? where
        # did it actually go?
        predictions = self._compute_predictions(forecast)

        # Forecast backscoring: resolve any anchors that matured at this
        # tick (target_ts ≤ now) against the realized price, then enqueue
        # the fresh anchors we just computed. The auditor persists each
        # resolved record to data/forecasts/YYYY-MM-DD.jsonl and keeps a
        # trailing window in memory for the UI.
        backscore_snapshot: dict | None = None
        calibrator_snapshot: dict | None = None
        kernel_snapshot: dict | None = None
        if self.auditor is not None:
            resolved_now = self.auditor.record_tick(
                ts, self.state.last_price, predictions,
                forecast_conf=forecast.confidence,
                forecast_emove_bps=forecast.expected_move_bps,
                net_pressure=agg.net_pressure,
            )
            backscore_snapshot = self.auditor.snapshot()
            # Phase G loop: feed freshly resolved anchors to the skill
            # calibrator so each tribe's EWMA moves toward the truth.
            if self.calibrator is not None and resolved_now:
                self.calibrator.apply_resolved(resolved_now)
            # Phase A loop: the same resolved anchors fit the projector's
            # gain. Push the refreshed value straight onto the projector so
            # the NEXT tick's forecast uses it.
            if self.kernel_calibrator is not None and resolved_now:
                self.kernel_calibrator.apply_resolved(resolved_now)
                self.projector.bps_per_unit_pressure = (
                    self.kernel_calibrator.current_gain()
                )
        if self.calibrator is not None and self._tick_count % 20 == 0:
            calibrator_snapshot = self.calibrator.snapshot()
        if self.kernel_calibrator is not None and self._tick_count % 20 == 0:
            kernel_snapshot = self.kernel_calibrator.snapshot()

        # Tick record for daily audit
        self.ledger.write({
            "event": "tick", "ts": ts, "price": self.state.last_price,
            "funding_rate": self.state.last_funding_rate,
            "net_pressure": agg.net_pressure, "uncertainty": agg.uncertainty,
            "expected_move_bps": forecast.expected_move_bps, "confidence": forecast.confidence,
            "predictions": predictions,
            "votes": [
                {"tribe_id": v.tribe_id, "side": v.side.value,
                 "conviction": v.conviction, "weight": v.weight, "reason": v.reason}
                for v in votes
            ],
        })

        if intent.action != "hold":
            self.executor.execute(intent, self.state)

        # Session fitness — equity is THE quality signal. Print a running
        # line every 20 ticks (≈5min) so it's visible without opening the UI.
        if self._tick_count % 20 == 0:
            delta = self.executor.equity_usd - self._starting_equity
            pct = 0.0 if self._starting_equity == 0 else delta / self._starting_equity
            logger.info("[fitness] tick={} Δequity=${:+.2f} ({:+.2%})  trades={}",
                        self._tick_count, delta, pct,
                        getattr(self.executor, "trades_completed", 0))
            for tid, ledger in self.shadow_ledgers.items():
                logger.info(
                    "[SHADOW] tribe={} eq=${:,.2f} pnl={:+.1f}bps trades={} open={}",
                    tid, ledger.equity_usd, ledger.pnl_bps_vs_start(),
                    ledger.trades_completed, len(ledger.positions),
                )

        if self.broadcaster is not None:
            positions = self.executor.positions
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
                "predictions": predictions,
                "forecast_steps": [
                    {"net_pressure": s.net_pressure, "uncertainty": s.uncertainty}
                    for s in forecast.steps
                ],
                # Wiggly-path enrichments. `forecast_steps_bps` is the
                # projector's own cumulative-drift curve (authoritative);
                # `forecast_sigma_bps` is the per-step stddev for the
                # uncertainty fan; `forecast_sample_paths_bps` is a bundle
                # of Monte Carlo realizations for the UI to render behind
                # the expected curve.
                "forecast_steps_bps": forecast.steps_bps,
                "forecast_sigma_bps": forecast.steps_sigma_bps,
                "forecast_sample_paths_bps": forecast.sample_paths_bps,
                "forecast_sample_paths_tribe_ids": forecast.sample_paths_tribe_ids,
                # Per-(tribe, horizon) performance weights — the Adaptive
                # Temporal Weighting matrix the projector ledger is learning.
                # Shape: { tribe_id: [w_h0, w_h1, ..., w_hN] }. Values are
                # bounded in [0.1, 3.0] with 1.0 meaning "neutral". UI uses
                # this to render a 17 × N heatmap so the user can watch each
                # tribe's horizon-specialist profile emerge over time.
                "projector_weights": dict(getattr(self.projector, "_weights", {}) or {}),
                # Phase 15 — council intelligence surface. `diversity_score` is
                # the Shannon entropy of dominant-side mass across orthogonal
                # archetype categories (1.0 = ideally spread, 0 = one bloc).
                # `category_mass` is the per-category buy/sell mass breakdown
                # (lets the UI show who's behind a high-conf call — retail
                # bloc vs a true cross-category consensus). `avg_voter_horizon_s`
                # is the weighted-average holding horizon of the voters.
                "diversity_score": forecast.diversity_score,
                "category_mass": forecast.category_mass,
                "avg_voter_horizon_s": forecast.avg_voter_horizon_s,
                # Task 4c — Path-Integrity driver score. Pool-weight-weighted
                # calibrator skill of tribes voting with the consensus. < 0.7
                # caps all size multipliers in the decision engine.
                "driver_skill_avg": forecast.driver_skill_avg,
                # Task 4e — Harvest Mass. Share of pool weight sitting in
                # contrarian-flipped tribes, and the resulting conviction
                # boost applied to the cumulative path (1.0 = no boost).
                "harvest_mass_ratio": forecast.harvest_mass_ratio,
                "conviction_boost": forecast.conviction_boost,
                "votes": [
                    {"tribe_id": v.tribe_id, "side": v.side.value,
                     "conviction": v.conviction, "weight": v.weight, "reason": v.reason}
                    for v in votes
                ],
                # Portfolio snapshot — every open trade with its brackets, so
                # the UI can draw a list and per-trade distance-to-TP/SL.
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
                "starting_equity_usd": self._starting_equity,
                "trades": getattr(self.executor, "trades_completed", 0),
                "wins": getattr(self.executor, "wins", 0),
                "longs_opened": getattr(self.executor, "longs_opened", 0),
                "shorts_opened": getattr(self.executor, "shorts_opened", 0),
                "capitulation_triggers": getattr(self.projector, "_last_capitulation_triggers", 0),
                "recent_trades": list(getattr(self.executor, "recent_trades", [])),
                "action": intent.action,
                "reason": intent.reason,
                "backscore": backscore_snapshot,
                "calibration": calibrator_snapshot,
                "kernel": kernel_snapshot,
                "regime": regime_snapshot,
                "risk": (self.executor.risk_snapshot()
                         if hasattr(self.executor, "risk_snapshot") else None),
                # Per-tribe shadow ledger equity snapshot — one row per tribe
                # with equity, pnl_bps, trades, and open-position count. Feeds
                # the Tribe Trading Heatmap in the UI.
                "shadow_ledgers": (
                    [ledger.snapshot() for ledger in self.shadow_ledgers.values()]
                    if self.shadow_ledgers else None
                ),
                # Tribe scorer composite metrics (EWMA long/short skill,
                # realized/unrealized PnL, Sharpe, SL-discipline, etc.).
                "tribe_trading": (
                    self.tribe_scorer.snapshot()
                    if self.tribe_scorer is not None else None
                ),
            })
