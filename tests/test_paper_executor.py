"""Unit tests for PaperExecutor — exit-layer logic and risk breakers.

Covers the specific failure modes surfaced by the 2026-04-20 audit:
  - C1: volatility sanity rail clamps expected_move_bps at entry
  - C2: forecast-TP uses raw_move / boost (de-boosted target)
  - Hard SL floor (max_position_loss_pct) fires before any other layer
  - Session-dd hard halt hard-closes positions + requires manual_unlock
  - Peak-dd soft halt auto-clears on recovery
  - Daily-loss halt clears on UTC roll
  - Profit ratchet arm + retrace
  - Regime-aware dead zone scales by regime_label

Run:  python -m pytest tests/test_paper_executor.py -q
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from src.core.types import Forecast, Position, Side, TradeIntent
from src.execution.ledger import Ledger
from src.execution.paper import PaperExecutor
from src.tribes.market_state import MarketState


def _state(price: float = 50_000.0) -> MarketState:
    s = MarketState()
    s.last_price = price
    s.best_bid = price - 2.5
    s.best_ask = price + 2.5
    return s


def _ts(minute: int = 0, second: int = 0) -> datetime:
    base = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minute, seconds=second)


def _mk_executor(exit_strategy_over: dict | None = None, **over) -> PaperExecutor:
    es = {
        "strong_flip_conf": 0.25,
        "weak_flip_confirm_ticks": 2,
        "ratchet_arm_bps": 20.0,
        "ratchet_retrace_frac": 0.35,
        "forecast_tp_factor": 1.0,
        "forecast_tp_min_bps": 0.0,
        "shape_grace_seconds": 30.0,
        "edge_exhausted_factor": 0.0,   # off by default in tests — noisy
        "edge_exhausted_min_age_s": 20.0,
        "dead_zone_regime_mult": {
            "chop": 1.5, "normal": 1.0, "cascade_up": 0.5, "cascade_down": 0.5,
        },
    }
    if exit_strategy_over:
        es.update(exit_strategy_over)
    kwargs = dict(
        starting_equity_usd=50_000.0,
        fee_rate_bps=2.0, slippage_bps=2.0,
        max_concurrent_positions=10, max_total_notional_pct=500.0,
        max_hold_seconds=600, exit_dead_zone_bps=3.0,
        ledger=Ledger(),
        exit_strategy=es,
        half_spread_bps=0.5,
    )
    kwargs.update(over)
    return PaperExecutor(**kwargs)


def _mk_intent(
    *, ts: datetime | None = None, action: str = "open_long", size_usd: float = 10_000.0,
    price_ref: float = 50_000.0, emove_bps: float = 20.0, conf: float = 0.6,
    horizon_s: int = 300, peak_s: int = 180, conviction_boost: float = 1.0,
    realized_vol_bps: float | None = None,
) -> TradeIntent:
    return TradeIntent(
        ts=ts or _ts(),
        action=action, size_usd=size_usd, price_ref=price_ref,
        reason="test", expected_move_bps=emove_bps, confidence=conf,
        horizon_seconds=horizon_s, peak_favorable_s=peak_s,
        conviction_boost=conviction_boost, realized_vol_bps=realized_vol_bps,
    )


def _mk_forecast(emove_bps: float, conf: float = 0.5, horizon: int = 300) -> Forecast:
    from src.core.types import Aggregate
    ts = _ts()
    step = Aggregate(ts=ts, net_pressure=emove_bps/10, buy_mass=1.0, sell_mass=0.0,
                     abstain_mass=0.0, vote_count=1, uncertainty=0.1)
    return Forecast(
        ts=ts, steps=[step], expected_move_bps=emove_bps,
        confidence=conf, horizon_seconds=horizon,
        steps_bps=[emove_bps], steps_sigma_bps=[30.0],
    )


# ============== C1: volatility sanity rail clamps at entry =================

def test_volatility_rail_clamps_inflated_emove():
    """emove stamped at 200bps against realized_vol 50bps ⇒ clamp to 62.5bps."""
    ex = _mk_executor()
    intent = _mk_intent(emove_bps=200.0, realized_vol_bps=50.0)
    ex.execute(intent, state=_state())
    pos = ex.positions[0]
    assert pos.expected_move_bps == pytest.approx(62.5)  # 1.25 × 50.0


def test_volatility_rail_respects_sign():
    ex = _mk_executor()
    intent = _mk_intent(action="open_short", emove_bps=-200.0, realized_vol_bps=50.0)
    ex.execute(intent, state=_state())
    pos = ex.positions[0]
    assert pos.expected_move_bps == pytest.approx(-62.5)


def test_volatility_rail_noop_when_inside_cap():
    ex = _mk_executor()
    intent = _mk_intent(emove_bps=30.0, realized_vol_bps=50.0)  # 30 < 62.5
    ex.execute(intent, state=_state())
    assert ex.positions[0].expected_move_bps == pytest.approx(30.0)


def test_volatility_rail_noop_when_no_realized_vol():
    ex = _mk_executor()
    intent = _mk_intent(emove_bps=200.0, realized_vol_bps=None)
    ex.execute(intent, state=_state())
    assert ex.positions[0].expected_move_bps == pytest.approx(200.0)


# ============== C2: forecast TP de-boost (raw_move / boost) ================

def test_forecast_tp_uses_deboosted_target():
    """Forecast TP layer should compute target from raw_move = emove / boost,
    so a 1.5× harvest boost doesn't push the TP 50% farther than consensus."""
    # Disable shape/ratchet/edge so forecast-TP is isolated.
    ex = _mk_executor(exit_strategy_over={
        "ratchet_arm_bps": 0.0,
        "shape_grace_seconds": 0.0,
    })
    # Entry: 60 bps stamped at 1.5× boost ⇒ raw_move=40 bps.
    # TP target = |40| × 0.6 conf × 1.0 factor = 24 bps.
    intent = _mk_intent(emove_bps=60.0, conf=0.6, conviction_boost=1.5)
    ex.execute(intent, state=_state(50_000.0))
    assert len(ex.positions) == 1
    # Push to +28 bps of notional — after entry slip ~2.5 the unreal is
    # ~25.5 bps > deboosted target 24 bps.
    new_price = 50_000.0 * (1 + 28 / 10_000)
    f = _mk_forecast(emove_bps=+20, conf=0.6)   # council still aligned
    ex.evaluate_exits(_ts(1, 0), new_price, f)
    assert len(ex.positions) == 0
    assert any("forecast target" in t.get("reason", "") for t in ex.recent_trades)


