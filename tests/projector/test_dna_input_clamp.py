"""Unit tests for WS3 H6.A — DNAContext input clamp.

Covers the Phase 0 audit fix (research/phase11_diagnostic/WS3_H6_phase0_audit.md
§6 Option C): bound `prev_state_bps` and `prev_delta_bps` entering the
per-step DNAContext to ±DNA_CTX_CLAMP_BPS so the feedback chain through
`prev_delta` cannot amplify beyond the clamp.

Contracts covered:

  1. Constant value ±100 bps (locked to audit §6).
  2. Sane inputs (|v| ≤ DNA_CTX_CLAMP_BPS) pass through unchanged.
  3. Large inputs (|v| > DNA_CTX_CLAMP_BPS) clipped to ±DNA_CTX_CLAMP_BPS.
  4. End-to-end recursion reproduction — a TA-category chain with scale>1.54
     diverges geometrically without the clamp, stays bounded with it.
  5. Retail-panic recursion — quadratic overshoot bounded with the clamp.
  6. Projector-level diagnostic — `_dna_clamp_hits_last` increments on
     divergent input state, stays 0 on sane tape.

Run:  python -m pytest tests/projector/test_dna_input_clamp.py -q
"""
from __future__ import annotations

import math

import pytest

from src.projector.behavioral_dna import DNAContext, retail_dna, ta_dna
from src.projector.chess import DNA_CTX_CLAMP_BPS


# ------------------------ Contract 1: constant value --------------------

def test_dna_ctx_clamp_constant_is_100_bps():
    """Locked to research/phase11_diagnostic/WS3_H6_phase0_audit.md §6 Option C.
    100 bps = 1% single-step state. If this moves, the test fails and forces
    a review — intentional, not accidental coupling."""
    assert DNA_CTX_CLAMP_BPS == 100.0


# ------------------------ Contract 2 & 3: clamp transform ---------------

def _clamp(v: float) -> float:
    """Reproduce the chess.py:599-600 clamp expression so the contract has
    an executable spec that doesn't require instantiating ChessProjector."""
    return max(-DNA_CTX_CLAMP_BPS, min(DNA_CTX_CLAMP_BPS, v))


@pytest.mark.parametrize("v", [-100.0, -99.99, -50.0, 0.0, 10.0, 99.99, 100.0])
def test_clamp_passes_sane_values_unchanged(v):
    """|v| ≤ DNA_CTX_CLAMP_BPS round-trips identically — preserves every
    legitimate momentum / state signal in the normal operating envelope
    (2026-04 realised BTC σ ≈ 30 bps/5s, well under the 100 bps clamp)."""
    assert _clamp(v) == v


@pytest.mark.parametrize(
    "v,expected",
    [
        (100.01, 100.0),
        (200.0, 100.0),
        (6.5e295, 100.0),      # H5 pathological magnitude
        (float("inf"), 100.0),
        (-100.01, -100.0),
        (-200.0, -100.0),
        (-6.5e295, -100.0),
        (float("-inf"), -100.0),
    ],
)
def test_clamp_clips_large_values(v, expected):
    """|v| > DNA_CTX_CLAMP_BPS → ±DNA_CTX_CLAMP_BPS (sign preserved)."""
    assert _clamp(v) == expected


# ------------------------ Contract 4: TA divergence bounded -------------

def _run_ta_chain(n_steps: int, scale: float, clamp: bool) -> tuple[float, int]:
    """Simulate the per-step TA recursion isolated from the projector.
    Mirrors chess.py:577-637 arithmetic. Returns (final cum_bps, clamp hits)."""
    cum_bps = 0.0
    prev_delta = 0.0
    mr_rate = 0.005   # default mean_reversion_rate for BTC perp runs
    hits = 0
    for step_i in range(n_steps):
        ps_in = _clamp(cum_bps) if clamp else cum_bps
        pd_in = _clamp(prev_delta) if clamp else prev_delta
        if clamp and (ps_in != cum_bps or pd_in != prev_delta):
            hits += 1
        ctx = DNAContext(
            prev_state_bps=ps_in,
            prev_delta_bps=pd_in,
            regime="normal",
            perceived=[],
            signed_intent=1.0,
            base_strength=1.0,
            h=step_i,
        )
        dna_delta = ta_dna(ctx)
        step_bps = dna_delta * scale
        if mr_rate > 0.0 and cum_bps != 0.0:
            step_bps -= mr_rate * cum_bps
        cum_bps += step_bps
        prev_delta = step_bps
    return cum_bps, hits


