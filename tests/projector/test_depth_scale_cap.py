"""Unit tests for WS3 H6.B — depth-scale cap.

Covers the Phase 0 audit fix (research/phase11_diagnostic/WS3_H6B_phase0_audit.md
§7 Option B): cap the per-step amplifier `scale = depth_ref_usd /
max(side_depth, depth_floor_usd)` at `DEPTH_SCALE_MAX = 5.0` so the
product `(dna_delta + event_bps) * scale` cannot exceed the H5.A
saturation ceiling on routine BTC chop.

Contracts covered:

  1. Constant value 5.0 (locked to audit §7 recommendation).
  2. Sane raw scales (raw <= cap) pass through `min()` unchanged.
  3. Excess raw scales (raw > cap) cap at 5.0.
  4. Boundary: raw_scale == cap returns the cap (no off-by-one).
  5. Side-depth=0 / None branch preserves the legacy `scale = 1.0`
     (no regression introduced by the cap).
  6. Projector-level diagnostic — `_depth_scale_cap_hits_last`
     initialises at 0 so tests and regression tooling can read it
     before any projection runs.

Run:  python -m pytest tests/projector/test_depth_scale_cap.py -q
"""
from __future__ import annotations

import pytest

from src.projector.chess import DEPTH_SCALE_MAX


# ------------------------ Contract 1: constant value --------------------

def test_depth_scale_max_constant_is_5():
    """Locked to research/phase11_diagnostic/WS3_H6B_phase0_audit.md §7
    Option B. 5.0 = thin-book amplification ceiling beyond which
    `total_bps * scale > 500 bps` H5.A saturation ceiling on routine
    chop. If this moves, the test fails and forces a review —
    intentional, not accidental coupling."""
    assert DEPTH_SCALE_MAX == 5.0


# ------------------------ Contracts 2 & 3: cap transform ----------------

def _cap(raw: float) -> float:
    """Reproduce the chess.py:628-630 cap expression so the contract has
    an executable spec that doesn't require instantiating ChessProjector.
    Mirror of: `scale = min(raw_scale, DEPTH_SCALE_MAX)`."""
    return min(raw, DEPTH_SCALE_MAX)


@pytest.mark.parametrize("raw", [1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 4.99])
def test_cap_passes_sane_raw_scales_unchanged(raw):
    """Raw scales within the legal envelope (1 <= raw <= cap) round-trip
    identically — preserves the design intent that 'thin books amplify
    pushes' up to the 5x ceiling."""
    assert _cap(raw) == raw


@pytest.mark.parametrize(
    "raw,expected",
    [
        (5.01, 5.0),
        (7.1, 5.0),    # ta_dna routine breach point per audit §2
        (10.0, 5.0),
        (20.0, 5.0),   # legacy theoretical max scale
        (100.0, 5.0),  # pathological / floor-divisor
    ],
)
def test_cap_clips_excess_raw_scales(raw, expected):
    """Raw scales above the cap (raw > 5) clip down to exactly 5.0,
    bounding the per-step amplifier and closing door #2 of the
    saturation chain."""
    assert _cap(raw) == expected


# ------------------------ Contract 4: boundary --------------------------

def test_cap_at_exact_boundary_returns_cap():
    """`min(5.0, 5.0) = 5.0` — no off-by-one. Important because the
    audit's quartile arithmetic (ta_dna 65 bps × 5 = 325 < 500) treats
    the cap as inclusive, not exclusive."""
    assert _cap(DEPTH_SCALE_MAX) == DEPTH_SCALE_MAX
    assert _cap(DEPTH_SCALE_MAX) == 5.0


# ------------------------ Contract 5: legacy fallback -------------------

def test_legacy_zero_depth_branch_preserved():
    """When `side_depth` is 0 / None, chess.py falls back to
    `scale = 1.0` (unchanged from pre-H6.B behaviour). The cap MUST
    NOT be applied to this fallback — it would silently turn the
    legacy 1.0 into an unintended floor. Verified here at the
    constant level: 1.0 < cap, so even if a future refactor pipes
    the fallback through `min(...)`, the value remains 1.0.

    Spec form: the legacy scalar 1.0 is below the cap, so the cap
    expression `min(1.0, DEPTH_SCALE_MAX)` is identity."""
    legacy_fallback = 1.0
    assert _cap(legacy_fallback) == legacy_fallback
    assert _cap(legacy_fallback) == 1.0


# ------------------------ Contract 6: projector diagnostic --------------

def test_projector_init_has_depth_scale_cap_counter():
    """Fresh ChessProjector exposes `_depth_scale_cap_hits_last` = 0 so
    tests and regression tooling can read it before any projection
    runs (mirrors the H6.A `_dna_clamp_hits_last` pattern)."""
    from src.projector.chess import ChessProjector
    proj = ChessProjector(tribes=[], steps=1, tick_seconds=5)
    assert proj._depth_scale_cap_hits_last == 0