def test_forecast_tp_no_boost_unchanged():
    """With conviction_boost=1.0, target is raw emove×conf (no de-boost effect)."""
    ex = _mk_executor(exit_strategy_over={
        "ratchet_arm_bps": 0.0,
        "shape_grace_seconds": 0.0,
    })
    intent = _mk_intent(emove_bps=40.0, conf=0.6, conviction_boost=1.0)
    ex.execute(intent, state=_state(50_000.0))
    # Target = 40 × 0.6 = 24 bps; push price to +28 bps of notional.
    new_price = 50_000.0 * (1 + 28 / 10_000)
    f = _mk_forecast(emove_bps=+20, conf=0.6)
    ex.evaluate_exits(_ts(1, 0), new_price, f)
    assert len(ex.positions) == 0


# ============== Hard SL floor (max_position_loss_pct) ======================

def test_hard_sl_floor_closes_large_loss():
    """Position losing > 1% of equity closes immediately, before any layer."""
    ex = _mk_executor(max_position_loss_pct=1.0)
    # 10,000 notional at 50k; 1% equity = $500 max loss. A 500-bp adverse
    # move on 10k = $500 → trigger.
    intent = _mk_intent(size_usd=10_000.0, emove_bps=20.0)
    ex.execute(intent, state=_state(50_000.0))
    adverse = 50_000.0 * (1 - 0.05)  # 500 bps down on a long
    f = _mk_forecast(emove_bps=+10)  # council still aligned
    ex.evaluate_exits(_ts(0, 30), adverse, f)
    assert len(ex.positions) == 0
    # Exit reason should reference the position-loss floor.
    reasons = " ".join(t.get("reason", "") for t in ex.recent_trades).lower()
    assert "loss" in reasons or "sl" in reasons or "floor" in reasons


# ============== Session drawdown hard halt =================================

def test_session_halt_triggers_and_blocks_new_opens():
    """When equity drops > max_session_dd_pct from peak, halt fires and
    refuses new entries until manual_unlock."""
    ex = _mk_executor(max_session_dd_pct=8.0)
    # Force equity below threshold and refresh risk state to trip halt.
    ex._peak_equity = 50_000.0
    ex._equity = 45_000.0  # 10% dd > 8% threshold
    ex._refresh_risk_state(_ts())
    assert ex._session_halted
    # Capacity check should reject new opens.
    reject = ex._capacity_check(1000.0)
    assert reject is not None
    assert "session" in reject.lower()


def test_manual_unlock_clears_halt_and_resets_counters():
    """manual_unlock() clears the session-halt flag AND resets
    longs_opened/shorts_opened (the M9 fix)."""
    ex = _mk_executor(max_session_dd_pct=8.0)
    # Manually trip then unlock.
    ex._session_halted = True
    ex._session_halt_reason = "forced"
    ex.longs_opened = 5
    ex.shorts_opened = 3
    ex.manual_unlock()
    assert ex._session_halted is False
    assert ex.longs_opened == 0
    assert ex.shorts_opened == 0


# ============== Peak / daily drawdown soft halts ===========================

def test_peak_dd_halt_auto_clears_on_recovery():
    ex = _mk_executor(max_drawdown_from_peak_pct=8.0)
    # Force peak via simulated loss recovery.
    ex._peak_equity = 50_000.0
    ex._equity = 45_000.0  # 10% drawdown
    ex._refresh_risk_state(_ts())
    assert ex._halt_reason is not None
    # Recover above halt level.
    ex._equity = 49_000.0
    ex._refresh_risk_state(_ts(1))
    assert ex._halt_reason is None


