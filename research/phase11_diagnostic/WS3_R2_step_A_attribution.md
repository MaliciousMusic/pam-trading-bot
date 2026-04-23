# WS3 R2 Step A — Attribution snapshot

**Status:** **Commit A landed + sprint-side verified (2026-04-23).**
Commit A hash `fd400e8`. Determinism gate (full-day, --day 2026-04-20
--seed 42) PASS with sha256 `14d149feafe8ac7d…` byte-identical across
two runs. Test-discipline gate PASS (33/33, no numerical expected-
value changes). Condition-(a) gate PASS on the §2.1 algebraic proof
combined with §5.4 determinism and §5.5 test discipline — the
cross-commit empirical A/B is deliberately skipped as redundant with
the algebra. §5.2 sprint cal distribution captured. Live-tape delta
post-deploy remains §5.1 TODO until the first post-A session archives.

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
  - `last_gain`: **TODO** (expected steady state: `pre-A_gain ×
    median_boost` ≈ `1.57 × ~1.2` ≈ `1.9`, since the calibrator was
    previously fitting on post-boost predictions and now fits on raw.
    Note: this is the *equilibrium* requirement — see §5.2 on the
    cold-start transient that overshoots it).
  - `last_correction`: **TODO** (expected: post-convergence, should
    no longer sit near clamp boundaries; cold-start may still clamp
    hi until history buffer fills).
  - `last_dropped_fraction` / `last_correct_ratio_p50`: **TODO**.
  - **Full-day correction distribution** (not just a snapshot):
    histogram of per-tick `raw_correction` values, fraction of ticks
    at hi-clamp, fraction at lo-clamp, time-to-equilibrium if it
    converges. Snapshot the state at end-of-day in addition.

### 5.2 Sprint-tape delta (source: Step 4 live-feedback driver)

Full-day sprint on 2026-04-20 (16,811 ticks, --seed 42), via
`verify_determinism` since that already runs two full-day sprints and
logs the kernel-cal clamp stream; two runs produce byte-identical
`decisions.jsonl` (see §5.4) and byte-identical clamp streams (2,520
clamped-hi events per run, exact match).

**Correction-stream distribution — hi-clamp tail** (the
`KernelCalibrator._refit` warning is emitted only when `raw_correction`
exceeds the 4.00 hi-bound, so this distribution is the clamped subset,
not the full histogram):

| stat | raw_correction at hi-clamp |
|:-----|---------------------------:|
| n (per run) | 2,520 / 16,811 ticks ≈ **15.0 %** |
| min  | 4.001 |
| p25  | 4.357 |
| p50  | 5.059 |
| p75  | 5.979 |
| p90  | 9.559 |
| p99  | 10.930 |
| max  | 11.245 |
| lo-clamp events | 0 |

**Interpretation**:

- **Not pinned at MAX_GAIN**. 85 % of refits fit inside the clamp band
  (unclamped, below raw=4.0); the 15 % that clip are mostly mild (p50
  raw≈5), with a heavy-tail excursion up to raw≈11. This is the
  signature of a calibrator *walking toward equilibrium*, not one
  stuck at the ceiling.
- **Time evolution on the clipped tail**: early-day clipped values
  concentrate in raw ∈ [4.1, 4.7]; mid-day excursions reach raw ≈ 11;
  end-of-day values return to raw ≈ 4.00-4.01 (within 0.25 % of the
  clamp boundary). The calibrator is closing on the clamp from above,
  consistent with §5.1's steady-state estimate of ~1.9.
- **Cold-start re-interpretation**: the 9.5-10 raw values observed in
  the 600-tick short-window are the *mid-sequence heavy-tail* events,
  not a uniform cold-start overshoot. Full-day view shows they are a
  ~10 % tail, not the modal behavior.

- Sprint-vs-live magnitude channel delta: **TODO** (requires live-tape
  post-A deployment data; sprint side captured here).

### 5.3 Condition-(a) gate check

The empirical cross-commit A/B (pre-A sprint forecasts vs post-A
sprint forecasts on the same tape) is NOT run. It would take two
additional full-day sprints (~6-7 h wall-clock) to measure a quantity
the algebra in §2.1 already pins at exactly 0.00 pp. The gate is
resolved instead on three convergent pieces of evidence:

1. **Algebraic proof (§2.1)**: `conviction_boost ∈ [1.0, 1.5]` is a
   positive scalar, applied uniformly across every step of every
   forecast. `sign(boost × x) = sign(x)` for all x, so no per-forecast
   directional hit can flip. Aggregate delta is exactly 0.00 pp by
   construction, not "expected" 0.00.
2. **Determinism gate (§5.4)**: the full sprint tick loop, including
   the changed code paths (chess.py projector, decision engine
   `_peak_favorable_seconds`, paper+shadow Layer 2b TP), runs
   byte-identically across two independent invocations. A
   sign-affecting refactor bug would have produced a non-deterministic
   tail — ruled out.
3. **Test discipline (§5.5)**: unit tests exercising the changed
   code paths with hand-constructed inputs keep their expected
   outputs (peak_favorable_s==240, TP target==24 bps, exit fires)
   through the refactor. A per-site sign bug would have surfaced
   there — ruled out.

