"""Unit tests for DecisionEngine — gates the audit flagged as silently broken.

Covers the specific failure modes surfaced by the 2026-04-20 audit:
  - agree_mult clamp behaviour (was max(1.0, min(1.0, a)) — always 1.0)
  - path-integrity threshold (was < 0.0 — never fired)
  - shrink floors (sigma/crowd/horizon) — were all 1.0 (no-ops)
  - HTF counter-trend shrink
  - min_conf gate
  - directional-agreement multiplier range

Run:  python -m pytest tests/test_decision_engine.py -q
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.core.types import Aggregate, Forecast, Side
from src.decision.engine import DecisionEngine
from src.tribes.market_state import MarketState


def _mk_forecast(
    *,
    emove_bps: float = 10.0,
    conf: float = 0.5,
    horizon_s: int = 300,
    sigma_terminal_bps: float = 30.0,
    agree: float | None = None,
    dsa: float | None = None,
    category_mass: dict | None = None,
    avg_voter_horizon_s: float | None = None,
) -> Forecast:
    ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    step = Aggregate(ts=ts, net_pressure=0.1, buy_mass=1.0, sell_mass=0.0,
                     abstain_mass=0.0, vote_count=1, uncertainty=0.1)
    return Forecast(
        ts=ts,
        steps=[step],
        expected_move_bps=emove_bps,
        confidence=conf,
        horizon_seconds=horizon_s,
        steps_bps=[emove_bps],
        steps_sigma_bps=[sigma_terminal_bps],
        directional_agreement=agree,
        driver_skill_avg=dsa,
        category_mass=category_mass,
        avg_voter_horizon_s=avg_voter_horizon_s,
    )


def _settings(**over) -> dict:
    base = {
        "risk": {"max_hold_seconds": 600},
        "sizing": {"min_pct_equity": 1.0, "max_pct_equity": 6.0, "curve": "linear"},
        "leverage": 1.0,
        "decision": {
            "min_conf": 0.10,
            "sigma_ref_bps": 40.0,
            "sigma_shrink_floor": 0.35,
            "crowded_trade": {
                "conf_threshold": 0.5,
                "share_threshold": 0.5,
                "shrink_floor": 0.45,
                "categories": ["retail", "ta"],
            },
            "horizon_shrink": {"ref_ratio": 0.5, "shrink_floor": 0.55},
        },
        "htf": {"counter_strength": 0.6, "max_age_seconds": 7200.0},
    }
    for k, v in over.items():
        base[k] = v
    return base


def _state(price: float = 50000.0, htf_score: float = 0.0, htf_ts=None) -> MarketState:
    s = MarketState()
    s.last_price = price
    if htf_score != 0.0:
        s.htf_trend_score = htf_score
        s.htf_trend_ts = htf_ts or datetime(2026, 4, 20, 11, 55, 0, tzinfo=timezone.utc)
    return s


# ------------------------- Gate: min_conf --------------------------------

def test_min_conf_gate_holds_below_threshold():
    eng = DecisionEngine(_settings())
    f = _mk_forecast(conf=0.05)
    ts = f.ts
    intent = eng.decide(ts, f, _state(), 50_000.0)
    assert intent.action == "hold"
    assert "incoherent" in intent.reason


def test_min_conf_gate_opens_above_threshold():
    eng = DecisionEngine(_settings())
    f = _mk_forecast(conf=0.5, emove_bps=+10)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert intent.action == "open_long"


def test_flat_forecast_holds():
    eng = DecisionEngine(_settings())
    f = _mk_forecast(emove_bps=0.0, conf=0.5)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert intent.action == "hold"


def test_no_mark_price_holds():
    eng = DecisionEngine(_settings())
    f = _mk_forecast()
    intent = eng.decide(f.ts, f, _state(price=0.0), 50_000.0)
    assert intent.action == "hold"


# --------- C3: agree_mult clamp. Was max(1.0, min(1.0, a))=1.0 always ----

def test_agree_mult_clamps_low_agreement_down():
    """directional_agreement = 0.5 ⇒ agree_mult = 0.5 (was 1.0 pre-fix)."""
    eng = DecisionEngine(_settings())
    f_hi = _mk_forecast(agree=1.0)
    f_lo = _mk_forecast(agree=0.5)
    i_hi = eng.decide(f_hi.ts, f_hi, _state(), 50_000.0)
    i_lo = eng.decide(f_lo.ts, f_lo, _state(), 50_000.0)
    assert i_lo.size_usd < i_hi.size_usd
    # Ratio should be ≈ 0.5 (the only multiplier that differs).
    assert i_lo.size_usd == pytest.approx(0.5 * i_hi.size_usd, rel=1e-6)


def test_agree_mult_below_half_floors_at_half():
    """Even agree=0.3 should clamp to 0.5 (lower bound)."""
    eng = DecisionEngine(_settings())
    f = _mk_forecast(agree=0.3)
    base = _mk_forecast(agree=1.0)
    i = eng.decide(f.ts, f, _state(), 50_000.0)
    i0 = eng.decide(base.ts, base, _state(), 50_000.0)
    assert i.size_usd == pytest.approx(0.5 * i0.size_usd, rel=1e-6)


def test_agree_mult_missing_is_unity():
    """driver_skill None ⇒ agree_mult=1.0; no amplification, no shrink."""
    eng = DecisionEngine(_settings())
    f = _mk_forecast(agree=None)
    i = eng.decide(f.ts, f, _state(), 50_000.0)
    # With all multipliers = 1.0, size = equity * pct * lev.
    pct = 1.0 + (6.0 - 1.0) * 0.5  # linear curve at conf=0.5
    expected = 50_000.0 * pct / 100.0 * 1.0
    assert i.size_usd == pytest.approx(expected, rel=1e-6)


# --------- C4: path-integrity threshold. Was < 0.0 (dead). -----------------

def test_path_integrity_caps_boosters_when_skill_low():
    """driver_skill_avg < 0.7 ⇒ agree/crowd/htf/horizon all capped at 1.0."""
    eng = DecisionEngine(_settings())
    # With dsa=0.5, path_integrity_ok=False. agree=1.0 is below the cap,
    # unchanged — but the flag MUST be set; use a low-agree case to observe.
    f = _mk_forecast(dsa=0.5, agree=0.8)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert "PATH_INTEGRITY_CAP" in intent.reason
    assert intent.action in ("open_long", "open_short")  # we still enter


def test_path_integrity_passes_when_skill_high():
    eng = DecisionEngine(_settings())
    f = _mk_forecast(dsa=0.9, agree=1.0)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert "PATH_INTEGRITY_CAP" not in intent.reason


def test_path_integrity_threshold_at_boundary():
    eng = DecisionEngine(_settings())
    # 0.7 exactly should NOT trigger (strict <).
    f_ok = _mk_forecast(dsa=0.7)
    f_cap = _mk_forecast(dsa=0.69)
    ok = eng.decide(f_ok.ts, f_ok, _state(), 50_000.0)
    cap = eng.decide(f_cap.ts, f_cap, _state(), 50_000.0)
    assert "PATH_INTEGRITY_CAP" not in ok.reason
    assert "PATH_INTEGRITY_CAP" in cap.reason


# ---------- H1: shrink floors. Were all 1.0 (defensive gates dead) ----------

def test_sigma_shrink_floors_to_floor_when_sigma_huge():
    eng = DecisionEngine(_settings())
    # sigma_ref=40; sigma_terminal=400 ⇒ raw = 0.1 < floor 0.35 ⇒ clip to 0.35.
    f_huge = _mk_forecast(sigma_terminal_bps=400.0)
    f_norm = _mk_forecast(sigma_terminal_bps=40.0)
    i_huge = eng.decide(f_huge.ts, f_huge, _state(), 50_000.0)
    i_norm = eng.decide(f_norm.ts, f_norm, _state(), 50_000.0)
    assert i_huge.size_usd == pytest.approx(0.35 * i_norm.size_usd, rel=1e-6)


def test_sigma_shrink_neutral_when_sigma_tight():
    eng = DecisionEngine(_settings())
    f = _mk_forecast(sigma_terminal_bps=10.0)  # way below ref
    f_ref = _mk_forecast(sigma_terminal_bps=40.0)  # at ref = 1.0
    i = eng.decide(f.ts, f, _state(), 50_000.0)
    i_ref = eng.decide(f_ref.ts, f_ref, _state(), 50_000.0)
    # Both clip to 1.0; equal size.
    assert i.size_usd == pytest.approx(i_ref.size_usd, rel=1e-6)


def test_crowded_trade_shrinks_when_high_conf_retail_heavy():
    """Crowded-trade filter (C4/H1): high conf + herd dominance → shrink."""
    eng = DecisionEngine(_settings())
    # Buy side dominated by retail+ta → share > 0.5 crowded_share_threshold.
    mass = {
        "retail": {"buy": 0.5, "sell": 0.0},
        "ta": {"buy": 0.3, "sell": 0.0},
        "mm": {"buy": 0.1, "sell": 0.0},
        "event": {"buy": 0.1, "sell": 0.0},
    }
    # With retail+ta share = 0.8 and threshold 0.5, overshoot/span = 0.6.
    # Shrink factor = 1 - (1 - 0.45) * 0.6 = 0.67
    f_crowd = _mk_forecast(conf=0.9, category_mass=mass)
    clean = {"mm": {"buy": 0.5, "sell": 0.0}, "event": {"buy": 0.5, "sell": 0.0}}
    f_clean = _mk_forecast(conf=0.9, category_mass=clean)
    i_c = eng.decide(f_crowd.ts, f_crowd, _state(), 50_000.0)
    i_0 = eng.decide(f_clean.ts, f_clean, _state(), 50_000.0)
    assert i_c.size_usd < i_0.size_usd
    assert i_c.size_usd == pytest.approx(0.67 * i_0.size_usd, rel=1e-2)


def test_horizon_shrinks_when_scalpers_dominate():
    """Avg voter horizon far below projector nominal ⇒ deleverage toward floor."""
    eng = DecisionEngine(_settings())
    # avg_voter_horizon=30s against nominal=300s ⇒ ratio=0.1; ref=0.5 ⇒
    # t = 0.1/0.5 = 0.2; shrink = 0.55 + 0.45*0.2 = 0.64.
    f = _mk_forecast(avg_voter_horizon_s=30.0, horizon_s=300)
    f_ref = _mk_forecast(avg_voter_horizon_s=300.0, horizon_s=300)
    i = eng.decide(f.ts, f, _state(), 50_000.0)
    i_ref = eng.decide(f_ref.ts, f_ref, _state(), 50_000.0)
    assert i.size_usd < i_ref.size_usd
    assert i.size_usd == pytest.approx(0.64 * i_ref.size_usd, rel=1e-2)


# -------------- H3: HTF counter-trend shrink ------------------------------

def test_htf_shrinks_counter_trend_long():
    eng = DecisionEngine(_settings())
    # Long entry against score=-1.0 (strong down 1h trend) ⇒ shrink by 0.6
    # (counter_strength) × 1.0 = 0.6 ⇒ mult = 0.4.
    f = _mk_forecast(emove_bps=+10)
    i_ctr = eng.decide(f.ts, f, _state(htf_score=-1.0), 50_000.0)
    i_alg = eng.decide(f.ts, f, _state(htf_score=+1.0), 50_000.0)
    assert i_ctr.size_usd < i_alg.size_usd
    assert i_ctr.size_usd == pytest.approx(0.4 * i_alg.size_usd, rel=1e-6)


def test_htf_disabled_when_counter_strength_zero():
    eng = DecisionEngine(_settings(htf={"counter_strength": 0.0}))
    f = _mk_forecast(emove_bps=+10)
    i_ctr = eng.decide(f.ts, f, _state(htf_score=-1.0), 50_000.0)
    i_alg = eng.decide(f.ts, f, _state(htf_score=+1.0), 50_000.0)
    assert i_ctr.size_usd == pytest.approx(i_alg.size_usd, rel=1e-6)


def test_htf_stale_score_treated_as_no_signal():
    eng = DecisionEngine(_settings())
    stale_ts = datetime(2026, 4, 20, 0, 0, 0, tzinfo=timezone.utc)  # 12h old
    f = _mk_forecast(emove_bps=+10)
    st = _state(htf_score=-1.0, htf_ts=stale_ts)
    i = eng.decide(f.ts, f, st, 50_000.0)
    # Stale → treated as 1.0.
    f2 = _mk_forecast(emove_bps=+10)
    i0 = eng.decide(f2.ts, f2, _state(), 50_000.0)
    assert i.size_usd == pytest.approx(i0.size_usd, rel=1e-6)


# -------------- Peak-favorable: conviction-boost de-boost (C2 linkage) ----

def test_peak_favorable_uses_deboosted_curve():
    """The stamped peak-favorable step should be argmax of the RAW curve,
    not the boosted one. Uniform scaling doesn't shift argmax today but the
    contract matters for future horizon-dependent boost shapes."""
    from src.core.types import Forecast
    ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    step = Aggregate(ts=ts, net_pressure=0.1, buy_mass=1.0, sell_mass=0.0,
                     abstain_mass=0.0, vote_count=1, uncertainty=0.1)
    # Curve peaks at index 3 out of 5. horizon=300 ⇒ step=60s each ⇒
    # peak_favorable_s = (3+1)*60 = 240.
    curve = [1.0, 2.0, 3.0, 5.0, 4.0]
    f = Forecast(
        ts=ts, steps=[step], expected_move_bps=5.0, confidence=0.5,
        horizon_seconds=300, steps_bps=curve,
        steps_sigma_bps=[30.0] * 5, conviction_boost=1.5,
    )
    eng = DecisionEngine(_settings())
    intent = eng.decide(ts, f, _state(), 50_000.0)
    assert intent.peak_favorable_s == 240


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
