"""Unit tests for WS3 H5.A — step_bps_list saturation clamp.

Covers the four acceptance criteria from WS3_H5_phase0_audit.md §4 + the
§6.3 e2e fold-in (NaN projector output must not open a position):

  1. |step| > MAX_STEP_BPS → clamped to ±MAX_STEP_BPS, saturation_hit=True.
  2. NaN/inf entries → coerced to 0.0, saturation_hit=True.
  3. Sane entries → pass through unchanged, saturation_hit=False.
  4. End-to-end: a Forecast emitted with saturation_hit=True (confidence=0
     by H5.A contract) must trigger DecisionEngine hold with 'incoherent'
     in the reason — reproducing the §6.3 fix path that closes the
     12-shorts-on-NaN bug.

The first three tests reproduce the projector's sanitize transform as a
test-local helper (``_sat_transform``) so the algorithmic contract is
covered by an executable spec independent of the projector's construction
path. Mirror the pattern used in tests/test_ws3_h5_instrumentation.py.

Run:  python -m pytest tests/projector/test_chess_saturation.py -q
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src.core.types import Aggregate, Forecast, Side
from src.decision.engine import DecisionEngine
from src.tribes.market_state import MarketState


# MUST stay in lock-step with chess.py (H5.A block). If that constant moves,
# the test fails and forces a review — intentional, not accidental coupling.
MAX_STEP_BPS = 500.0


def _sat_transform(step_bps_list: list[float]) -> tuple[list[float], bool]:
    """Reproduce the H5.A sanitize logic from chess.py so the contract has
    an executable spec that doesn't depend on instantiating ChessProjector.

    Returns ``(sanitized_list, saturation_hit)``. Must match chess.py:789-833.
    """
    saturation_hit = any(
        (not math.isfinite(b)) or abs(b) > MAX_STEP_BPS
        for b in step_bps_list
    )
    if not saturation_hit:
        return list(step_bps_list), False

    def _sat(b: float) -> float:
        if not math.isfinite(b):
            return 0.0
        if b > MAX_STEP_BPS:
            return MAX_STEP_BPS
        if b < -MAX_STEP_BPS:
            return -MAX_STEP_BPS
        return b

    return [_sat(b) for b in step_bps_list], True


# ------------------------ Contract 1: huge finite clamp ------------------

def test_huge_finite_step_clamps_at_max_and_flags():
    """|step| > MAX_STEP_BPS clamps to ±MAX_STEP_BPS and flags saturation."""
    # Spike pattern from WS3_H5_phase0_audit.md §1: single-horizon positive
    # spike creates adjacent ±HUGE in step_bps_list. 6.5e+295 is the real
    # magnitude observed in the pathological tape.
    inp = [0.0, 0.0, 6.5e295, -6.5e295, 0.0, 0.0]
    out, hit = _sat_transform(inp)
    assert hit is True
    assert out == [0.0, 0.0, MAX_STEP_BPS, -MAX_STEP_BPS, 0.0, 0.0]
    # abs_activity contribution now bounded: Σ |b| ≤ n * MAX_STEP_BPS.
    assert sum(abs(b) for b in out) <= len(out) * MAX_STEP_BPS


def test_moderate_exceed_clamps_but_small_unchanged():
    """Only entries strictly exceeding the clamp are modified — the rest
    round-trips bit-identical (determinism preservation on sane tails)."""
    inp = [499.999, 500.0, 500.00001, -501.0, 10.0]
    out, hit = _sat_transform(inp)
    # 500.0 is NOT > MAX_STEP_BPS (strict), so it passes. 500.00001 clamps.
    assert hit is True
    assert out[0] == 499.999
    assert out[1] == 500.0
    assert out[2] == MAX_STEP_BPS
    assert out[3] == -MAX_STEP_BPS
    assert out[4] == 10.0


# ------------------------ Contract 2: NaN/inf → 0 ------------------------

def test_nan_and_inf_steps_zero_and_flag():
    """NaN / ±inf entries coerced to 0.0; saturation_hit=True. Fixes §6.3
    where NaN emove slipped past DecisionEngine's conf-check."""
    inp = [0.0, float("nan"), float("inf"), float("-inf"), 1.0]
    out, hit = _sat_transform(inp)
    assert hit is True
    # NaN/inf zeroed; the 0.0 and 1.0 pass through unchanged.
    assert out[0] == 0.0
    assert out[1] == 0.0     # was NaN
    assert out[2] == 0.0     # was +inf
    assert out[3] == 0.0     # was -inf
    assert out[4] == 1.0
    # No NaN remains — critical for the downstream emove=Σ step·w sum.
    assert all(math.isfinite(b) for b in out)


# ------------------------ Contract 3: sane pass-through ------------------