def test_daily_loss_halt_clears_on_utc_roll():
    ex = _mk_executor(max_daily_loss_pct=5.0)
    day0 = _ts()
    ex._day_anchor_date = day0.date()
    ex._day_anchor_equity = 50_000.0
    ex._equity = 47_000.0  # 6% daily loss
    ex._refresh_risk_state(day0)
    assert ex._halt_reason is not None
    # Next UTC day — anchor resets.
    next_day = day0 + timedelta(days=1)
    ex._refresh_risk_state(next_day)
    assert ex._halt_reason is None
    assert ex._day_anchor_date == next_day.date()


# ============== Profit ratchet ============================================

def test_ratchet_arms_and_closes_on_retrace():
    # Disable forecast TP / shape-aware so the ratchet is the only layer.
    ex = _mk_executor(exit_strategy_over={
        "forecast_tp_factor": 0.0,
        "shape_grace_seconds": 0.0,
    })
    intent = _mk_intent(size_usd=10_000.0, emove_bps=50.0, conf=0.6)
    ex.execute(intent, state=_state(50_000.0))
    # Account for entry slippage (~2.5 bps on a long). Peak = +33 bps of
    # notional ⇒ net unreal ≈ +30 bps > arm=20 ⇒ ratchet arms.
    peak_price = 50_000.0 * (1 + 33 / 10_000)
    f = _mk_forecast(emove_bps=+30, conf=0.6)
    ex.evaluate_exits(_ts(0, 30), peak_price, f)
    assert len(ex.positions) == 1
    # Retrace to +20 bps ⇒ unreal ≈ +17.5 bps < 30×0.65=19.5 ⇒ close.
    retrace_price = 50_000.0 * (1 + 20 / 10_000)
    ex.evaluate_exits(_ts(1, 0), retrace_price, f)
    assert len(ex.positions) == 0
    assert any("ratchet" in t.get("reason", "").lower() for t in ex.recent_trades)


# ============== H6: regime-aware dead zone =================================

def test_cascade_regime_tightens_dead_zone():
    """cascade_up regime multiplier 0.5 halves the dead zone → flip closes earlier."""
    ex = _mk_executor(exit_strategy_over={
        "edge_exhausted_factor": 0.0,
        "weak_flip_confirm_ticks": 1,
    })
    intent = _mk_intent(size_usd=10_000.0, emove_bps=30.0, conf=0.6)  # long
    ex.execute(intent, state=_state(50_000.0))
    # emove=-2 flips in cascade (dz=1.5) but NOT in normal (dz=3).
    f_flip = _mk_forecast(emove_bps=-2.0, conf=0.5)
    ex.evaluate_exits(_ts(0, 30), 50_000.0, f_flip, regime_label="normal")
    assert len(ex.positions) == 1
    ex.evaluate_exits(_ts(1, 0), 50_000.0, f_flip, regime_label="cascade_up")
    assert len(ex.positions) == 0


def test_chop_regime_widens_dead_zone():
    """chop multiplier 1.5 widens dead zone so noise doesn't trigger exits."""
    ex = _mk_executor(exit_strategy_over={
        "edge_exhausted_factor": 0.0,
        "weak_flip_confirm_ticks": 1,
    })
    intent = _mk_intent(size_usd=10_000.0, emove_bps=30.0, conf=0.6)
    ex.execute(intent, state=_state(50_000.0))
    # -4 bps: flips in normal (>3), but NOT in chop (>4.5).
    f_flip = _mk_forecast(emove_bps=-4.0, conf=0.5)
    ex.evaluate_exits(_ts(0, 30), 50_000.0, f_flip, regime_label="chop")
    assert len(ex.positions) == 1  # dead zone widened; no flip
    # Sanity: in normal, -4 IS outside dz=3, closes on confirm.
    ex2 = _mk_executor(exit_strategy_over={
        "edge_exhausted_factor": 0.0,
        "weak_flip_confirm_ticks": 1,
    })
    ex2.execute(_mk_intent(size_usd=10_000.0, emove_bps=30.0, conf=0.6),
                state=_state(50_000.0))
    ex2.evaluate_exits(_ts(0, 30), 50_000.0, f_flip, regime_label="normal")
    assert len(ex2.positions) == 0


# ============== Max hold backstop =========================================

def test_max_hold_seconds_force_closes():
    ex = _mk_executor(max_hold_seconds=60)
    intent = _mk_intent(size_usd=10_000.0)
    ex.execute(intent, state=_state(50_000.0))
    ex.mark_to_market(_ts(0, 59), 50_000.0)
    assert len(ex.positions) == 1
    ex.mark_to_market(_ts(1, 1), 50_000.0)
    assert len(ex.positions) == 0
    assert any("max hold" in t.get("reason", "").lower() for t in ex.recent_trades)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
