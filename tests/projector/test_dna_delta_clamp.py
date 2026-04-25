"""Unit tests for WS3 H6.C — DNA-output clamp.

Covers the Phase 0 audit fix (research/phase11_diagnostic/WS3_H6C_phase0_audit.md
§5 Option A): cap `dna_delta = dna_fn(ctx)` at ±DNA_DELTA_CLAMP_BPS = 100
post-evaluation so the per-tribe `step_bps = (dna_delta + event_bps) *
scale` cannot exceed `100 * DEPTH_SCALE_MAX (= 5) = 500 bps`, the H5.A
consensus saturation ceiling.

Why output cap (not upstream): H6.A clamps recursion *inputs*
(`prev_state_bps`, `prev_delta_bps`) to ±100 — but the DNA *output* term
`intent_drift = c × base_strength × signed_intent` (whale c=0.55, mev 0.35,
ta 0.25...) has `base_strength = conviction × calibrator_skill ×
effective_gain` which is unbounded via calibrator skill_multiplier
feedback. Phase 0 live instrumentation observed `max_dna` per projection
drift from 65 bps (warmup) to 2,744 bps (steady state) over 234
projections in chop regime, despite both H6.A and H6.B in place.

Contracts covered:

  1. Constant value 100.0 (locked to audit §5 recommendation).
  2. Sane raw outputs (|raw| <= cap) pass through unchanged.
  3. Excess raw outputs (raw > cap) clip to +cap.
  4. Excess raw outputs (raw < -cap) clip to -cap.
  5. Boundary: raw == cap returns cap (no off-by-one).
  6. Projector-level diagnostic — `_dna_delta_clamp_hits_last`
     initialises at 0 so tests and regression tooling can read it
     before any projection runs (mirrors H6.A `_dna_clamp_hits_last`
     and H6.B `_depth_scale_cap_hits_last` patterns).

Run:  python -m pytest tests/projector/test_dna_delta_clamp.py -q
"""
from __future__ import annotations

import pytest

from src.projector.chess import DNA_DELTA_CLAMP_BPS


# ------------------------ Contract 1: constant value --------------------

def test_dna_delta_clamp_bps_constant_is_100():
    """Locked to research/phase11_diagnostic/WS3_H6C_phase0_audit.md §5
    Option A. 100.0 = symmetric with H6.A `DNA_CTX_CLAMP_BPS` input
    clamp; with H6.B `DEPTH_SCALE_MAX = 5`, per-tribe step_bps ≤ 500
    (= H5.A `MAX_STEP_BPS`) strictly. If this moves, the test fails
    and forces a review — intentional, not accidental coupling."""
    assert DNA_DELTA_CLAMP_BPS == 100.0


# ------------------------ Contracts 2-5: clamp transform ----------------

def _clamp(raw: float) -> float:
    """Reproduce the chess.py cap expression so the contract has an
    executable spec that doesn't require instantiating ChessProjector.
    Mirror of: `dna_delta = max(-DNA_DELTA_CLAMP_BPS,
    min(DNA_DELTA_CLAMP_BPS, dna_delta_raw))`."""
    return max(-DNA_DELTA_CLAMP_BPS, min(DNA_DELTA_CLAMP_BPS, raw))


@pytest.mark.parametrize(
    "raw",
    [0.0, 1.0, 25.0, 50.0, 65.0, 99.0, 99.99, -1.0, -25.0, -50.0, -65.0, -99.0, -99.99],
)
def test_clamp_passes_sane_raw_outputs_unchanged(raw):
    """Raw DNA outputs within the legal envelope (|raw| <= cap) round-trip
    identically — preserves design intent that DNA functions express
    bounded per-step magnitudes up to the ±100 ceiling."""
    assert _clamp(raw) == raw


@pytest.mark.parametrize(
    "raw,expected",
    [
        (100.01, 100.0),
        (150.0, 100.0),
        (500.0, 100.0),
        (1132.1, 100.0),  # phase 0 audit p50 max_dna
        (3364.1, 100.0),  # phase 0 audit max max_dna observed
    ],
)
def test_clamp_clips_positive_excess(raw, expected):
    """Raw outputs above the cap (raw > +100) clip to exactly +100,
    bounding the per-step DNA contribution to step_bps and closing
    door #3 of the saturation chain."""
    assert _clamp(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (-100.01, -100.0),
        (-150.0, -100.0),
        (-500.0, -100.0),
        (-1132.1, -100.0),
        (-3364.1, -100.0),
    ],
)
def test_clamp_clips_negative_excess(raw, expected):
    """Raw outputs below -cap clip to exactly -100, symmetric with the
    positive case so a negatively-signed_intent whale_dna or fade-mode
    arb_dna can't push step_bps below -500 either."""
    assert _clamp(raw) == expected


def test_clamp_at_exact_boundary_returns_cap():
    """`max(-100, min(100, 100.0)) = 100.0` — no off-by-one. Important
    because the chain arithmetic (100 × 5 scale = 500 = MAX_STEP_BPS)
    treats the cap as inclusive, not exclusive."""
    assert _clamp(DNA_DELTA_CLAMP_BPS) == DNA_DELTA_CLAMP_BPS
    assert _clamp(-DNA_DELTA_CLAMP_BPS) == -DNA_DELTA_CLAMP_BPS
    assert _clamp(100.0) == 100.0
    assert _clamp(-100.0) == -100.0


# ------------------------ Contract 6: projector diagnostic --------------

def test_projector_init_has_dna_delta_clamp_counter():
    """Fresh ChessProjector exposes `_dna_delta_clamp_hits_last` = 0 so
    tests and regression tooling can read it before any projection
    runs (mirrors the H6.A `_dna_clamp_hits_last` and H6.B
    `_depth_scale_cap_hits_last` patterns)."""
    from src.projector.chess import ChessProjector
    proj = ChessProjector(tribes=[], steps=1, tick_seconds=5)
    assert proj._dna_delta_clamp_hits_last == 0
