"""Paper executor — simulates fills with fees + slippage across a portfolio
of simultaneously-open positions.

Exit model: every tick the council's live forecast is evaluated against every
open position via `evaluate_exits()`. The base rule is a sign-flip beyond the
configured dead-zone; four optional layers stack on top:

  1. **Conviction-magnitude** — weak flips (conf < strong_flip_conf) must
     persist for `weak_flip_confirm_ticks` before closing; strong flips close
     immediately. Streaks reset when the council realigns.
  2. **Profit ratchet** — once a position's peak unrealized reaches
     `ratchet_arm_bps`, it closes on a retrace of `ratchet_retrace_frac` of
     that peak. Arm threshold must clear round-trip fees or the close books
     a net loss.
  2b. **Forecast-target TP** — close when unrealized reaches the move the
     council promised at entry (|emove| × conf × forecast_tp_factor).
     Suppressed when the target is below round-trip fees. This is the only
     layer that takes profit at the top; the ratchet just protects the way
     down.
  3. **Regime-aware dead zone + edge exhaustion** — dead zone scales by
     regime (cascade tighter, chop wider); if remaining forecasted move in
     the position's direction no longer covers fees, close.

`mark_to_market()` remains as a hard backstop for `max_hold_seconds`.

Fees and slippage are charged on both entry and exit of every position.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from ..core.types import Position, Side, TradeIntent
from ..tribes.market_state import MarketState
from .base import Executor
from .harden import HardenConfig, HardenState, PendingFill, realized_vol_bps_from_state
from .ledger import Ledger


class PaperExecutor(Executor):
    def __init__(
        self,
        starting_equity_usd: float,
        fee_rate_bps: float = 5.0,
        slippage_bps: float = 2.0,
        max_concurrent_positions: int = 20,
        max_total_notional_pct: float = 500.0,
        max_hold_seconds: int = 600,
        exit_dead_zone_bps: float = 0.5,
        ledger: Ledger | None = None,
        *,
        exit_strategy: dict | None = None,
        max_drawdown_from_peak_pct: float = 0.0,
        max_daily_loss_pct: float = 0.0,
        slippage_size_k: float = 0.0,
        slippage_ref_notional_usd: float = 100_000.0,
        max_session_dd_pct: float = 0.0,
        max_position_loss_pct: float = 0.0,
        black_swan_sigma: float = 0.0,
        black_swan_cooldown_s: float = 180.0,
        black_swan_window_s: float = 30.0,
        black_swan_sigma_window_s: float = 300.0,
        black_swan_min_ref_sigma_bps: float = 5.0,
        black_swan_warmup_s: float = 30.0,
        slippage_cap_bps: float = 30.0,
        book_depth_frac_cap: float = 0.25,
        half_spread_bps: float = 0.5,
        depth_ref_usd: float = 1_000_000.0,
        # Phase 10-Harden — Merciless mode. When `harden.enabled` is False
        # (default), the executor behaves exactly as pre-Harden code; every
        # harden call-site short-circuits on the flag. When True, three
        # stacked layers (adversarial fill, fat-tail latency, sqrt depth
        # overlay) punish fills the way a real book would.
        harden: HardenConfig | None = None,
        tick_seconds: float = 1.0,
    ) -> None:
        self._equity = starting_equity_usd
        self._start_equity = starting_equity_usd
        self._positions: list[Position] = []
        self.fee_rate_bps = fee_rate_bps
        self.slippage_bps = slippage_bps
        self.slippage_size_k = max(0.0, float(slippage_size_k))
        self.slippage_ref_notional_usd = max(1.0, float(slippage_ref_notional_usd))
        self.max_concurrent_positions = max_concurrent_positions
        self.max_total_notional_pct = max_total_notional_pct
        self.max_hold_seconds = max_hold_seconds
        self.exit_dead_zone_bps = exit_dead_zone_bps
        self.ledger = ledger or Ledger()
        self.trades_completed = 0
        self.wins = 0
        self.longs_opened = 0
        self.shorts_opened = 0
        self._next_id = 1
        self._recent_trades: list[dict] = []   # last 60 closed, newest last

        # Drawdown circuit breaker. Two halts, either trips:
        #   1. equity <= session_peak × (1 - max_drawdown_from_peak_pct/100)
        #   2. equity <= day_anchor × (1 - max_daily_loss_pct/100)
        # While halted, `_capacity_check` rejects new entries; exits and
        # mark-to-market continue unaffected so we don't trap open risk.
        # Daily halt clears on UTC date roll (anchor re-captured). Peak
        # halt stays until equity recovers back to the halt level — any
        # recovery is authentic, not a rounding flap.
        # Both thresholds default to 0.0 = disabled.
        self.max_drawdown_from_peak_pct = max(0.0, float(max_drawdown_from_peak_pct))
        self.max_daily_loss_pct = max(0.0, float(max_daily_loss_pct))
        self._peak_equity = starting_equity_usd
        self._day_anchor_date = None       # type: ignore[assignment]
        self._day_anchor_equity = starting_equity_usd
        self._halt_reason: str | None = None

        # Phase 10 — session drawdown circuit breaker. Unlike the peak-dd
        # halt above (which auto-clears once equity recovers), session-dd
        # HARD-CLOSES all positions the moment it trips and remains halted
        # until `manual_unlock()` is called. Forces a post-mortem before
        # resuming trading. Default 0.0 = disabled.
        self.max_session_dd_pct = max(0.0, float(max_session_dd_pct))
        self._session_halted: bool = False
        self._session_halt_ts: datetime | None = None
        self._session_halt_reason: str | None = None

        # Phase 10 — per-trade hard SL floor. Closes a position as soon as
        # its unrealized loss exceeds `max_position_loss_pct` of current
        # equity. This is the last line of defense against a long tail —
        # overrides all peak-favorable / shape-aware keep-open logic.
        self.max_position_loss_pct = max(0.0, float(max_position_loss_pct))

        # Phase 10 — black-swan 30s price monitor. Rolling price deltas
        # over `black_swan_window_s` are compared to `black_swan_sigma_window_s`-
        # realized stddev; when |Δ| > sigma × ref_sigma we close all positions
        # and suspend new opens for `black_swan_cooldown_s`. sigma=0 disables.
        # `min_ref_sigma_bps` floors the σ baseline so dead-vol resumes don't
        # treat any move as ∞-sigma; `warmup_s` skips detection for that many
        # seconds after a cooldown clears so σ has time to repopulate from
        # real movement instead of the flat-line that built up during the halt.
        self.black_swan_sigma = max(0.0, float(black_swan_sigma))
        self.black_swan_window_s = max(5.0, float(black_swan_window_s))
        self.black_swan_sigma_window_s = max(60.0, float(black_swan_sigma_window_s))
        self.black_swan_cooldown_s = max(0.0, float(black_swan_cooldown_s))
        self.black_swan_min_ref_sigma_bps = max(0.0, float(black_swan_min_ref_sigma_bps))
        self.black_swan_warmup_s = max(0.0, float(black_swan_warmup_s))
        self._black_swan_cooldown_until: datetime | None = None
        self._black_swan_last_fired: datetime | None = None
        self._black_swan_resumed_at: datetime | None = None

        # Phase 11 — realistic execution knobs.
        self.slippage_cap_bps = max(0.0, float(slippage_cap_bps))
        self.book_depth_frac_cap = max(0.0, float(book_depth_frac_cap))
        self.half_spread_bps = max(0.0, float(half_spread_bps))
        self.depth_ref_usd = max(1.0, float(depth_ref_usd))
        # Running totals for report augmentation.
        self._slippage_usd_total = 0.0
        self._fee_usd_total = 0.0
        self._frictionless_equity = starting_equity_usd

        # Multi-layer exit overlay. All layers opt-in via settings; neutral
        # defaults preserve the sign-flip-only behavior.
        es = exit_strategy or {}
        self.strong_flip_conf = float(es.get("strong_flip_conf", 0.25))
        self.weak_flip_confirm_ticks = max(1, int(es.get("weak_flip_confirm_ticks", 2)))
        self.ratchet_arm_bps = float(es.get("ratchet_arm_bps", 0.0))
        self.ratchet_retrace_frac = float(es.get("ratchet_retrace_frac", 1.0))
        self.dead_zone_regime_mult: dict[str, float] = dict(
            es.get("dead_zone_regime_mult", {}) or {}
        )
        self.edge_exhausted_factor = float(es.get("edge_exhausted_factor", 0.0))
        self.edge_exhausted_min_age_s = float(es.get("edge_exhausted_min_age_s", 20.0))
        self.forecast_tp_factor = float(es.get("forecast_tp_factor", 0.0))
        self.forecast_tp_min_bps = float(es.get("forecast_tp_min_bps", 0.0))
        # Shape-aware exit — close if the position has been held past the
        # forecasted favorable-peak step by `shape_grace_seconds`, but only
        # once it's in the green (never turn this into a stop-loss). 0 = off.
        self.shape_grace_seconds = float(es.get("shape_grace_seconds", 0.0))
        # Per-position state for layered exits. Keyed by Position.id; purged
        # on close. Dict lookups are O(1) and we never have more than
        # max_concurrent_positions entries, so the memory story is bounded.
        self._flip_streak: dict[str, int] = {}
        self._peak_bps: dict[str, float] = {}

        # Phase 10-Harden — Merciless mode state. `harden` is a frozen
        # `HardenConfig`; we wrap it in a `HardenState` that owns the
        # seeded RNG + telemetry counters. A None config means "disabled"
        # — use the default, which sets `enabled=False`.
        self._harden_cfg = harden or HardenConfig()
        self._harden = HardenState(self._harden_cfg)
        # Tick cadence is needed so latency draws can decide whether to
        # defer a fill (latency < tick_seconds → absorbed as network
        # noise; latency ≥ tick_seconds → queued to a future tick).
        self._tick_seconds = max(0.001, float(tick_seconds))
        # Pending-fills queue for latency-spiked opens. Drained at the top
        # of every `tick()`. FIFO — a spike landing behind a later-decided
        # short-latency fill still respects its own fill_due_ts, so two
        # intents deferred at the same tick land in decision order even
        # if one had a longer draw.
        self._pending_fills: list[PendingFill] = []

        # WS1 §8 — regime label stashed each tick via set_regime_label()
        # so every close path (black-swan, session halt, council flip,
        # max-hold backstop, manual unwind) can stamp the regime it closed
        # INTO on the ledger without threading a kwarg through five call
        # sites. Engines set this once at the top of the tick.
        self._current_regime_label: str | None = None

    # -- Executor interface ---------------------------------------------

    @property
    def recent_trades(self) -> list[dict]:
        return self._recent_trades

    @property
    def equity_usd(self) -> float:
        return self._equity

    @property
    def positions(self) -> list[Position]:
        return self._positions

    def set_regime_label(self, label) -> None:
        """Stash the current tick's regime label. Every close path reads
        from `self._current_regime_label` so the ledger's close events
        carry the regime the position closed INTO. Accepts RegimeLabel
        enum or bare string; stored as string. Called by every engine
        at the top of the tick, right after regime classification."""
        self._current_regime_label = (
            label.value if hasattr(label, "value") else label
        )

    # Phase 10 — black-swan hook exposed for the engine to call each tick
    # BEFORE forecast-driven exits so a detonating move doesn't let the
    # projector's slow-moving forecast stall on stale state.
    def check_black_swan(
        self, ts: datetime, state: "MarketState"
    ) -> str | None:
        """Scan MarketState's price series for a > σ_ref × N move in the
        last `black_swan_window_s`. When tripped, close all positions at the
        current mark and arm a cooldown. Returns the halt reason if fired,
        else None. Cooldown auto-clears after `black_swan_cooldown_s`."""
        if self.black_swan_sigma <= 0:
            return None
        # Cooldown still active?
        if self._black_swan_cooldown_until is not None:
            if ts < self._black_swan_cooldown_until:
                return (f"black-swan cooldown until "
                        f"{self._black_swan_cooldown_until.isoformat()}")
            # Just resumed — stamp it so the warmup gate below skips
            # detection until σ has been rebuilt from post-halt prints.
            self._black_swan_cooldown_until = None
            self._black_swan_resumed_at = ts
        # Post-resume warmup. The σ baseline was contaminated by a flat-line
        # during the halt; ignore the breaker for a few seconds so the next
        # price print isn't compared against an artificially-tiny σ.
        if (self._black_swan_resumed_at is not None
                and self.black_swan_warmup_s > 0
                and ts < self._black_swan_resumed_at + timedelta(
                    seconds=self.black_swan_warmup_s)):
            return None
        prices = getattr(state, "prices", None)
        if prices is None or not prices._data:
            return None
        cur_price = state.last_price
        if not cur_price or cur_price <= 0:
            return None
        # Find the price closest to (now - window_s).
        now_t = ts.timestamp()
        target = now_t - self.black_swan_window_s
        oldest_in_window: float | None = None
        for t, v in prices._data:
            if t >= target:
                oldest_in_window = v
                break
        if oldest_in_window is None or oldest_in_window <= 0:
            return None
        delta_bps = abs((cur_price - oldest_in_window) / oldest_in_window * 10_000.0)
        # Reference σ — realized stddev of bps returns over the sigma window.
        returns_bps: list[float] = []
        prev: tuple[float, float] | None = None
        sigma_cutoff = now_t - self.black_swan_sigma_window_s
        for t, v in prices._data:
            if t < sigma_cutoff or v <= 0:
                prev = (t, v)
                continue
            if prev is not None and prev[1] > 0:
                returns_bps.append((v - prev[1]) / prev[1] * 10_000.0)
            prev = (t, v)
        if len(returns_bps) < 5:
            return None
        mean = sum(returns_bps) / len(returns_bps)
        var = sum((x - mean) ** 2 for x in returns_bps) / len(returns_bps)
        ref_sigma = var ** 0.5
        # Floor σ so a quiet regime doesn't make a routine twitch look like
        # an ∞-sigma event. Anything below the floor is treated as the floor.
        ref_sigma = max(ref_sigma, self.black_swan_min_ref_sigma_bps)
        if ref_sigma <= 0:
            return None
        threshold = self.black_swan_sigma * ref_sigma
        if delta_bps < threshold:
            return None
        # Fired. Close every open position at the mark and arm cooldown.
        reason = (f"black-swan Δ30s={delta_bps:.1f}bps > "
                  f"{self.black_swan_sigma:.1f}×σ_5m={threshold:.1f}bps")
        self._black_swan_last_fired = ts
        self._black_swan_cooldown_until = ts + timedelta(seconds=self.black_swan_cooldown_s)
        for p in list(self._positions):
            self._close_position(p, cur_price, ts, reason)
        self._positions = []
        logger.warning("BLACK-SWAN  {}  cooldown until {}", reason,
                       self._black_swan_cooldown_until.isoformat())
        return reason

    def manual_unlock(self) -> None:
        """Operator-driven clear of the session-drawdown halt. Leaves the
        peak-drawdown auto-clear halt untouched (it manages itself).

        Also resets the opened-position counters so "post-unlock" session
        activity is visible as a fresh count rather than buried inside
        whatever accumulated before the halt."""
        if self._session_halted:
            logger.warning("[session-dd] manually unlocked (was: {})",
                           self._session_halt_reason)
        self._session_halted = False
        self._session_halt_ts = None
        self._session_halt_reason = None
        self.longs_opened = 0
        self.shorts_opened = 0

    @property
    def session_halted(self) -> bool:
        return self._session_halted

    @property
    def session_halt_reason(self) -> str | None:
        return self._session_halt_reason

    @property
    def black_swan_active(self) -> bool:
        return self._black_swan_cooldown_until is not None

    @property
    def slippage_usd_total(self) -> float:
        return self._slippage_usd_total

    @property
    def fee_usd_total(self) -> float:
        return self._fee_usd_total

    @property
    def frictionless_equity_usd(self) -> float:
        return self._frictionless_equity

    # -- Phase 10-Harden surface ----------------------------------------

    @property
    def harden_enabled(self) -> bool:
        return self._harden_cfg.enabled

    def harden_snapshot(self) -> dict:
        """Serializable harden telemetry for the shock report and UI."""
        snap = self._harden.snapshot()
        snap["pending_fills"] = len(self._pending_fills)
        return snap

    def evaluate_exits(
        self,
        ts: datetime,
        price: float,
        forecast,
        regime_label: str | None = None,
    ) -> None:
        """Close positions the council no longer supports.

        Layered rule — any trigger closes the position:
          • sign-flip beyond regime-scaled dead zone, gated by
            conviction-magnitude (weak flips need a confirm streak)
          • profit ratchet (peak retrace after arming)
          • edge exhausted (remaining forecasted move can't cover fees)

        Positions that stay aligned with a healthy edge keep running; the
        max_hold backstop in `mark_to_market` is the last line of defense.
        """
        if price is None or price <= 0 or not self._positions:
            return
        # Phase 10 — if the session-DD halt just armed, close everything
        # at the mark so the halt actually protects equity (the default
        # refuse-new-opens halt lets existing positions keep bleeding).
        self._refresh_risk_state(ts)
        if self._session_halted:
            reason = f"session halt: {self._session_halt_reason}"
            for p in list(self._positions):
                self._close_position(p, price, ts, reason)
            self._positions = []
            return
        emove = forecast.expected_move_bps
        conf = getattr(forecast, "confidence", 0.0)
        dz = self.exit_dead_zone_bps * float(
            self.dead_zone_regime_mult.get(regime_label or "", 1.0)
        )
        # Round-trip fee budget (notional bps) — both sides pay slippage + fee.
        rt_fees_bps = 2.0 * (self.fee_rate_bps + self.slippage_bps)

        survivors: list[Position] = []
        for p in self._positions:
            reason = self._exit_reason(p, price, ts, emove, conf, dz, rt_fees_bps)
            if reason is not None:
                self._close_position(p, price, ts, reason)
            else:
                survivors.append(p)
        self._positions = survivors

    def _exit_reason(
        self,
        p: Position,
        price: float,
        ts: datetime,
        emove: float,
        conf: float,
        dz: float,
        rt_fees_bps: float,
    ) -> str | None:
        """Return a close-reason string if any exit layer fires, else None."""
        side_sign = 1.0 if p.side is Side.BUY else -1.0
        unreal_bps_pre = side_sign * (price - p.entry_price) / p.entry_price * 1e4

        # Layer 0 — hard SL floor (Phase 10). Worst-case per-position loss is
        # capped at `max_position_loss_pct` of current equity. Converts that
        # dollar cap into a bps-of-notional threshold and closes when reached.
        # This takes priority over all shape-aware / peak-favorable holds.
        if self.max_position_loss_pct > 0 and p.size_usd > 0:
            max_loss_usd = self._equity * self.max_position_loss_pct / 100.0
            if max_loss_usd > 0:
                floor_bps = -(max_loss_usd / p.size_usd) * 1e4
                if unreal_bps_pre <= floor_bps:
                    return (f"hard SL floor {unreal_bps_pre:+.1f}bps <= "
                            f"{floor_bps:+.1f}bps (cap "
                            f"{self.max_position_loss_pct:.2f}% eq)")

        # Layer 1 — sign flip beyond the (regime-scaled) dead zone, gated by
        # flip confidence. Weak flips must persist to close.
        flipped = (p.side is Side.BUY and emove <= -dz) or (
            p.side is Side.SELL and emove >= dz
        )
        if flipped:
            if conf >= self.strong_flip_conf:
                self._flip_streak.pop(p.id, None)
                direction = "short" if p.side is Side.BUY else "long"
                return f"strong flip {direction} emove={emove:+.1f}bps conf={conf:.2f}"
            streak = self._flip_streak.get(p.id, 0) + 1
            self._flip_streak[p.id] = streak
            if streak >= self.weak_flip_confirm_ticks:
                self._flip_streak.pop(p.id, None)
                return (f"confirmed weak flip ×{streak} emove={emove:+.1f}bps "
                        f"conf={conf:.2f}")
        else:
            # Council realigned (or within dead zone) — drop any streak so a
            # future weak flip starts fresh rather than resuming a stale count.
            self._flip_streak.pop(p.id, None)

        # Current unrealized in bps of notional (signed — positive = winning).
        unreal_bps = side_sign * (price - p.entry_price) / p.entry_price * 1e4

        # Layer 2 — profit ratchet. Arms once peak unrealized crosses
        # `ratchet_arm_bps`, then trails at `retrace_frac` of the peak.
        if self.ratchet_arm_bps > 0 and self.ratchet_retrace_frac < 1.0:
            peak = max(self._peak_bps.get(p.id, 0.0), unreal_bps)
            self._peak_bps[p.id] = peak
            if peak >= self.ratchet_arm_bps:
                trigger = peak * (1.0 - self.ratchet_retrace_frac)
                if unreal_bps <= trigger:
                    return (f"ratchet retrace peak={peak:+.1f}bps "
                            f"now={unreal_bps:+.1f}bps")

        # Layer 2b — forecast-target take-profit. Close when the position
        # reaches the move the council promised at entry (|emove| × conf),
        # gated below fees so we never TP into a net loss. This turns a
        # realized forecast into a clean profit-take instead of letting the
        # ratchet give half of it back on the way down.
        #
        # `expected_move_bps` is the raw consensus path: the projector no
        # longer multiplies it by `conviction_boost`, so no de-boost is
        # needed here. The sanity rail stamped at entry (Task 2) separately
        # bounds this against realized vol.
        if self.forecast_tp_factor > 0.0 and p.expected_move_bps != 0.0:
            target = abs(p.expected_move_bps) * p.confidence * self.forecast_tp_factor
            target = max(target, self.forecast_tp_min_bps)
            if target >= rt_fees_bps and unreal_bps >= target:
                return (f"forecast target hit unreal={unreal_bps:+.1f}bps "
                        f"target={target:.1f}bps emove={p.expected_move_bps:+.1f}")

        # Layer 2c — shape-aware exit. The decision engine stamped each
        # position with the step at which the forecast peaked in its favor;
        # once we've been held past that peak by a grace window AND are in
        # the green, scalp the move instead of riding it back down the hill.
        if (self.shape_grace_seconds > 0.0 and p.peak_favorable_s > 0
                and unreal_bps > 0):
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held_s >= p.peak_favorable_s + self.shape_grace_seconds:
                return (f"past forecast peak held={held_s:.0f}s "
                        f"peak@{p.peak_favorable_s}s unreal={unreal_bps:+.1f}bps")

        # Layer 3b — edge exhausted. If the remaining forecasted move in the
        # position's favor can't cover fees, close even though the council is
        # nominally still aligned. Aged-in check avoids killing fresh entries.
        if self.edge_exhausted_factor > 0.0:
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0.0
            if held_s >= self.edge_exhausted_min_age_s:
                remaining = side_sign * emove
                threshold = rt_fees_bps * self.edge_exhausted_factor
                if remaining < threshold:
                    return (f"edge exhausted remaining={remaining:+.1f}bps "
                            f"< fees×{self.edge_exhausted_factor:.2f}={threshold:.1f}bps")
        return None

    def apply_funding(self, funding_rate: float, tick_seconds: float) -> None:
        """Accrue perpetual-futures funding for every open position.

        Longs pay when funding_rate > 0 (bulls pay bears), shorts receive.
        Shorts pay when funding_rate < 0 (bears pay bulls), longs receive.
        Binance epochs are 8 hours (28 800 s); we prorate per tick.

            payment = size_usd * funding_rate * (tick_seconds / 28_800)
        """
        if not self._positions or funding_rate == 0.0:
            return
        total_paid = 0.0
        for p in self._positions:
            sign = 1.0 if p.side is Side.BUY else -1.0
            payment = p.size_usd * funding_rate * sign * (tick_seconds / 28_800.0)
            self._equity -= payment
            total_paid += payment
        if abs(total_paid) > 0.01:
            logger.debug(
                "FUNDING  net=${:+.4f}  rate={:.6f}  open={}",
                total_paid, funding_rate, len(self._positions),
            )
        self._refresh_risk_state()

    def mark_to_market(self, ts: datetime, price: float) -> None:
        """Hard backstop: close positions that exceeded max_hold_seconds."""
        if price is None or price <= 0 or not self._positions:
            return
        survivors: list[Position] = []
        for p in self._positions:
            held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0
            if self.max_hold_seconds and held_s >= self.max_hold_seconds:
                self._close_position(p, price, ts, f"max hold {int(held_s)}s")
            else:
                survivors.append(p)
        self._positions = survivors

    def execute(self, intent: TradeIntent, state: MarketState) -> None:
        price = state.last_price
        if price is None or price <= 0:
            return
        if intent.action == "open_long":
            self._dispatch_open(Side.BUY, intent, price, state=state)
        elif intent.action == "open_short":
            self._dispatch_open(Side.SELL, intent, price, state=state)
        elif intent.action == "close":
            ts = intent.ts
            for p in list(self._positions):
                self._close_position(p, price, ts, intent.reason or "manual", state=state)
            self._positions = []
        # "hold" → nothing

    def _dispatch_open(self, side: Side, intent: TradeIntent, price: float,
                       state: "MarketState | None" = None) -> None:
        """Phase 10-Harden: branch on latency draw.

        Draws one latency sample. Latencies below `tick_seconds` are
        absorbed as "normal network noise" and the fill happens at this
        tick's mark exactly as pre-Harden. Latencies ≥ `tick_seconds`
        queue the intent for a future tick, at which point `_open()` will
        be called against whatever the mark has become (a fresh
        adversarial layer draw is implicit — `_fill_price` reads state
        each time).

        When `harden.enabled=False`, `sample_latency_s` returns `(0.0,
        False)` and the branch takes the immediate path — bit-for-bit
        identical to pre-Harden behavior.
        """
        draw_s, was_spike = self._harden.sample_latency_s()
        if self._harden_cfg.enabled:
            self._harden.latency_samples += 1
            self._harden.latency_total_s += draw_s
            if was_spike:
                self._harden.pareto_draws += 1
        # Sub-tick latencies are network noise — the fill lands this tick.
        if draw_s < self._tick_seconds or not self._harden_cfg.enabled:
            self._open(side, intent, price, state=state)
            return
        # Supra-tick latency — defer. The intent rejoins the executor on
        # the next eligible `tick()` whose `ts >= fill_due_ts`.
        self._harden.latency_spikes += 1
        fill_due = intent.ts + timedelta(seconds=float(draw_s))
        self._pending_fills.append(PendingFill(
            intent=intent,
            intended_mark=price,
            decision_ts=intent.ts,
            fill_due_ts=fill_due,
            drawn_latency_s=float(draw_s),
            was_spike=was_spike,
        ))
        # Sort so later drains pop in due-order regardless of insertion
        # order. Small lists, O(n log n) is free.
        self._pending_fills.sort(key=lambda p: p.fill_due_ts)
        logger.info(
            "LATENCY-SPIKE  {:<5} ${:>8.2f}  draw={:.2f}s{}  queued → fill@{}",
            side.value.upper(), intent.size_usd, draw_s,
            "  [pareto]" if was_spike else "",
            fill_due.isoformat(),
        )
        self._log({
            "event": "latency_spike", "ts": intent.ts, "side": side.value,
            "size_usd": intent.size_usd, "latency_s": round(draw_s, 3),
            "was_pareto": was_spike, "fill_due_ts": fill_due,
            "intended_mark": price,
        })

    def tick(self, ts: datetime, state: "MarketState") -> None:
        """Phase 10-Harden: drain pending-fill queue.

        Call at the top of every engine tick, BEFORE `evaluate_exits` and
        `execute`. Fills whose `fill_due_ts <= ts` land at the *current*
        mark (not the mark at decision time) — so `_fill_price` naturally
        gets a fresh adversarial-bps draw against a price that has
        (usually) moved since the decision. No-op when the pending
        queue is empty OR when harden is disabled.
        """
        if not self._pending_fills:
            return
        cur_price = state.last_price
        if cur_price is None or cur_price <= 0:
            return
        still_waiting: list[PendingFill] = []
        for pending in self._pending_fills:
            if pending.fill_due_ts <= ts:
                self._harden.deferred_fills += 1
                intent = pending.intent
                side = Side.BUY if intent.action == "open_long" else Side.SELL
                # _open uses intent.size_usd, intent.ts, etc. unchanged;
                # the mark is NOW, not at decision time.
                self._open(side, intent, cur_price, state=state,
                           pending_meta=pending)
            else:
                still_waiting.append(pending)
        self._pending_fills = still_waiting

    # -- internals -------------------------------------------------------

    def _fill_price(self, mark: float, taker_side: Side, size_usd: float = 0.0,
                    state: "MarketState | None" = None) -> tuple[float, float]:
        """Book-depth-shaded fill (Phase 11 + Phase 10-Harden overlay).
        Returns `(fill_price, slippage_bps)`.

        Layers, in order:
          - Half-spread charge on entry/exit (always paid, one side each).
          - Linear-within-5bps depth impact: when we know the 5-bp book depth
            on the taker side, slippage_bps = 5 · size_usd / side_depth_5bps_usd
            (i.e. eating the whole 5 bp band would cost 5 bp of slippage).
            Falls back to a size-scaled legacy model when depth is missing.
          - **Phase 10-Harden: sqrt-slippage overlay** (optional, via
            `HardenConfig.sqrt_k`). Multiplies `depth_bps` by
            `1 + sqrt_k * sqrt(size/ref)` — medium-to-large orders take
            a harder hit than the linear model predicts.
          - **Phase 10-Harden: adversarial layer** (optional, via
            `HardenConfig.adv_mult`). Adds `adv_mult × short-window
            realized-vol-bps + adv_floor_bps`, capped at `adv_cap_bps`.
            Captures "the book moved against you during the decision-to-
            fill window". Runs on entry AND exit (caller branches as
            usual).
          - Capped at `slippage_cap_bps` so a broken book snapshot can't
            report a 10 000 bp fill.

        When `harden.enabled=False`, the two harden layers are strict
        no-ops: the overlay multiplier is 1.0 and the adversarial bps is
        0.0. Bit-for-bit identity with pre-Harden code is preserved on
        the Gentle path.

        Backwards-compat: existing callers that do `fill = _fill_price(...)`
        still work — Python tuples unpack on assignment so we keep the 2-arg
        shape by returning the composite; historical single-float paths
        were updated to use the tuple explicitly below.
        """
        sign = 1 if taker_side is Side.BUY else -1
        side_depth = None
        if state is not None:
            side_depth = (state.ask_depth_5bps_usd if taker_side is Side.BUY
                          else state.bid_depth_5bps_usd)
        depth_bps = 0.0
        if side_depth and side_depth > 0 and size_usd > 0:
            # Book-depth model: eating the entire 5 bps band costs 5 bps of
            # adverse slippage. Scale linearly below that; clip above.
            depth_bps = 5.0 * size_usd / float(side_depth)
        else:
            # Legacy size-scaled fallback (kept so replay runs with no L2
            # feed still produce reasonable slippage numbers).
            depth_bps = self.slippage_bps
            if self.slippage_size_k > 0 and size_usd > 0:
                depth_bps *= 1.0 + self.slippage_size_k * (
                    size_usd / self.slippage_ref_notional_usd
                )
        # --- Phase 10-Harden: sqrt-slippage overlay ---------------------
        # Only active when `HardenConfig.enabled=True` AND `sqrt_k > 0`.
        # Otherwise mult == 1.0 and the formula reduces to pre-Harden.
        depth_bps_pre = depth_bps
        sqrt_mult = self._harden.sqrt_overlay_mult(size_usd)
        depth_bps = depth_bps * sqrt_mult
        # Account the marginal dollars attributable to the overlay so the
        # shock report can break out each layer's contribution.
        if sqrt_mult > 1.0 and size_usd > 0:
            marginal_bps = depth_bps - depth_bps_pre
            self._harden.sqrt_overlay_usd_total += (
                size_usd * marginal_bps / 10_000.0
            )

        # --- Phase 10-Harden: adversarial fill --------------------------
        # Short-window realized vol is the multiplicand. Computed from the
        # state's rolling-prices series — falls back to floor-only when
        # the window isn't populated yet (cold start).
        adv_bps = 0.0
        if self._harden_cfg.enabled and state is not None:
            vol_bps = realized_vol_bps_from_state(
                state, self._harden_cfg.adv_vol_window_s
            )
            adv_bps = self._harden.adversarial_bps(vol_bps)
        if adv_bps > 0.0 and size_usd > 0:
            # Telemetry: dollars paid specifically to this layer.
            self._harden.adv_usd_total += size_usd * adv_bps / 10_000.0

        total_bps = min(
            self.slippage_cap_bps,
            self.half_spread_bps + depth_bps + adv_bps,
        )
        # Running averages for the shock-report fill-distribution snapshot.
        self._harden.slip_bps_sum += total_bps
        self._harden.slip_fills_n += 1
        return mark * (1 + sign * total_bps / 10_000.0), total_bps

    def _fee(self, notional_usd: float) -> float:
        return notional_usd * self.fee_rate_bps / 10_000.0

    def _book_capacity_usd(self, side: Side, state: "MarketState | None") -> float | None:
        """Return the max order size, in USD, that stays within the
        configured `book_depth_frac_cap` × side_depth_5bps_usd. None when
        the book snapshot isn't available (no cap enforced)."""
        if state is None or self.book_depth_frac_cap <= 0:
            return None
        d = state.ask_depth_5bps_usd if side is Side.BUY else state.bid_depth_5bps_usd
        if not d or d <= 0:
            return None
        return float(d) * self.book_depth_frac_cap

    def _refresh_risk_state(self, ts: datetime | None = None) -> None:
        """Update session peak + daily anchor, evaluate halts.

        Called before every capacity check and on every equity-changing
        event (open fee deduction, close PnL, funding). Cheap — three
        comparisons and a date check.
        """
        # Ratchet session peak.
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity

        # Phase 10 — session-DD hard halt. Once armed, stays armed until
        # `manual_unlock()` is called (operator post-mortem gate).
        if self.max_session_dd_pct > 0 and not self._session_halted:
            session_trigger = self._peak_equity * (1.0 - self.max_session_dd_pct / 100.0)
            if self._equity <= session_trigger:
                self._session_halted = True
                self._session_halt_ts = ts or datetime.utcnow()
                self._session_halt_reason = (
                    f"session DD ${self._equity:,.2f} <= ${session_trigger:,.2f} "
                    f"(peak ${self._peak_equity:,.2f} - "
                    f"{self.max_session_dd_pct:.1f}%)"
                )
                logger.warning("[session-dd] HALTED  {}", self._session_halt_reason)
        # Daily anchor — set on first call, reset when UTC date changes.
        try:
            today = (ts or datetime.utcnow()).date()
        except Exception:
            today = None
        if today is not None and today != self._day_anchor_date:
            self._day_anchor_date = today
            self._day_anchor_equity = self._equity
            # Daily halts clear with the day. Peak halts survive unless
            # equity has recovered; re-evaluate below.
            if self._halt_reason and self._halt_reason.startswith("daily"):
                self._halt_reason = None

        # Peak-drawdown halt. Arm once equity falls below the trigger;
        # stay halted until equity recovers back above the trigger (no
        # hysteresis band — the peak itself ratchets, so "above the old
        # trigger" is a genuine recovery).
        if self.max_drawdown_from_peak_pct > 0:
            trigger = self._peak_equity * (1.0 - self.max_drawdown_from_peak_pct / 100.0)
            if self._equity <= trigger:
                self._halt_reason = (
                    f"peak drawdown ${self._equity:,.2f} <= "
                    f"${trigger:,.2f} (peak ${self._peak_equity:,.2f} "
                    f"- {self.max_drawdown_from_peak_pct:.1f}%)"
                )
            elif self._halt_reason and self._halt_reason.startswith("peak"):
                self._halt_reason = None

        # Daily-loss halt evaluated after peak so a same-tick recovery
        # on the peak halt doesn't mask an independent daily breach.
        if self.max_daily_loss_pct > 0 and self._day_anchor_equity > 0:
            trigger = self._day_anchor_equity * (1.0 - self.max_daily_loss_pct / 100.0)
            if self._equity <= trigger and (
                self._halt_reason is None or self._halt_reason.startswith("peak")
            ):
                self._halt_reason = (
                    f"daily loss ${self._equity:,.2f} <= ${trigger:,.2f} "
                    f"(open ${self._day_anchor_equity:,.2f} "
                    f"- {self.max_daily_loss_pct:.1f}%)"
                )

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def risk_snapshot(self) -> dict:
        """Per-tick risk state surfaced to the UI for the HALTED banner."""
        return {
            "halted": bool(self._halt_reason or self._session_halted
                           or self._black_swan_cooldown_until is not None),
            "peak_halt_reason": self._halt_reason,
            "session_halted": self._session_halted,
            "session_halt_reason": self._session_halt_reason,
            "session_halt_ts": (self._session_halt_ts.isoformat()
                                if self._session_halt_ts else None),
            "black_swan_cooldown_until": (
                self._black_swan_cooldown_until.isoformat()
                if self._black_swan_cooldown_until else None
            ),
            "peak_equity": self._peak_equity,
            "equity": self._equity,
            "dd_pct_from_peak": (
                (self._peak_equity - self._equity) / self._peak_equity * 100.0
                if self._peak_equity > 0 else 0.0
            ),
            "max_session_dd_pct": self.max_session_dd_pct,
            "max_position_loss_pct": self.max_position_loss_pct,
            "black_swan_sigma": self.black_swan_sigma,
            "slippage_usd_total": self._slippage_usd_total,
            "fee_usd_total": self._fee_usd_total,
            "frictionless_equity_usd": self._frictionless_equity,
        }

    def _capacity_check(self, size_usd: float) -> str | None:
        self._refresh_risk_state()
        if self._halt_reason is not None:
            return f"halted: {self._halt_reason}"
        if self._session_halted:
            return f"session-halted: {self._session_halt_reason}"
        if self._black_swan_cooldown_until is not None:
            return (f"black-swan cooldown until "
                    f"{self._black_swan_cooldown_until.isoformat()}")
        if len(self._positions) >= self.max_concurrent_positions:
            return f"portfolio full ({len(self._positions)}/{self.max_concurrent_positions})"
        cap = self._equity * self.max_total_notional_pct / 100.0
        if self.total_notional_usd + size_usd > cap:
            return (f"notional cap: {self.total_notional_usd + size_usd:,.0f} > "
                    f"{cap:,.0f} ({self.max_total_notional_pct:.0f}% of equity)")
        return None

    def _open(self, side: Side, intent: TradeIntent, mark: float,
              state: "MarketState | None" = None,
              pending_meta: "PendingFill | None" = None) -> None:
        # Phase 11 — capacity clip BEFORE the notional/halt checks so we
        # emit a CAPACITY_CLIPPED event even if the clipped size would have
        # tripped other caps.
        # Phase 10-Harden: `pending_meta` is populated when this open was
        # deferred by a latency spike. Used only for telemetry (the report
        # wants to know "decided at $X, actually filled at $Y N seconds
        # later"); the fill-price math is identical to the non-deferred path.
        requested_size = intent.size_usd
        clipped = False
        clip_note = ""
        cap_usd = self._book_capacity_usd(side, state)
        size_usd = requested_size
        if cap_usd is not None and size_usd > cap_usd:
            clipped = True
            clip_note = (f"book clip {size_usd:,.0f}→{cap_usd:,.0f} "
                         f"(≤{self.book_depth_frac_cap:.0%} × 5bp depth)")
            size_usd = cap_usd
            if size_usd <= 0:
                self._log({
                    "event": "capacity_rejected", "ts": intent.ts,
                    "side": side.value, "size_usd_requested": requested_size,
                    "reason": "book depth = 0",
                })
                return
        reject = self._capacity_check(size_usd)
        if reject is not None:
            logger.info("SKIP  {:<5} ${:>8.2f}  {}", side.value.upper(),
                        size_usd, reject)
            self._log({
                "event": "skip", "ts": intent.ts, "side": side.value,
                "size_usd": size_usd, "reason": reject,
                "size_usd_requested": requested_size,
            })
            return
        intended_mark = mark
        fill, slip_bps = self._fill_price(mark, side, size_usd, state=state)
        intended_pnl_bps_cost = slip_bps  # entry slippage contributes 1× to round-trip
        slip_usd = size_usd * slip_bps / 10_000.0
        fee = self._fee(size_usd)
        self._equity -= fee
        self._slippage_usd_total += slip_usd
        self._fee_usd_total += fee
        self._refresh_risk_state(intent.ts)
        pid = f"t{self._next_id:04d}"
        self._next_id += 1
        # Task 2 — volatility sanity rail. The council's expected_move_bps
        # can be inflated up to 1.5× by the predatory harvest-mass boost.
        # Clamp the stamped move at 1.25× the realized 14-min total-window
        # sigma so exits never chase a target beyond what market vol has
        # actually been producing. No-op when no realized vol is available
        # yet (cold start) or the forecast is already inside the rail.
        emove_stamped = intent.expected_move_bps
        rail_bps = intent.realized_vol_bps
        vol_capped = False
        if rail_bps is not None and rail_bps > 0:
            cap = 1.25 * rail_bps
            mag = abs(emove_stamped)
            if mag > cap:
                sign = 1.0 if emove_stamped >= 0 else -1.0
                emove_stamped = sign * cap
                vol_capped = True
        pos = Position(
            id=pid,
            side=side,
            size_usd=size_usd,
            entry_price=fill,
            entry_ts=intent.ts,
            tp_price=0.0,
            sl_price=0.0,
            horizon_seconds=intent.horizon_seconds,
            expected_move_bps=emove_stamped,
            confidence=intent.confidence,
            peak_favorable_s=intent.peak_favorable_s,
            conviction_boost=intent.conviction_boost,
        )
        # Stash entry-side metadata on the position for later realized-vs-
        # intended accounting at close time. Using a dict-attached sidecar
        # keeps the dataclass untouched (slots=True prohibits new fields).
        self._entry_meta: dict = getattr(self, "_entry_meta", {})
        self._entry_meta[pid] = {
            "intended_mark": intended_mark,
            "entry_slip_bps": slip_bps,
            "entry_slip_usd": slip_usd,
            "entry_fee_usd": fee,
            "requested_size_usd": requested_size,
            "clipped": clipped,
        }
        self._positions.append(pos)
        if side is Side.BUY:
            self.longs_opened += 1
        elif side is Side.SELL:
            self.shorts_opened += 1
        vol_note = ""
        if vol_capped:
            vol_note = (f"  [vol-rail {intent.expected_move_bps:+.1f}→"
                        f"{emove_stamped:+.1f}bps "
                        f"(1.25×σ={1.25 * rail_bps:.1f})]")
        logger.info(
            "OPEN  {} {:<5} ${:>8.2f} @ {:.2f}  emove={:+.1f}bps conf={:.2f}"
            "  slip={:.1f}bps fee=${:.2f}  open={} eq=${:.2f}{}{}",
            pid, side.value.upper(), size_usd, fill,
            emove_stamped, intent.confidence,
            slip_bps, fee, len(self._positions), self._equity,
            f"  [{clip_note}]" if clipped else "",
            vol_note,
        )
        open_rec = {
            "event": "open", "ts": intent.ts, "position_id": pid, "side": side.value,
            "size_usd": size_usd, "fill_price": fill, "mark_price": mark,
            "fee_usd": fee, "slippage_bps": slip_bps, "slippage_usd": slip_usd,
            "equity_usd": self._equity, "reason": intent.reason,
            "expected_move_bps": emove_stamped,
            "expected_move_bps_raw": intent.expected_move_bps,
            "conviction_boost": intent.conviction_boost,
            "realized_vol_bps": rail_bps,
            "vol_capped": vol_capped,
            "confidence": intent.confidence,
            "open_positions": len(self._positions),
            "size_usd_requested": requested_size, "capacity_clipped": clipped,
            "capacity_note": clip_note,
            # WS1 §8 — stamp the forecast shape so auditors can recompute
            # peak_favorable_s off-disk without re-running the projector,
            # and stamp the regime the entry was taken IN.
            "peak_favorable_s": intent.peak_favorable_s,
            "steps_bps": intent.steps_bps,
            "regime_label": self._current_regime_label,
        }
        # Phase 10-Harden: stamp deferred-fill diagnostics when this open
        # came through the latency-spike queue. `drift_bps` is signed
        # against the trade direction — positive = market moved in our
        # favor during the stall, negative = moved against us.
        if pending_meta is not None:
            intended = pending_meta.intended_mark
            drift_bps = 0.0
            if intended > 0:
                sign_move = 1.0 if side is Side.BUY else -1.0
                drift_bps = sign_move * (mark - intended) / intended * 10_000.0
            open_rec.update({
                "harden_deferred": True,
                "decision_ts": pending_meta.decision_ts,
                "decision_mark": intended,
                "latency_s": round(pending_meta.drawn_latency_s, 3),
                "latency_was_pareto": pending_meta.was_spike,
                "latency_drift_bps": round(drift_bps, 2),
            })
        self._log(open_rec)

    def _close_position(self, p: Position, mark: float, ts: datetime, reason: str,
                        state: "MarketState | None" = None) -> None:
        # Scrub per-position layer state before the close goes through so
        # these dicts never outlive the position they describe.
        self._flip_streak.pop(p.id, None)
        self._peak_bps.pop(p.id, None)
        exit_side = Side.SELL if p.side is Side.BUY else Side.BUY
        fill, slip_bps_exit = self._fill_price(mark, exit_side, p.size_usd, state=state)
        if p.side is Side.BUY:
            pnl_usd = p.size_usd * (fill - p.entry_price) / p.entry_price
        else:
            pnl_usd = p.size_usd * (p.entry_price - fill) / p.entry_price
        fee = self._fee(p.size_usd)
        slip_usd_exit = p.size_usd * slip_bps_exit / 10_000.0
        self._slippage_usd_total += slip_usd_exit
        self._fee_usd_total += fee
        self._equity += pnl_usd - fee
        meta = getattr(self, "_entry_meta", {}).pop(p.id, None) if hasattr(self, "_entry_meta") else None
        # Phase 11 — frictionless counterfactual PnL (no slippage, no fee) on
        # both sides. Uses the intended mark at entry vs the current mark at
        # close so the report can publish a "what the strategy would have
        # earned with zero friction" equity curve.
        intended_mark = (meta["intended_mark"] if meta else p.entry_price)
        if p.side is Side.BUY:
            frictionless_pnl = p.size_usd * (mark - intended_mark) / intended_mark
        else:
            frictionless_pnl = p.size_usd * (intended_mark - mark) / intended_mark
        self._frictionless_equity += frictionless_pnl
        self._refresh_risk_state(ts)
        self.trades_completed += 1
        net = pnl_usd - fee
        held_s = (ts - p.entry_ts).total_seconds() if p.entry_ts else 0
        if net > 0:
            self.wins += 1
        entry_slip_bps = float(meta["entry_slip_bps"]) if meta else 0.0
        entry_slip_usd = float(meta["entry_slip_usd"]) if meta else 0.0
        entry_fee_usd = float(meta["entry_fee_usd"]) if meta else 0.0
        total_slip_usd = entry_slip_usd + slip_usd_exit
        total_fee_usd = entry_fee_usd + fee
        intended_bps = (((mark - intended_mark) / intended_mark * 10_000.0)
                        if intended_mark > 0 else 0.0)
        realized_bps = ((net / p.size_usd) * 10_000.0) if p.size_usd > 0 else 0.0
        logger.info(
            "CLOSE {} {:<5} ${:>8.2f} @ {:.2f}  pnl=${:+.2f}  fee=${:.2f}  "
            "slip=${:.2f}  net=${:+.2f}  held={:.0f}s  eq=${:.2f}  ({})",
            p.id, p.side.value.upper(), p.size_usd, fill, pnl_usd, fee,
            slip_usd_exit, net, held_s, self._equity, reason,
        )
        self._recent_trades.append({
            "id": p.id,
            "side": p.side.value,
            "size_usd": p.size_usd,
            "entry_price": p.entry_price,
            "exit_price": fill,
            "held_s": round(held_s),
            "reason": reason,
            "pnl_usd": round(pnl_usd, 4),
            "fee_usd": round(fee, 4),
            "net_usd": round(net, 4),
            "fee_killed": bool(pnl_usd > 0 and net <= 0),
            "expected_move_bps": p.expected_move_bps,
            "confidence": round(p.confidence, 3),
            # Phase 11 — realized vs intended friction accounting.
            "entry_slip_bps": round(entry_slip_bps, 2),
            "exit_slip_bps": round(slip_bps_exit, 2),
            "entry_slip_usd": round(entry_slip_usd, 4),
            "exit_slip_usd": round(slip_usd_exit, 4),
            "total_slip_usd": round(total_slip_usd, 4),
            "total_fee_usd": round(total_fee_usd, 4),
            "intended_bps": round(intended_bps, 2),
            "realized_bps": round(realized_bps, 2),
            "frictionless_pnl_usd": round(frictionless_pnl, 4),
            "capacity_clipped": bool(meta and meta.get("clipped", False)),
        })
        if len(self._recent_trades) > 60:
            self._recent_trades.pop(0)
        self._log({
            "event": "close", "ts": ts, "position_id": p.id, "side": p.side.value,
            "size_usd": p.size_usd, "entry_price": p.entry_price, "fill_price": fill,
            "mark_price": mark, "pnl_usd": pnl_usd, "fee_usd": fee,
            "net_usd": net, "held_seconds": held_s,
            "equity_usd": self._equity, "reason": reason,
            "trades_completed": self.trades_completed, "wins": self.wins,
            "open_positions": len(self._positions) - 1,
            "entry_slip_bps": entry_slip_bps, "exit_slip_bps": slip_bps_exit,
            "total_slip_usd": total_slip_usd, "total_fee_usd": total_fee_usd,
            "intended_bps": intended_bps, "realized_bps": realized_bps,
            "frictionless_equity_usd": self._frictionless_equity,
            # WS1 §8 — regime the position closed INTO, so WS1-style
            # attribution joins (regime × close-reason) can be done from
            # the ledger alone. Stashed by set_regime_label() at tick top.
            "regime_label": self._current_regime_label,
        })

    def _log(self, rec: dict[str, Any]) -> None:
        self.ledger.write(rec)