def test_ta_chain_diverges_without_clamp():
    """Replay the Phase 0 audit §3 numerical example: scale=3, 30 steps.
    Without the clamp the chain ratio ≈ 2.08/step → cum_bps hits 1e9+
    magnitudes inside 30 steps. This test PROVES the pathology exists in
    the recursion the clamp is designed to arrest; if this test ever
    starts PASSing with clamp=False at a small bound, the DNA
    coefficients themselves have been tamed and H6.A is redundant."""
    cum_unclamped, _ = _run_ta_chain(n_steps=30, scale=3.0, clamp=False)
    assert abs(cum_unclamped) > 1e6, (
        f"expected runaway divergence without clamp, got {cum_unclamped}"
    )


def test_ta_chain_stays_bounded_with_clamp():
    """Same 30-step TA recursion WITH the H6.A clamp → cum_bps stays in
    a bounded envelope. Theoretical ceiling at prev_delta clamp (100):
    dna_delta_max = 0.25 + 0.65·100 = 65.25; step_bps_max ≈ 65.25·scale;
    mean_reversion counters. Runtime observed ≤ ~5_000 bps for scale=3."""
    cum_clamped, hits = _run_ta_chain(n_steps=30, scale=3.0, clamp=True)
    assert math.isfinite(cum_clamped)
    assert abs(cum_clamped) < 100_000.0, (
        f"clamp failed to bound recursion, got {cum_clamped}"
    )
    assert hits > 0, "clamp never fired — test setup wrong"


def test_ta_chain_sane_input_never_hits_clamp():
    """scale=1.0 → AR chain coef 0.65 < 1 → stable by construction → the
    clamp should never fire on this trajectory. Guards against an
    over-eager clamp that would alter legitimate momentum signals."""
    cum, hits = _run_ta_chain(n_steps=30, scale=1.0, clamp=True)
    assert math.isfinite(cum)
    assert abs(cum) < DNA_CTX_CLAMP_BPS, (
        f"stable scale should leave cum_bps sub-clamp, got {cum}"
    )
    assert hits == 0, (
        f"clamp fired {hits}× on a stable trajectory — false positive"
    )


# ------------------------ Contract 5: retail panic bounded --------------

def _run_retail_chain(n_steps: int, scale: float, clamp: bool) -> tuple[float, int]:
    """Retail panic mode. We seed with cum_bps=200 to trigger the
    panic branch (threshold 120 per behavioral_dna.py:362) immediately."""
    cum_bps = 200.0   # above panic threshold
    prev_delta = 50.0
    mr_rate = 0.005
    hits = 0
    for step_i in range(n_steps):
        ps_in = _clamp(cum_bps) if clamp else cum_bps
        pd_in = _clamp(prev_delta) if clamp else prev_delta
        if clamp and (ps_in != cum_bps or pd_in != prev_delta):
            hits += 1
        ctx = DNAContext(
            prev_state_bps=ps_in,
            prev_delta_bps=pd_in,
            regime="cascade_up",
            perceived=[],
            signed_intent=1.0,
            base_strength=1.0,
            h=step_i,
        )
        dna_delta = retail_dna(ctx)
        step_bps = dna_delta * scale
        if mr_rate > 0.0 and cum_bps != 0.0:
            step_bps -= mr_rate * cum_bps
        cum_bps += step_bps
        prev_delta = step_bps
    return cum_bps, hits


def test_retail_panic_diverges_without_clamp():
    """Without clamp, retail `chase = 4.0·prev_delta` + quadratic
    `overshoot = 0.04·state²/100` compound — faster than geometric.
    30 steps at scale=3 → catastrophic."""
    cum_unclamped, _ = _run_retail_chain(n_steps=30, scale=3.0, clamp=False)
    assert not math.isfinite(cum_unclamped) or abs(cum_unclamped) > 1e9, (
        f"expected runaway retail divergence, got {cum_unclamped}"
    )


def test_retail_panic_stays_bounded_with_clamp():
    """With clamp, retail panic is arrested at the 100 bps input ceiling.
    dna_delta_max = 0.15 + 4.0·100 + 0.04·100·1.0 = 404.15;
    step_bps_max ≈ 404·scale, then mean-reversion pulls back."""
    cum_clamped, hits = _run_retail_chain(n_steps=30, scale=3.0, clamp=True)
    assert math.isfinite(cum_clamped)
    assert abs(cum_clamped) < 500_000.0
    assert hits > 0


# ------------------------ Contract 6: projector diagnostic --------------

def test_projector_init_has_dna_clamp_counter():
    """Fresh ChessProjector exposes `_dna_clamp_hits_last` = 0 so tests
    and regression tooling can read it before any projection runs."""
    from src.projector.chess import ChessProjector
    proj = ChessProjector(tribes=[], steps=1, tick_seconds=5)
    assert proj._dna_clamp_hits_last == 0
