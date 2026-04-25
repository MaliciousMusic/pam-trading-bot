"""Unit tests for WS3 H5.I1 — cumulative-path attractor instrumentation.

Covers the two-scalar additive plumbing per ``WS3_H5_SCOPE.md`` §3.1:

  - ``abs_activity_bps`` (float): Σ |step_bps_i| · decay_i, denominator of
    ``directional_score`` at ``chess.py:805``.
  - ``step_bps_signflips`` (int): #(consecutive sign flips in
    ``step_bps_list``). Upper bound n_steps − 1.

Scope of verification:

  - ``Forecast`` struct accepts both fields and defaults both to ``None``
    (additive contract — pre-H5.I1 construction sites must still work).
  - ``ForecastAuditor`` carries both fields verbatim through
    ``PendingAnchor`` → ``Resolved`` → persisted ``.jsonl`` payload.
  - The sign-flip counting rule matches the pure-alternation attractor
    documented in the H3 path-audit (P4): ``[+X, -X, +X, ...]`` yields
    n − 1 flips; ``[+X, 0, +X, ...]`` yields 0 (strict ``a*b < 0.0``);
    monotone increments yield 0.

Run:  python -m pytest tests/test_ws3_h5_instrumentation.py -q
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.audit.backscore import ForecastAuditor, PendingAnchor, Resolved
from src.core.types import Forecast


# ---------------------------------------------------------------- Forecast struct

def _minimal_forecast(**overrides) -> Forecast:
    """Smallest legal Forecast — only required (positional-like) fields."""
    defaults = dict(
        ts=datetime(2026, 4, 24, 20, 0, 0, tzinfo=timezone.utc),
        steps=[],
        expected_move_bps=0.0,
        confidence=0.5,
        horizon_seconds=60,
    )
    defaults.update(overrides)
    return Forecast(**defaults)


def test_forecast_defaults_h5i1_fields_to_none():
    """Pre-H5.I1 construction sites must still work — both additive
    fields default to None and are accessible as attributes."""
    f = _minimal_forecast()
    assert f.abs_activity_bps is None
    assert f.step_bps_signflips is None


def test_forecast_round_trips_h5i1_fields():
    f = _minimal_forecast(abs_activity_bps=12.5, step_bps_signflips=247)
    assert f.abs_activity_bps == pytest.approx(12.5)
    assert f.step_bps_signflips == 247


# ---------------------------------------------------------------- signflip contract

def _signflips(xs: list[float]) -> int:
    """Mirror of the projector's one-liner (chess.py H5.I1 block).

    Kept in-test so the contract documented in the H5 scope is covered
    by an executable spec — not just by the projector's behaviour.
    """
    return sum(1 for a, b in zip(xs, xs[1:]) if a * b < 0.0)


def test_signflip_pure_alternation_attracts_to_n_minus_one():
    # P4 suspected-bug attractor: step_bps_list = [+X, -X, +X, -X, ...].
    # n = 250 ⇒ 249 sign-changes expected (matches scope §3.3).
    xs = [1.0 if i % 2 == 0 else -1.0 for i in range(250)]
    assert _signflips(xs) == 249


def test_signflip_flat_monotone_is_zero():
    # Monotone same-sign increments ⇒ no flips (clean trend, scope §3.3).
    xs = [0.01 * (i + 1) for i in range(250)]
    assert _signflips(xs) == 0


def test_signflip_ignores_exact_zero_entries():
    # Strict ``a * b < 0.0`` — a zero-crossing through 0.0 is NOT a flip.
    # Documented behaviour: avoids a Dirac-at-0 dominating the count on
    # flat stretches where the averaging produced numerical 0.0.
    xs = [1.0, 0.0, 1.0, 0.0, -1.0]  # only the 0.0,-1.0 pair is < 0? No: 0.0*-1.0 = -0.0 → 0, not < 0.
    assert _signflips(xs) == 0


def test_signflip_single_element_is_zero():
    assert _signflips([1.0]) == 0
    assert _signflips([]) == 0


def test_signflip_mixed_example_counts_exact():
    # Hand-counted: pairs are (+-), (-+), (++), (+-) → 3 flips out of 4.
    xs = [1.0, -1.0, 2.0, 3.0, -0.5]
    assert _signflips(xs) == 3


# ---------------------------------------------------------------- auditor plumbing

def test_auditor_persists_h5i1_fields_on_resolved_jsonl(tmp_path: Path):
    """End-to-end contract: H5.I1 scalars stamped on a tick forecast
    must reach the persisted jsonl unmodified, both on tick emit and
    after resolve. Covers the full path: ``record_tick`` → ``PendingAnchor``
    → ``_resolve_due`` → ``Resolved`` → ``_persist``."""
    auditor = ForecastAuditor(directory=tmp_path)

    # Tick 1 — emit one 15-second anchor at t=0 carrying the scalars.
    t0 = datetime(2026, 4, 24, 20, 0, 0, tzinfo=timezone.utc)
    predictions = [{
        "horizon_seconds": 15,
        "predicted_price": 50_100.0,
        "expected_move_bps": 20.0,
        "label": "15s",
    }]
    resolved = auditor.record_tick(
        t0, 50_000.0, predictions,
        forecast_conf=0.4,
        forecast_emove_bps=20.0,
        net_pressure=0.25,
        signal_quality=0.3,
        directional_score=0.01010101,
        base_conf=0.00303,
        diversity_score=0.7,
        diversity_mult=0.88,
        abs_activity_bps=49.68,
        step_bps_signflips=248,
    )
    assert resolved == []  # nothing matured yet

    # Tick 2 — 30 s later, matures the 15 s anchor and triggers persist.
    t1 = datetime(2026, 4, 24, 20, 0, 30, tzinfo=timezone.utc)
    resolved = auditor.record_tick(
        t1, 50_025.0, [],
        forecast_conf=0.0,
        forecast_emove_bps=0.0,
        net_pressure=0.0,
    )
    assert len(resolved) == 1
    rec = resolved[0]
    assert rec.abs_activity_bps == pytest.approx(49.68)
    assert rec.step_bps_signflips == 248

    # Re-read the persisted jsonl and check the payload carries the two
    # scalars under their exact keys.
    jsonl_path = tmp_path / "2026-04-24.jsonl"
    assert jsonl_path.exists()
    rows = [json.loads(ln) for ln in jsonl_path.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["abs_activity_bps"] == pytest.approx(49.68)
    assert row["step_bps_signflips"] == 248
    # Ensure the H1.I1 fields stayed intact on the same row (no regression).
    assert row["signal_quality"] == pytest.approx(0.3)
    assert row["directional_score"] == pytest.approx(0.01010101)
    assert row["diversity_mult"] == pytest.approx(0.88)


def test_auditor_persists_none_for_pre_h5i1_ticks(tmp_path: Path):
    """A tick recorded without the H5.I1 kwargs must persist ``null``
    for both keys — pre-H5.I1 backward-compat contract per scope §3.1."""
    auditor = ForecastAuditor(directory=tmp_path)

    t0 = datetime(2026, 4, 24, 20, 0, 0, tzinfo=timezone.utc)
    auditor.record_tick(
        t0, 50_000.0,
        [{"horizon_seconds": 15, "predicted_price": 50_100.0,
          "expected_move_bps": 20.0, "label": "15s"}],
        forecast_conf=0.4, forecast_emove_bps=20.0, net_pressure=0.1,
    )
    # No abs_activity_bps / step_bps_signflips passed — defaults apply.

    t1 = datetime(2026, 4, 24, 20, 0, 30, tzinfo=timezone.utc)
    resolved = auditor.record_tick(
        t1, 50_025.0, [],
        forecast_conf=0.0, forecast_emove_bps=0.0, net_pressure=0.0,
    )
    assert len(resolved) == 1
    rec = resolved[0]
    assert rec.abs_activity_bps is None
    assert rec.step_bps_signflips is None

    row = json.loads((tmp_path / "2026-04-24.jsonl").read_text().strip())
    assert row["abs_activity_bps"] is None
    assert row["step_bps_signflips"] is None


# ---------------------------------------------------------------- dataclass defaults

def test_pending_anchor_defaults_h5i1_to_none():
    a = PendingAnchor(
        tick_ts=0.0, target_ts=15.0, label="15s", horizon_s=15,
        entry_price=50_000.0, predicted_price=50_100.0,
        expected_move_bps=20.0, forecast_conf=0.4,
        forecast_emove_bps=20.0, net_pressure=0.1,
    )
    assert a.abs_activity_bps is None
    assert a.step_bps_signflips is None


def test_resolved_defaults_h5i1_to_none():
    r = Resolved(
        tick_ts=0.0, target_ts=15.0, realized_ts=15.0, label="15s",
        horizon_s=15, entry_price=50_000.0, predicted_price=50_100.0,
        realized_price=50_050.0, expected_move_bps=20.0,
        realized_move_bps=10.0, signed_error_bps=10.0, abs_error_bps=10.0,
        directional_hit=True, forecast_conf=0.4, net_pressure=0.1,
    )
    assert r.abs_activity_bps is None
    assert r.step_bps_signflips is None