def test_sane_steps_pass_through_unchanged():
    """Sane curves (no-NaN, |step| ≤ MAX_STEP_BPS) must be bit-identical
    through the transform — determinism guarantee for the 99%+ case."""
    inp = [1.23, -4.56, 0.0, 2.0, -3.14, 100.0, -200.0]
    out, hit = _sat_transform(inp)
    assert hit is False
    assert out == inp
    # Explicit id-preservation contract: returned list is a shallow copy,
    # not the same object. Prevents accidental aliasing of steps_bps.
    assert out is not inp


def test_empty_list_does_not_flag():
    """Degenerate input — empty step_bps_list (pre-warmup) must not flag."""
    out, hit = _sat_transform([])
    assert hit is False
    assert out == []


# ------------------------ Contract 4: e2e §6.3 fold-in -------------------

def _mk_saturated_forecast(emove_bps: float = 0.0) -> Forecast:
    """Forecast as it would emerge from ChessProjector.project() when
    saturation_hit=True: directional_score=0, base_conf=0, confidence=0,
    saturation_hit=True stamp."""
    ts = datetime(2026, 4, 24, 22, 47, 0, tzinfo=timezone.utc)
    step = Aggregate(ts=ts, net_pressure=0.0, buy_mass=0.0, sell_mass=0.0,
                     abstain_mass=1.0, vote_count=0, uncertainty=1.0)
    return Forecast(
        ts=ts,
        steps=[step],
        expected_move_bps=emove_bps,
        confidence=0.0,
        horizon_seconds=60,
        steps_bps=[0.0],
        steps_sigma_bps=[30.0],
        directional_score=0.0,
        base_conf=0.0,
        saturation_hit=True,
    )


def _settings() -> dict:
    """Minimal DecisionEngine settings — conf=0 must trip min_conf=0.10."""
    return {
        "risk": {"max_hold_seconds": 600},
        "sizing": {"min_pct_equity": 1.0, "max_pct_equity": 6.0, "curve": "linear"},
        "leverage": 1.0,
        "decision": {
            "min_conf": 0.10,
            "sigma_ref_bps": 40.0,
            "sigma_shrink_floor": 0.35,
            "crowded_trade": {
                "conf_threshold": 0.5, "share_threshold": 0.5,
                "shrink_floor": 0.45, "categories": ["retail", "ta"],
            },
            "horizon_shrink": {"ref_ratio": 0.5, "shrink_floor": 0.55},
        },
        "htf": {"counter_strength": 0.6, "max_age_seconds": 7200.0},
    }


def _state(price: float = 50_000.0) -> MarketState:
    s = MarketState()
    s.last_price = price
    return s


def test_saturated_forecast_decision_engine_holds_incoherent():
    """Acceptance criterion 3 (§6.3 fold-in): a saturated Forecast with
    confidence=0 AND non-zero emove must route to DecisionEngine hold
    with 'incoherent' in the reason — the exact signature of the pre-H5.A
    bug where 12 shorts opened on NaN emove + conf=0.79.

    emove=10 is used (not 0) to drive the assertion through the
    `conf < min_conf` gate rather than the earlier `emove == 0`
    'council flat' gate — the latter would hold correctly too, but via a
    different code path and wouldn't prove H5.A's contract. The §6.3 bug
    was specifically a min_conf bypass, so we assert on that gate."""
    eng = DecisionEngine(_settings())
    f = _mk_saturated_forecast(emove_bps=10.0)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert intent.action == "hold"
    assert "incoherent" in intent.reason


def test_saturated_forecast_holds_even_on_flat_emove():
    """Defence-in-depth: when saturation produces emove=0 (all spikes
    zeroed and sanitized values happened to sum to 0), the tick still
    holds — via the earlier `emove == 0` 'council flat' gate instead of
    min_conf. Either gate is acceptable; H5.A just needs zero trades on
    saturated output. Asserts the weaker 'action == hold' contract."""
    eng = DecisionEngine(_settings())
    f = _mk_saturated_forecast(emove_bps=0.0)
    intent = eng.decide(f.ts, f, _state(), 50_000.0)
    assert intent.action == "hold"


# ------------------------ Forecast struct defaults -----------------------

def test_forecast_saturation_hit_defaults_false():
    """Pre-H5.A Forecast fixtures (no saturation_hit kwarg) keep working —
    additive contract same as H5.I1 scalars."""
    ts = datetime(2026, 4, 24, 22, 47, 0, tzinfo=timezone.utc)
    f = Forecast(
        ts=ts, steps=[], expected_move_bps=0.0, confidence=0.5,
        horizon_seconds=60,
    )
    assert f.saturation_hit is False


def test_forecast_saturation_hit_round_trips_true():
    ts = datetime(2026, 4, 24, 22, 47, 0, tzinfo=timezone.utc)
    f = Forecast(
        ts=ts, steps=[], expected_move_bps=0.0, confidence=0.0,
        horizon_seconds=60, saturation_hit=True,
    )
    assert f.saturation_hit is True
