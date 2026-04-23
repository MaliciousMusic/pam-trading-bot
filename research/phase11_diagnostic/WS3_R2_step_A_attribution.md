# WS3 R2 Step A — Attribution snapshot

**Status:** **Pre-commit-A draft (2026-04-23).** Pre-commit sections
(§1 framing, §2 directional expectation, §3 H1-input observation) are
finalized. §5 post-replay attribution sections are placeholders to be
populated after commit A lands and is replayed against the live-tape
and sprint panels. Do not read this file as a completed attribution
until the status banner changes.

> **What R2 does NOT fix.** R2 removes a known upstream prediction-
> side inflator from the calibration pipeline. It does not improve
> directional hit rate, does not lift aggregate signal above the WS1
> baseline, and does not constitute progress toward the 52 %
> acceptance bar. Its value is making subsequent H1/H3 measurements
> clean.

## 1. Scope of commit A

Commit A removes the unidirectional prediction-side inflation of
`steps_bps` by `conviction_boost` inside `ChessProjector.project` and
the four downstream de-boost sites that exist only to compensate for
that inflation. Touches: `src/projector/chess.py` (delete lines
769-770), `src/decision/engine.py` (simplify `_peak_favorable_seconds`),
`src/execution/paper.py` (Layer 2b TP, stop dividing by boost),
`src/execution/shadow.py` (mirror), plus two test files retargeted to
raw-consensus input semantics with **no numerical changes to expected
outputs** (refinement 2, 2026-04-23 review).

`conviction_boost` **stays on the Forecast and TradeIntent objects**
as a signal; commit B reads it in `DecisionEngine` as a sizing
multiplier (with Path-Integrity clamp). Commits A and B are attributed
separately per R3.

## 2. Directional expectation — primary

### 2.1 Algebraic invariance (primary result)

Commit A cannot move `directional_hit` on any individual archived
forecast. Proof:

```
  post-boost steps_bps[h]  = boost × pre-boost steps_bps[h]           (chess.py:769-770)
  expected_move_bps        = Σ kernel[h] × steps_bps[h]               (chess.py:794)
  ⇒ post-boost emove       = boost × pre-boost emove                  (boost factors out)
  ⇒ sign(post-boost emove) = sign(boost) × sign(pre-boost emove)
                           = (+1) × sign(pre-boost emove)              (boost ∈ [1.0, 1.5] > 0)
                           = sign(pre-boost emove)
```

`conviction_boost` is a positive scalar applied uniformly to every
step. Positive-scalar multiplication cannot flip a sign. Therefore
`sign(expected_move_bps)` is bit-identical pre- and post-commit A.
`directional_hit = sign(emove) == sign(realized)` is a function of
sign only, so aggregate directional delta is **exactly 0.00 pp by
construction**, stronger than the conservative <1 pp expectation in
the R2 plan. The condition-(a) >3 pp pause trigger remains an
**implementation-correctness safety rail**: any non-zero delta
indicates a bug in the five-file refactor (e.g., a typo in the
`/ boost` removal, an accidentally-dropped forecast branch),
not a real mechanism effect.

### 2.2 Empirical companion — archive hit-rate signatures

Panel = 04-18/19/20 (04-21 excluded pending divergence audit). Source:
`data/forecasts/<day>.jsonl` resolved-anchor archive, written by the
live `TradingEngine` (production calibrator, gain=1.57). The archive
does not persist `conviction_boost` per-anchor, so a direct boost-vs-
sign correlation cannot be computed; what the archive does show is the
pattern that boost would produce if it were magnitude-only (uniform
scaling on both signs) versus directional (concentrated on one sign).

**1m label (primary 52 % acceptance label), N=18,126:**

| stat | long (N=10,496) | short (N=7,630) |
|:-----|---------------:|---------------:|
| hit rate | 50.56 % | 48.58 % |

**Hit rate by `|expected_move_bps|` decile (1m):**

```
  dec  |e|_lo   |e|_hi    long_N  long_hit  short_N  short_hit
   d1    0.000    0.020    1030    50.10%      782    47.57%
   d2    0.020    0.082     981    46.69%      832    45.67%
   d3    0.083    0.185     938    47.23%      874    50.11%
   d4    0.185    0.300    1090    51.38%      723    50.21%
   d5    0.300    0.437    1079    53.75%      734    46.19%
   d6    0.437    0.653     961    52.24%      851    50.76%
   d7    0.653    0.949    1109    54.10%      704    48.30%
   d8    0.949    1.427    1094    49.73%      718    49.16%
   d9    1.427    2.757    1035    50.14%      778    47.56%
   d10   2.758   82.666    1179    49.62%      634    50.47%
```

No monotonic magnitude→hit trend in either direction. Long/short hit
rates are within 2-3 pp at every decile. The d10 tail (max `|emove|`
82 bps) hits at 50 % long / 50 % short — no directional concentration
where boost-inflation would have been strongest. This is the signature
of a magnitude-only scaler on top of a near-random sign surface.
Consistent with §2.1 and inconsistent with "boost was doing directional
work."

**1m aggregate:** 49.73 % hit (9,014/18,126). Near random.

## 3. H1 input — short-horizon anti-signal (orthogonal to R2)

Surfaced by the same archive scan. **Not in R2's scope**; recorded here
because the analysis is fresh.

**15s label, N=18,505:**