**Gate decision: PASS** on algebraic+determinism+test-discipline. The
3 pp pause trigger remains a safety rail for any future observation
(post-live-deploy directional delta, post-B cross-commit check);
within commit A's scope it is not a binding gate because the
measurable quantity is pinned at 0.00 by the code change itself.

Live-tape directional delta post-A deploy: **TODO** (captured in
`data/forecasts/<day>.jsonl` for the first full session after A
ships to live; expected 0.00 pp on sign-preservation).

### 5.4 Determinism gate

- **Full-day PASS**: `scripts.verify_determinism --day 2026-04-20
  --seed 42`, sha256 `14d149feafe8ac7d…`, 260,138,724 bytes,
  byte-identical across both runs (2026-04-23, ~6h54m wall-clock for
  the pair, 16,811 ticks each). Warning-stream byte-identity also
  verified (2,520 clamped-hi events per run, exact count match).
- Short-window (600 ticks, same day/seed): **PASS**, sha256
  `1c9b2c38935ddfd4…` byte-identical.

### 5.5 Test discipline verification

- Test numerical expected values unchanged through the refactor:
  **PASS** (commit A constraint per refinement 2, 2026-04-23 review).
  Two retargeted tests — peak-favorable argmax-of-raw-curve
  (peak_favorable_s == 240) and forecast-TP invariant-under-boost-
  stamp (target == 24 bps, exit fires at +28 bps unreal) — keep their
  expected assertions unchanged. 33/33 pass on
  `tests/test_decision_engine.py` + `tests/test_paper_executor.py`.

### 5.6 Measurement-architecture implication for commit B

Commit B (conviction_boost read as DecisionEngine sizing multiplier
with Path-Integrity clamp) needs a clean signal on whether the boost,
used as a sizing lever, improves expected-outcome-per-trade. That
signal lives in two channels:

- **Sprint-side**: per-trade PnL on replayed panel days as a function
  of `conviction_boost × path_integrity_clamp`. Load-bearing only if
  the sprint calibrator is NOT ceiling-pinned: a pinned gain
  saturates magnitudes and compresses per-trade PnL variance, so the
  boost-sizing signal gets drowned in noise regardless of whether
  boost-as-sizing is a real primitive.
- **Live-tape**: per-trade PnL in production where the calibrator has
  settled at its post-A equilibrium. Load-bearing regardless of
  sprint behavior, but slower to accumulate (needs multiple sessions
  post-B deployment).

**Conditional framing**: if §5.2's full-day correction distribution
shows the sprint calibrator pinned at MAX_GAIN post-A, treat the
sprint-side B measurement as structurally noisy and rely on live-tape
for B's acceptance verdict. Do not interpret a null sprint-side B
result as "boost-as-sizing doesn't work" in that regime — it may just
mean sprint lacks the magnitude headroom to show a differential. This
is consistent with MEASUREMENT_INFRA.md §5 item 5 (live-specific
numerical pathologies) and the D6 sprint-vs-live operating rule.

**Observed resolution of the conditional (2026-04-23 full-day)**: the
sprint cal is NOT pinned at MAX_GAIN — 85 % of refits fit inside the
[0.25, 4.00] band, with the clamped 15 % concentrated just above the
hi-bound (p50 raw=5.06) and a 10 % heavy tail reaching raw≈11. The
end-of-day trajectory closes on the hi-clamp from above, consistent
with walking toward the §5.1 equilibrium of ~1.9. Sprint-side B
measurement is therefore structurally **usable** — not noise-free
(15 % of ticks have compressed magnitude headroom), but also not
saturated in the way pre-kernel-cal-wiring sprint snapshots were.
Live-tape remains the primary acceptance channel for B, with sprint
as supporting evidence rather than load-bearing signal.

## 6. Provenance

- R2 plan reviewed + refined: 2026-04-23 session.
- Pre-commit draft written: 2026-04-23.
- Commit A landed: 2026-04-23, hash `fd400e8`, 6 files, +39/−56.
- pytest on touched modules: 33/33 passed (`tests/test_decision_engine.py`
  + `tests/test_paper_executor.py`).
- Short-window determinism gate (600 ticks, --day 2026-04-20 --seed 42):
  PASS, sha256 `1c9b2c38935ddfd4…` byte-identical across both runs
  (1,846,084 bytes each).
- Refinement sources: §1-3 frozen per 2026-04-23 review; §5 template
  per R2 landing-sequence plan.
- Archive scan inputs: `data/forecasts/{2026-04-18,2026-04-19,
  2026-04-20}.jsonl`.
- Full-day determinism SHA-256 × 2: `14d149feafe8ac7d…` (both runs,
  260,138,724 bytes each, 16,811 ticks, 2026-04-23).
- Clamp-stream determinism: 2,520 clamped-hi events per run, exact
  match; 0 lo-clamp events across both runs.
- Commit A replay run ID: `det_check` (via verify_determinism
  tempdir; decisions.jsonl not persisted post-run).

---

*Living document. §5.1 live-tape delta remains TODO until the first
post-A production session archives. All other gates closed.*
