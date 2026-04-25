"""Per-tribe shadow ledgers — isolated 50k paper accounts per archetype.

Every council tribe gets its own ledger. The tribe's Vote drives entries
(scaled by the tribe's personality), and the resulting Position is exited
through the same six-layer exit engine PaperExecutor uses against the
council forecast — but evaluated against the tribe's own standalone vote.

The real council portfolio is untouched: these are parallel audit books
that measure each archetype's trading skill in isolation so a downstream
calibrator can learn 'liquidation_hunter actually makes money, TA day
trader bleeds' without the noise of council-aggregation masking who
contributed what.

Exit parity with PaperExecutor:
  Layer 0 — hard SL floor (%-of-equity, personality-scaled by `panic`)
  Layer 1 — sign flip beyond regime-scaled dead zone + weak-flip confirm
  Layer 2 — profit ratchet (arm + retrace)
  Layer 2b — forecast-target TP
  Layer 2c — shape-aware exit (past peak-favorable + grace)
  Layer 3 — edge exhausted (remaining < fees)
  + session DD halt, per-position hard SL, max-hold backstop.
Black-swan halt is inherited from the real executor's state snapshot so
shadows respect the same detonation freeze.

Personality → trading knobs:
  panic ∈ [0, 1]            → SL floor: 1.5% (calm) → 0.5% (panicky)
  activation_rate ∈ (0,1]   → open probability this tick
  strategy.family           → horizon bias (microstructure short, whale long)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from ..core.types import Forecast, Position, Side, Vote
from ..tribes.tribe import Tribe


# Strategy families whose shadow horizon lives in the 30–120s band. These
# tribes chase microstructure moves (MM, arb, microstructure_prober, MEV);
# scoring them on 5–10min outcomes punishes their actual alpha surface.
_SHORT_HORIZON_FAMILIES: frozenset[str] = frozenset({
    "mean_reverter_mm", "grid_bot",
    "basis_arb", "cross_venue_arb",
    "microstructure_prober", "mev_frontrun",
})
# Macro tribes hold through the whole projector window and longer — scoring
# them on 60s outcomes is just noise.
_LONG_HORIZON_FAMILIES: frozenset[str] = frozenset({
    "passive_whale_twap", "options_hedger_proxy", "leverage_cycle",
    "swing_trend_follower", "news_reactor",
})


@dataclass(slots=True)
class _ShadowTrade:
    """Audit record handed to TribeTradingScorer after each close."""
    tribe_id: str
    side: Side
    size_usd: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl_usd: float
    fee_usd: float
    net_usd: float
    realized_bps: float
    held_s: float
    reason: str
    hit_tp: bool
    hit_sl: bool
    expected_move_bps: float
    # WS3 I3.c — executor-parity measurement surface. Per-close bps the
    # trade would have realized if priced through PaperExecutor's pure
    # fill model (`_fill_price_deterministic`, no Harden overlay) instead
    # of shadow's own simpler `_fill_price`. None when no PaperExecutor
    # reference was wired at shadow-ledger construction time (unit-test
    # fixtures, sprint-without-executor, legacy call sites).
    realized_bps_paper_model: float | None = None


class TribeShadowLedger:
    """Isolated 50k paper account for a single tribe.

    Consumes the tribe's own vote at each tick plus the council's live
    forecast (for exit arithmetic only — entries are driven by the tribe's
    solo view). Tracks its own equity, positions, fees, trade log. Does NOT
    share state with the real PaperExecutor; a blown-up shadow costs the
    council nothing.
    """

    def __init__(
        self,
        tribe: Tribe,
        *,
        starting_equity_usd: float = 50_000.0,
        fee_rate_bps: float = 2.0,
        slippage_bps: float = 2.0,
        half_spread_bps: float = 0.5,
        max_concurrent_positions: int = 5,
        max_total_notional_pct: float = 200.0,
        max_hold_seconds: int = 600,
        max_session_dd_pct: float = 12.0,
        # Exit-engine config (mirrors PaperExecutor's exit_strategy block).
        exit_dead_zone_bps: float = 0.5,
        strong_flip_conf: float = 0.25,
        weak_flip_confirm_ticks: int = 2,
        dead_zone_regime_mult: dict[str, float] | None = None,
        ratchet_arm_bps: float = 0.0,
        ratchet_retrace_frac: float = 1.0,
        forecast_tp_factor: float = 0.0,
        forecast_tp_min_bps: float = 0.0,
        shape_grace_seconds: float = 0.0,
        edge_exhausted_factor: float = 0.0,
        edge_exhausted_min_age_s: float = 20.0,
        # Vote-to-size gearing. A shadow trade risks a fixed fraction of
        # current equity per signal; high-conviction votes take the full
        # fraction, low-conviction ones scale toward zero.
        size_base_pct: float = 2.0,
        size_conv_scale: float = 1.0,
        min_vote_conviction: float = 0.10,
        # WS3 I3.c — optional reference to the real PaperExecutor so that
        # `_close_position` can log a parallel `realized_bps_paper_model`
        # using `PaperExecutor._fill_price_deterministic`. Duck-typed (we
        # only call `._fill_price_deterministic(...)`), so unit-test stubs
        # can pass a minimal object. None → field stays None on every
        # `_ShadowTrade`.
        paper_exec: Any | None = None,
    ) -> None:
        self.tribe = tribe
        self.tribe_id = tribe.spec.id
        self._equity = float(starting_equity_usd)
        self._peak_equity = float(starting_equity_usd)
        self._start_equity = float(starting_equity_usd)
        self._positions: list[Position] = []
        self._next_id = 1

        self.fee_rate_bps = float(fee_rate_bps)
        self.slippage_bps = float(slippage_bps)
        self.half_spread_bps = float(half_spread_bps)
        self.max_concurrent_positions = int(max_concurrent_positions)
        self.max_total_notional_pct = float(max_total_notional_pct)
        self.max_hold_seconds = int(max_hold_seconds)
        self.max_session_dd_pct = float(max_session_dd_pct)
        # WS3 I3.c — paper-model reference for parity measurement.
        self._paper_exec = paper_exec

        # Personality → per-position SL floor. Panicky tribes cut fast, calm
        # tribes ride drawdowns further. Linear interpolation between 1.5%
        # (panic=0) and 0.5% (panic=1), clamped.
        panic = max(0.0, min(1.0, float(tribe.spec.personality.panic)))
        self.max_position_loss_pct = 1.5 - 1.0 * panic

        # Personality → open-probability gate. The flow.activation_rate encodes
        # what fraction of the tribe's members are active each tick; we reuse
        # it directly as the open-chance gate so MM shadows fire every tick,
        # newbie-retail shadows fire rarely. Floor is 5% so even sleepy
        # tribes generate a sample stream within a reasonable run.
        self.open_probability = max(
            0.05, min(1.0, float(tribe.spec.flow.activation_rate)),
        )

        # Strategy family → horizon bias. Clip the default tick max_hold so
        # short-horizon shadows aren't kept open past their natural decay and
        # long-horizon shadows aren't prematurely closed by a 600s global cap.
        fam = tribe.spec.strategy.family
        if fam in _SHORT_HORIZON_FAMILIES:
            self.horizon_seconds = 120
        elif fam in _LONG_HORIZON_FAMILIES:
            self.horizon_seconds = 900
        else:
            self.horizon_seconds = 300

        # Exit-engine config.
        self.exit_dead_zone_bps = float(exit_dead_zone_bps)
        self.strong_flip_conf = float(strong_flip_conf)
        self.weak_flip_confirm_ticks = max(1, int(weak_flip_confirm_ticks))
        self.dead_zone_regime_mult = dict(dead_zone_regime_mult or {})
        self.ratchet_arm_bps = float(ratchet_arm_bps)
        self.ratchet_retrace_frac = float(ratchet_retrace_frac)
        self.forecast_tp_factor = float(forecast_tp_factor)
        self.forecast_tp_min_bps = float(forecast_tp_min_bps)
        self.shape_grace_seconds = float(shape_grace_seconds)
        self.edge_exhausted_factor = float(edge_exhausted_factor)
        self.edge_exhausted_min_age_s = float(edge_exhausted_min_age_s)

        # Sizing gearing.
        self.size_base_pct = float(size_base_pct)
        self.size_conv_scale = float(size_conv_scale)
        self.min_vote_conviction = float(min_vote_conviction)

        # Per-position state for the exit layers.
        self._flip_streak: dict[str, int] = {}
        self._peak_bps: dict[str, float] = {}
        self._intended_mark: dict[str, float] = {}
        self._peak_favorable_s: dict[str, int] = {}
        self._expected_move_bps: dict[str, float] = {}
        self._tp_price: dict[str, float] = {}
        self._sl_price: dict[str, float] = {}

        # Session halt.
        self._session_halted = False
        self._session_halt_reason: str | None = None
        self._session_halt_ts: datetime | None = None

        # Trade log — newest last, bounded.
        self._recent_trades: list[dict[str, Any]] = []
        self._trades_completed = 0
        self._wins = 0
        # Sink consumed by the scorer — every close pushes a _ShadowTrade
        # record; caller drains it each tick to fold into per-tribe EWMAs.
        self._pending: list[_ShadowTrade] = []

    # -- public surface --------------------------------------------------

    @property
    def equity_usd(self) -> float:
        return self._equity

    @property
    def starting_equity_usd(self) -> float:
        return self._start_equity

    @property
    def peak_equity_usd(self) -> float:
        return self._peak_equity

    @property
    def positions(self) -> list[Position]:
        return self._positions

    @property
    def trades_completed(self) -> int:
        return self._trades_completed

    @property
    def wins(self) -> int:
        return self._wins

    @property
    def recent_trades(self) -> list[dict[str, Any]]:
        return self._recent_trades

    @property
    def session_halted(self) -> bool:
        return self._session_halted

    @property
    def session_halt_reason(self) -> str | None:
        return self._session_halt_reason

    def drain_pending(self) -> list[_ShadowTrade]:
        """Hand off accumulated closes to the scorer, clear the sink."""
        out = self._pending
        self._pending = []
        return out

    def pnl_bps_vs_start(self) -> float:
        return (self._equity - self._start_equity) / self._start_equity * 1e4

    def unrealized_pnl_bps(self, mark: float) -> float:
        """Live open-position bps vs starting equity. Sums every open pos."""
        if mark is None or mark <= 0 or self._start_equity <= 0:
            return 0.0
        unreal = 0.0
        for p in self._positions:
            sign = 1.0 if p.side is Side.BUY else -1.0
            unreal += p.size_usd * sign * (mark - p.entry_price) / p.entry_price
        return unreal / self._start_equity * 1e4

    def on_tick(
        self,
        ts: datetime,
        price: float,
        vote: Vote,
        forecast: Forecast,
        regime_label: str | None = None,
        rng: Any | None = None,
        black_swan_active: bool = False,
    ) -> None:
        """One shadow tick: evaluate exits against the council forecast,
        then (conditionally) open a new position on the tribe's solo vote.

        Black-swan halts and session-DD halts short-circuit new opens; they
        do not prevent exits (we still want to book the close PnL)."""
        if price is None or price <= 0:
            return

        # 1) Exits first — mirrors PaperExecutor.evaluate_exits. Session halt
        #    cascade-closes everything at the mark, same semantics.
        self._refresh_session_state(ts)
        if self._session_halted:
            reason = f"session halt: {self._session_halt_reason}"
            for p in list(self._positions):
                self._close_position(p, price, ts, reason, hit_sl=True)
            self._positions = []
        else:
            emove = forecast.expected_move_bps
            conf = float(getattr(forecast, "confidence", 0.0))
            dz = self.exit_dead_zone_bps * float(
                self.dead_zone_regime_mult.get(regime_label or "", 1.0)
            )
            rt_fees_bps = 2.0 * (self.fee_rate_bps + self.slippage_bps)
            survivors: list[Position] = []
            for p in self._positions:
                reason, hit_tp, hit_sl = self._exit_reason(
                    p, price, ts, emove, conf, dz, rt_fees_bps,
                )
                if reason is not None:
                    self._close_position(p, price, ts, reason,
                                         hit_tp=hit_tp, hit_sl=hit_sl)
                else:
                    survivors.append(p)
            self._positions = survivors

        # 2) Max-hold backstop (independent of forecast).
        if self.max_hold_seconds > 0 and self._positions:
            survivors2: list[Position] = []
            for p in self._positions:
                held = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
                if held >= self.max_hold_seconds:
                    self._close_position(p, price, ts,
                                         f"max hold {int(held)}s", hit_sl=False)
                else:
                    survivors2.append(p)
            self._positions = survivors2

        # 3) Entry — gated by session halt, black-swan, activation probability,
        #    and a minimum-conviction floor. Shadow-open size scales linearly
        #    with the vote's conviction × activation-modulated base pct.
        if self._session_halted or black_swan_active:
            return
        if vote.side is Side.ABSTAIN:
            return
        if vote.conviction < self.min_vote_conviction:
            return
        if rng is not None:
            if rng.random() >= self.open_probability:
                return
        # Mild crowd-control so a single tribe doesn't run 20 shadow positions.
        if len(self._positions) >= self.max_concurrent_positions:
            return
        pct = self.size_base_pct * max(0.0, min(1.0,
            vote.conviction * self.size_conv_scale))
        size_usd = self._equity * pct / 100.0
        if size_usd <= 0:
            return
        notional_cap = self._equity * self.max_total_notional_pct / 100.0
        total_open = sum(p.size_usd for p in self._positions)
        if total_open + size_usd > notional_cap:
            return
        self._open_position(vote.side, size_usd, price, ts, forecast)

    # -- internals -------------------------------------------------------

    def _refresh_session_state(self, ts: datetime) -> None:
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        if self.max_session_dd_pct <= 0 or self._session_halted:
            return
        trigger = self._peak_equity * (1.0 - self.max_session_dd_pct / 100.0)
        if self._equity <= trigger:
            self._session_halted = True
            self._session_halt_ts = ts
            self._session_halt_reason = (
                f"shadow session DD ${self._equity:,.2f} ≤ ${trigger:,.2f} "
                f"(peak ${self._peak_equity:,.2f} − "
                f"{self.max_session_dd_pct:.1f}%)"
            )

    def _fill_price(self, mark: float, side: Side, size_usd: float) -> tuple[float, float]:
        """Simplified PaperExecutor._fill_price. Returns (fill, slip_bps)."""
        sign = 1 if side is Side.BUY else -1
        # A single depth-independent slippage scalar matches the replay
        # fallback path; shadow ledgers don't see the live L2 book.
        total_bps = self.half_spread_bps + self.slippage_bps
        return mark * (1 + sign * total_bps / 10_000.0), total_bps

    def _fee(self, notional: float) -> float:
        return notional * self.fee_rate_bps / 10_000.0

    def _open_position(
        self,
        side: Side,
        size_usd: float,
        mark: float,
        ts: datetime,
        forecast: Forecast,
    ) -> None:
        fill, slip_bps = self._fill_price(mark, side, size_usd)
        fee = self._fee(size_usd)
        self._equity -= fee
        pid = f"s-{self.tribe_id}-{self._next_id:04d}"
        self._next_id += 1
        peak_s = self._peak_favorable_seconds(forecast, side)
        # Task 2 — volatility sanity rail. Mirror of PaperExecutor._open:
        # clamp the stamped expected_move_bps at 1.25× the realized 14-min
        # sigma published on the Forecast. Task 1 — stamp conviction_boost
        # so the Layer 2b TP exit can recover the raw council move.
        emove = float(forecast.expected_move_bps)
        rail_bps = getattr(forecast, "realized_vol_bps", None)
        if rail_bps is not None and rail_bps > 0:
            cap = 1.25 * rail_bps
            mag = abs(emove)
            if mag > cap:
                sign = 1.0 if emove >= 0 else -1.0
                emove = sign * cap
        boost = float(getattr(forecast, "conviction_boost", 1.0) or 1.0)
        if boost <= 0:
            boost = 1.0
        pos = Position(
            id=pid,
            side=side,
            size_usd=size_usd,
            entry_price=fill,
            entry_ts=ts,
            tp_price=0.0,
            sl_price=0.0,
            horizon_seconds=self.horizon_seconds,
            expected_move_bps=emove,
            confidence=float(getattr(forecast, "confidence", 0.0)),
            peak_favorable_s=peak_s,
            conviction_boost=boost,
            tribe_id=self.tribe_id,
        )
        self._positions.append(pos)
        self._intended_mark[pid] = mark
        self._peak_favorable_s[pid] = peak_s
        self._expected_move_bps[pid] = emove

    def _peak_favorable_seconds(self, forecast: Forecast, side: Side) -> int:
        """Same as DecisionEngine._peak_favorable_seconds, isolated copy so
        shadow exits don't depend on decision-engine internals. Task 3 —
        de-boost the curve before argmax so the holding window reflects
        the un-boosted alpha peak, not the predatory-inflated one."""
        curve = getattr(forecast, "steps_bps", None)
        if not curve:
            return 0
        boost = float(getattr(forecast, "conviction_boost", 1.0) or 1.0)
        if boost <= 0:
            boost = 1.0
        raw_curve = [b / boost for b in curve] if boost != 1.0 else curve
        if side is Side.BUY:
            idx = max(range(len(raw_curve)), key=lambda i: raw_curve[i])
        else:
            idx = min(range(len(raw_curve)), key=lambda i: raw_curve[i])
        if idx <= 0:
            return 0
        step_seconds = forecast.horizon_seconds / max(1, len(curve))
        return int((idx + 1) * step_seconds)

    def _exit_reason(
        self,
        p: Position,
        price: float,
        ts: datetime,
        emove: float,
        conf: float,
        dz: float,
        rt_fees_bps: float,
    ) -> tuple[str | None, bool, bool]:
        """Return (reason, hit_tp, hit_sl). None reason → stay open."""
        side_sign = 1.0 if p.side is Side.BUY else -1.0
        unreal_bps_pre = side_sign * (price - p.entry_price) / p.entry_price * 1e4

        # Layer 0 — hard SL floor.
        if self.max_position_loss_pct > 0 and p.size_usd > 0 and self._equity > 0:
            max_loss_usd = self._equity * self.max_position_loss_pct / 100.0
            if max_loss_usd > 0:
                floor_bps = -(max_loss_usd / p.size_usd) * 1e4
                if unreal_bps_pre <= floor_bps:
                    return (
                        f"hard SL floor {unreal_bps_pre:+.1f}bps ≤ {floor_bps:+.1f}bps",
                        False, True,
                    )

        # Layer 1 — sign flip + confirm.
        flipped = (p.side is Side.BUY and emove <= -dz) or (
            p.side is Side.SELL and emove >= dz
        )
        if flipped:
            if conf >= self.strong_flip_conf:
                self._flip_streak.pop(p.id, None)
                direction = "short" if p.side is Side.BUY else "long"
                return (
                    f"strong flip {direction} emove={emove:+.1f}bps conf={conf:.2f}",
                    False, unreal_bps_pre < 0,
                )
            streak = self._flip_streak.get(p.id, 0) + 1
            self._flip_streak[p.id] = streak
            if streak >= self.weak_flip_confirm_ticks:
                self._flip_streak.pop(p.id, None)
                return (
                    f"confirmed weak flip ×{streak} emove={emove:+.1f}bps conf={conf:.2f}",
                    False, unreal_bps_pre < 0,
                )
        else:
            self._flip_streak.pop(p.id, None)

        unreal_bps = side_sign * (price - p.entry_price) / p.entry_price * 1e4

        # Layer 2 — profit ratchet.
        if self.ratchet_arm_bps > 0 and self.ratchet_retrace_frac < 1.0:
            peak = max(self._peak_bps.get(p.id, 0.0), unreal_bps)
            self._peak_bps[p.id] = peak
            if peak >= self.ratchet_arm_bps:
                trigger = peak * (1.0 - self.ratchet_retrace_frac)
                if unreal_bps <= trigger:
                    return (
                        f"ratchet peak={peak:+.1f}bps now={unreal_bps:+.1f}bps",
                        True, False,
                    )

        # Layer 2b — forecast-target TP. Mirror of PaperExecutor: the
        # projector no longer multiplies `expected_move_bps` by
        # `conviction_boost`, so the stamped value is already the raw
        # consensus target. The sanity rail at _open_position (Task 2) has
        # already bounded p.expected_move_bps against realized vol.
        if self.forecast_tp_factor > 0.0 and p.expected_move_bps != 0.0:
            target = abs(p.expected_move_bps) * p.confidence * self.forecast_tp_factor
            target = max(target, self.forecast_tp_min_bps)
            if target >= rt_fees_bps and unreal_bps >= target:
                return (
                    f"forecast TP unreal={unreal_bps:+.1f}bps target={target:.1f}bps "
                    f"emove={p.expected_move_bps:+.1f}",
                    True, False,
                )

        # Layer 2c — shape-aware exit.
        if (self.shape_grace_seconds > 0.0 and p.peak_favorable_s > 0
                and unreal_bps > 0):
            held = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held >= p.peak_favorable_s + self.shape_grace_seconds:
                return (
                    f"past peak held={held:.0f}s peak@{p.peak_favorable_s}s "
                    f"unreal={unreal_bps:+.1f}bps",
                    True, False,
                )

        # Layer 3 — edge exhausted.
        if self.edge_exhausted_factor > 0.0:
            held = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held >= self.edge_exhausted_min_age_s:
                remaining = side_sign * emove
                threshold = rt_fees_bps * self.edge_exhausted_factor
                if remaining < threshold:
                    return (
                        f"edge exhausted remaining={remaining:+.1f}bps < "
                        f"{threshold:.1f}bps",
                        False, unreal_bps < 0,
                    )
        return None, False, False

    def _close_position(
        self,
        p: Position,
        mark: float,
        ts: datetime,
        reason: str,
        *,
        hit_tp: bool = False,
        hit_sl: bool = False,
    ) -> None:
        self._flip_streak.pop(p.id, None)
        self._peak_bps.pop(p.id, None)
        self._intended_mark.pop(p.id, None)
        peak_s = self._peak_favorable_s.pop(p.id, 0)
        emove = self._expected_move_bps.pop(p.id, 0.0)
        exit_side = Side.SELL if p.side is Side.BUY else Side.BUY
        fill, _ = self._fill_price(mark, exit_side, p.size_usd)
        if p.side is Side.BUY:
            pnl = p.size_usd * (fill - p.entry_price) / p.entry_price
        else:
            pnl = p.size_usd * (p.entry_price - fill) / p.entry_price
        fee = self._fee(p.size_usd)
        net = pnl - fee
        self._equity += net
        # WS3 I3.c — parallel paper-model fill for executor-parity
        # measurement. Same close, same fee, priced through Paper's
        # `_fill_price_deterministic` instead of shadow's simpler
        # `_fill_price`. None when no PaperExecutor was wired, or on
        # any unexpected call-site mismatch (duck-typed safety net).
        paper_model_bps: float | None = None
        if self._paper_exec is not None and p.size_usd > 0:
            try:
                paper_fill, _ = self._paper_exec._fill_price_deterministic(
                    mark, exit_side, p.size_usd,
                )
                if p.side is Side.BUY:
                    paper_pnl = p.size_usd * (paper_fill - p.entry_price) / p.entry_price
                else:
                    paper_pnl = p.size_usd * (p.entry_price - paper_fill) / p.entry_price
                paper_model_bps = ((paper_pnl - fee) / p.size_usd) * 1e4
            except Exception:
                paper_model_bps = None
        self._trades_completed += 1
        if net > 0:
            self._wins += 1
        held = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
        realized_bps = (net / p.size_usd) * 1e4 if p.size_usd > 0 else 0.0
        self._recent_trades.append({
            "id": p.id,
            "side": p.side.value,
            "size_usd": p.size_usd,
            "entry_price": p.entry_price,
            "exit_price": fill,
            "held_s": round(held),
            "reason": reason,
            "pnl_usd": round(pnl, 4),
            "fee_usd": round(fee, 4),
            "net_usd": round(net, 4),
            "realized_bps": round(realized_bps, 2),
            "hit_tp": bool(hit_tp),
            "hit_sl": bool(hit_sl),
        })
        if len(self._recent_trades) > 60:
            self._recent_trades.pop(0)
        self._pending.append(_ShadowTrade(
            tribe_id=self.tribe_id,
            side=p.side,
            size_usd=p.size_usd,
            entry_price=p.entry_price,
            exit_price=fill,
            entry_ts=p.entry_ts,
            exit_ts=ts,
            pnl_usd=pnl,
            fee_usd=fee,
            net_usd=net,
            realized_bps=realized_bps,
            held_s=held,
            reason=reason,
            hit_tp=bool(hit_tp),
            hit_sl=bool(hit_sl),
            expected_move_bps=float(emove),
            realized_bps_paper_model=paper_model_bps,
        ))

    def snapshot(self) -> dict[str, Any]:
        """Per-tribe ledger state for UI + audit."""
        return {
            "tribe_id": self.tribe_id,
            "equity_usd": round(self._equity, 2),
            "start_equity_usd": round(self._start_equity, 2),
            "peak_equity_usd": round(self._peak_equity, 2),
            "pnl_bps": round(self.pnl_bps_vs_start(), 1),
            "trades": self._trades_completed,
            "wins": self._wins,
            "open_positions": len(self._positions),
            "session_halted": self._session_halted,
            "session_halt_reason": self._session_halt_reason,
        }


def build_shadow_ledgers(
    tribes: list[Tribe],
    cfg: dict,
    *,
    exit_strategy: dict | None = None,
    risk_cfg: dict | None = None,
    decision_cfg: dict | None = None,
    paper_exec: Any | None = None,
) -> dict[str, TribeShadowLedger]:
    """Factory: build one TribeShadowLedger per tribe using the same
    exit-strategy / risk block the real PaperExecutor reads, plus the
    shadow_ledgers overrides. Returns {} when shadow_ledgers.enabled is False.

    When `paper_exec` is provided, every shadow ledger receives a
    reference and logs `realized_bps_paper_model` per close for WS3 I3.c
    executor-parity measurement. None (sprint, tests) leaves the new
    field as None per additive-schema convention.
    """
    sh = cfg.get("shadow_ledgers", {}) or {}
    if not bool(sh.get("enabled", False)):
        return {}
    es = exit_strategy or {}
    rcfg = risk_cfg or {}
    dcfg = decision_cfg or {}
    start_equity = float(sh.get("start_equity_usd", 50_000.0))
    out: dict[str, TribeShadowLedger] = {}
    for t in tribes:
        out[t.spec.id] = TribeShadowLedger(
            t,
            starting_equity_usd=start_equity,
            fee_rate_bps=float(sh.get("fee_rate_bps", 2.0)),
            slippage_bps=float(sh.get("slippage_bps", 2.0)),
            half_spread_bps=float(sh.get("half_spread_bps", 0.5)),
            max_concurrent_positions=int(sh.get("max_concurrent_positions", 5)),
            max_total_notional_pct=float(sh.get("max_total_notional_pct", 200.0)),
            max_hold_seconds=int(sh.get("max_hold_seconds",
                                         rcfg.get("max_hold_seconds", 600))),
            max_session_dd_pct=float(sh.get("max_session_dd_pct", 12.0)),
            exit_dead_zone_bps=float(dcfg.get("exit_dead_zone_bps", 0.5)),
            strong_flip_conf=float(es.get("strong_flip_conf", 0.25)),
            weak_flip_confirm_ticks=int(es.get("weak_flip_confirm_ticks", 2)),
            dead_zone_regime_mult=es.get("dead_zone_regime_mult", {}) or {},
            ratchet_arm_bps=float(es.get("ratchet_arm_bps", 0.0)),
            ratchet_retrace_frac=float(es.get("ratchet_retrace_frac", 1.0)),
            forecast_tp_factor=float(es.get("forecast_tp_factor", 0.0)),
            forecast_tp_min_bps=float(es.get("forecast_tp_min_bps", 0.0)),
            shape_grace_seconds=float(es.get("shape_grace_seconds", 0.0)),
            edge_exhausted_factor=float(es.get("edge_exhausted_factor", 0.0)),
            edge_exhausted_min_age_s=float(es.get("edge_exhausted_min_age_s", 20.0)),
            size_base_pct=float(sh.get("size_base_pct", 2.0)),
            size_conv_scale=float(sh.get("size_conv_scale", 1.0)),
            min_vote_conviction=float(sh.get("min_vote_conviction", 0.10)),
            paper_exec=paper_exec,
        )
    logger.info("[shadow] built {} per-tribe shadow ledgers at ${:,.0f} each",
                len(out), start_equity)
    return out