- long (N=11,067): 39.95 % hit — **10.05 pp below random**
- short (N=7,438): 42.04 % hit — 7.96 pp below random

The 15s surface is **sub-random in both directions** with adequate
statistical power (each side N > 7k; the standard error on a
proportion at p=0.5 is `sqrt(0.25/N) ≈ 0.6 %` for N=7,438). The ~8-10
pp deficit is many σ away from the null hypothesis of random
prediction. WS1 had noted poor short-horizon performance; this is a
stronger version of the same finding with a specific numerical claim
at the 15s horizon.

Pairing 15s with the 1m result from §2.2 (49.73 %, near random):

| label | hit rate | interpretation |
|:------|:--------:|:---------------|
| 15s | ~40 % both sides | **anti-signal** — the forecast is systematically on the wrong side of the near-term move |
| 1m | ~50 % both sides | **null signal** — forecast is directionally uninformative |

These are likely **two distinct phenomena** with potentially different
mechanisms and fixes. Candidate sketches for H1 scope development:

- **Anti-signal at 15s** could reflect microstructure-frequency mean
  reversion that the council-consensus regime is on the wrong side of
  — council reads a short-window impulse as "trend continuation" when
  the immediate reaction is "fade." If so, a sign-flip operator on
  short-horizon predictions would convert 40 % → 60 %. That is
  speculative and not a conclusion.
- **Null signal at 1m** is what the H1 scope draft is currently framed
  around (the `signal_quality × directional × diversity_mult`
  compounding structure capping confidence ≤ 0.25). This finding does
  not contradict H1's framing but suggests H1 should treat 15s as a
  separate hypothesis branch rather than a smaller-scale version of
  the same problem.

**Recommended H1 scope-doc action** (when H1 activates per PLAN.md
§5.2 Q1): carry this observation forward as a dedicated §H1.x branch
so the measurement framework separates "lift the 1m-label toward
random" from "fix the 15s-label anti-signal." They may need different
interventions.

## 4. Landing sequence (recap — per R2 plan)

1. Audit chip returns verdict on 2026-04-21 divergence.
2. 04-21 panel-inclusion decision (verdict-driven).
3. Commit A (5 files + 2 test files, ~40 lines diff net).
4. Replay A: sprint panel (Step 4 driver) + live-tape snapshot.
5. Condition-(a) gate check: live directional delta ≤ 3 pp.
   **Algebraic floor: 0.00 pp.** Any non-zero indicates a refactor
   bug; pause for audit.
6. Determinism gate A: `scripts.verify_determinism --day 2026-04-20`
   full-day.
7. Commit B (decision/engine.py + test, ~15 lines diff).
8. Replay B: sprint + live.
9. Determinism gate B.
10. R2 closed; H1 scope doc (PLAN §5.2 Q1) is the default next chip.

## 5. Commit A attribution — post-replay (placeholders)

To be populated when commit A lands and the replay sprint completes.
Keeping the structure visible so the attribution template is clear
before execution.

### 5.1 Live-tape delta (source: `data/forecasts/<day>.jsonl`)

- Directional hit rate, per-label and aggregate: **TODO** (expected:
  0.00 pp delta per §2.1).
- `|expected_move_bps|` distribution: **TODO** (expected: median
  shrinks by the mean boost factor, ~15-25 %, across all signs
  symmetrically).
- Option 1a calibrator state on live tape:
  - `last_gain`: **TODO** (expected: ~`1.57 × median_boost` after
    fit window, since the calibrator was previously fitting on
    post-boost predictions and now fits on raw).
  - `last_correction`: **TODO** (expected: shifts, should no longer
    sit near clamp boundaries).
  - `last_dropped_fraction` / `last_correct_ratio_p50`: **TODO**.

### 5.2 Sprint-tape delta (source: Step 4 live-feedback driver)

- Per-day calibrator saturation behavior: **TODO** (expected: still
  saturates `MAX_GAIN=40.0` per MEASUREMENT_INFRA.md §4.1 — sprint
  pressure weakness dominates, not the boost removal).
- Sprint-vs-live magnitude channel delta: **TODO**.

### 5.3 Condition-(a) gate check

- Live aggregate 1m directional delta: **TODO** (gate: pass if
  ≤ 3 pp; algebraic expectation 0.00).
- Per-label directional deltas: **TODO**.
- Decision: **TODO**.

### 5.4 Determinism gate

- `scripts.verify_determinism --day 2026-04-20 --seed 42`
  full-day: **TODO** (SHA-256 byte-identical across two runs).

### 5.5 Test discipline verification

- Test numerical expected values unchanged through the refactor:
  **TODO** (commit A constraint per refinement 2, 2026-04-23 review).
  Any expected-value move flagged here as a "suspicious behavioral
  change" header blocking commit A.

## 6. Provenance

- R2 plan reviewed + refined: 2026-04-23 session.
- Pre-commit draft written: 2026-04-23.
- Refinement sources: §1-3 frozen per 2026-04-23 review; §5 template
  per R2 landing-sequence plan.
- Archive scan inputs: `data/forecasts/{2026-04-18,2026-04-19,
  2026-04-20}.jsonl`.
- Commit A hash: **TODO**.
- Commit A replay run ID: **TODO**.
- Determinism SHA-256 × 2: **TODO**.

---

*Living draft. Remove the pre-commit banner when §5 is populated.*
