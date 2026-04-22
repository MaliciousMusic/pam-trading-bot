# WS3 H2 Phase 0 — Prediction → Realized Path Audit

**Scope:** Per Decision D2 of `WS3_SCOPE.md`, trace one forecast
end-to-end through the prediction → realized comparison path. Verify
units and scaling match at each step. Rule out structural bugs before
proposing any floor change to `MIN_GAIN`.

**Method:** Static code read — no sprint executed for this audit.
Every line citation below was verified against the on-disk source as
of commit `9cdd1dd`.

**Verdict:** **SUSPECTED BUG — two candidates identified, neither of
which is a floor-binding issue.** The observed `last_correction=0.25`
on disk is almost certainly a clamp artifact, not a magnitude
measurement. A MIN_GAIN change would be addressing a symptom of a
calibrator-logic bug, not the binding constraint.

Recommendation: **shelf the floor-change intervention (§4.4 Options
A/B/C)**. Fix the calibrator fit-target first, then re-measure the
ratio distribution and re-decide whether a floor change is still
needed.

## 1. Trace — forward (projector → anchor emission)

Single conceptual tick at `tick_seconds=1`, `projector_steps=250`,
regime = `trend_up` (multiplier = M), all 24 tribes voting.

| # | Location                                  | Quantity emitted                              | Units   | Notes |
|:--|:------------------------------------------|:----------------------------------------------|:--------|:------|
| 1 | `chess.py:292`                            | `effective_gain = bps_per_unit × M`           | bps/unit| One-shot product at start of `project()` |
| 2 | `chess.py:478`                            | `base_strength_pre = conviction × sk0 × effective_gain` | bps (per tribe) | `sk0` is calibrator skill multiplier ≈ 1.0 |
| 3 | `chess.py:563`                            | `base_strength = base_strength_pre × exp(-h·dt/hs)`     | bps     | Horizon-decayed per step |
| 4 | `chess.py:581`                            | `dna_delta = dna_fn(ctx)`                     | bps/step| DNA uses `base_strength` and regime label |
| 5 | `chess.py:598`                            | `step_bps = (dna_delta + event_bps) × depth_scale`      | bps/step | |
| 6 | `chess.py:604`                            | `cum_bps += step_bps` → `curve.append(cum_bps)`         | cumulative bps | |
| 7 | `chess.py:760-762`                        | `steps_bps[h] = Σ weight·curve[h] / Σ weight` | cumulative bps (consensus) | Weighted across tribes |
| 8 | `chess.py:769-770`                        | `steps_bps = [b × conviction_boost for b in steps_bps]` | cumulative bps | `conviction_boost ∈ [1.0, 1.5]` |
| 9 | `core/engine.py:186`                      | `cumulative_by_step = list(forecast.steps_bps)`         | cumulative bps | Engine picks up boosted curve |
|10 | `core/engine.py:199`                      | `bps_at = cumulative_by_step[step_idx - 1]`   | cumulative bps | `step_idx` 1-based |
|11 | `core/engine.py:200`                      | `predicted = price × (1 + bps_at / 10_000)`   | USD     | Anchor's predicted future price |
|12 | `core/engine.py:204-206`                  | emit `{horizon_seconds = step_idx × tick_seconds, predicted_price, expected_move_bps=bps_at}` | — | Anchor record fed into `ForecastAuditor.record_tick` |

**Regime multiplier appears exactly once (row 1).** Not re-applied in
any subsequent step. ✓

**`conviction_boost` appears exactly once (row 8)** on the *predicted*
side, after the consensus average. No matching application on the
realized side.

**`sk0` (calibrator skill)** appears exactly once (row 2). Typical
value ≈ 1.0 per the tribe_skill.json readout (all tribes at ~0.50 hit
rate → skill multiplier pinned near 1.0).

**Kernel gain `bps_per_unit_pressure`** appears exactly once (row 1,
bundled into `effective_gain`). ✓

## 2. Trace — resolution (market → auditor → calibrator)

| # | Location                                   | Quantity                                   | Units   | Notes |
|:--|:-------------------------------------------|:-------------------------------------------|:--------|:------|
| A | `backscore.py:157`                         | `predicted_move_bps = (predicted_price − entry_price) / entry_price × 10_000` | bps | Reconstructs `bps_at` exactly from row 11. |
| B | `backscore.py:158`                         | `realized_move_bps = (realized_price − entry_price) / entry_price × 10_000` | bps | Raw market move over `horizon_s`. |
| C | `backscore.py:159`                         | `signed_error = (predicted_price − realized_price) / realized_price × 10_000` | bps | Not used downstream for calibration. |
| D | `kernel_calibrator.py:112`                 | filter: `abs(rec.expected_move_bps) < MIN_PRED_BPS (=0.5)` → drop | — | **Selection filter.** |
| E | `kernel_calibrator.py:114`                 | `pairs.append((expected_move_bps, realized_move_bps))` | (bps, bps) | `p = expected`, `r = realized` |
| F | `kernel_calibrator.py:130`                 | `ratios = [r / p for p, r in pairs if p != 0]` | dimensionless | **Signed ratio.** |
| G | `kernel_calibrator.py:133-138`             | `correction = median(ratios)`               | dimensionless | Robust central tendency. |
| H | `kernel_calibrator.py:141`                 | `correction = max(0.25, min(4.0, correction))` | — | **Hard clamp.** |
| I | `kernel_calibrator.py:153`                 | `target = max(MIN_GAIN, min(MAX_GAIN, gain × correction))` | bps/unit | Floor-clamp to `MIN_GAIN=1.0`. |
| J | `kernel_calibrator.py:154`                 | `gain ← gain + α · (target − gain)`         | bps/unit | EWMA update toward `target`. |

Dimensional consistency: **✓ clean.** Both predicted and realized
moves are bps relative to the same `entry_price`, both use
`× 10_000 / entry_price` on the same price basis. No unit error.

Anchor horizon alignment: **✓ clean.** Anchor's `target_ts = tick_ts +
step_idx × tick_seconds`, matching the horizon index used on the
prediction side (`cumulative_by_step[step_idx − 1]`, 0-based array /
1-based step).

## 3. Pair-check against §4.2 audit checklist

Cross-checking each bullet from `WS3_SCOPE.md §4.2 Phase 0`:

| Check | Result |
|:------|:-------|
| Regime multiplier applied exactly once on prediction, never on realized | **✓ Pass** — row 1 only |
| `conviction_boost` applied to prediction, not to realized comparison   | **⚠ Fail** — row 8 boosts predicted; rows A/B leave realized untouched. Predicted is inflated by up to 1.5×. |
| Off-by-one: `step_idx − 1` vs `step_idx × tick_seconds`                 | **✓ Pass** — array index is 0-based, horizon time is `step_idx × dt_s`. Consistent. |
| Both bps quantities normalized to same price basis                     | **✓ Pass** — both use `entry_price` (the tick's `last_price`). |
| Label vs horizon consistency                                           | **⚠ Cosmetic** — at `tick_seconds=1`, label `"15s"` now means `step_idx=1 → 1s horizon`. Display mismatch only; does not affect the predicted/realized math (same horizon used on both sides). Fix alongside §8 anchor-spec normalization. |

## 4. New finding — calibrator fit target is a signed ratio, not a magnitude

**This was not in the §4.2 audit checklist. Surfaced by walking the
code.**

### 4.1 The observation

`kernel_calibrator.py:130` computes
```python
ratios = [r / p for p, r in self._pairs if p != 0]
```

Both `r` and `p` are **signed** bps. So:

- Directionally correct anchor (same sign): `r/p > 0` (typically in
  `[0.1, 2.0]` for reasonably calibrated magnitude).
- Directionally wrong anchor (opposite sign): `r/p < 0` (in
  `[−2.0, −0.1]`).
- Flat realized (`r ≈ 0`): ratio ≈ 0.

The median over a mixed-sign distribution does not measure magnitude
bias. It measures a conflation of (a) directional hit rate and
(b) magnitude bias.

### 4.2 Why this matters given WS1

WS1 measured **directional hit at 20 s = 44.5 %** (N=236). If that
44.5 % holds across the ~1595-sample calibrator window on disk, then
roughly half the `r/p` ratios are negative. Sorted ratios look
something like:

```
[−2.0, ..., −1.0, ..., −0.3, −0.1, 0.0, +0.1, +0.3, ..., +1.0, ..., +2.0]
                                          ^
                                         median sits near zero
```

The **unclamped** median is near zero — or worse, slightly negative if
wrong-direction anchors have larger magnitudes than correct ones (which
WS1 §4 showed is the case under counterfactual extension: mean
frictionless mark-move is negative at every hold horizon).

Then `kernel_calibrator.py:141` applies
```python
correction = max(0.25, min(4.0, correction))
```

**This floor-clamps zero-or-negative corrections up to +0.25.** The
on-disk `last_correction = 0.25` is **consistent with any raw median
in `(−∞, +0.25]`**. We cannot tell from the persisted state whether
the true magnitude ratio is 0.25, 0.05, or −0.3.

### 4.3 Downstream implication

Because `correction = 0.25` is a clamp value, the chain:

```
target = gain × correction = 1.57 × 0.25 = 0.393
gain ← max(MIN_GAIN=1.0, 0.393) = 1.0  [floor clamp at line 153]
```

is being driven by a **measurement the calibrator cannot actually
make**, not by a real magnitude error. The calibrator has been
continuously "convinced" that predictions are 4× too large (or worse)
— but its own formula has no mechanism to distinguish "4× too large"
from "half the time the direction is simply wrong."

The H2 §4 hypothesis — "calibrator is pinned at floor because the true
magnitude ratio is < 1.0" — is only **possibly** correct. We have no
direct evidence. The more defensible statement is: "the calibrator's
fit target is dominated by directional-miss penalty, not magnitude
error, and the floor is binding as a side-effect."

## 5. Second finding — `conviction_boost` is a unidirectional inflator

### 5.1 Mechanics

`chess.py:740`:
```python
conviction_boost = 1.0 + harvest_scale × harvest_mass_ratio
```

Typical range: `[1.0, 1.5]`, with `harvest_scale` configured around
0.5 and `harvest_mass_ratio` in `[0, 1]`.

Applied once (`chess.py:769-770`) to the consensus `steps_bps` after
the weighted average across tribes. The boosted curve is what the
engine passes to the auditor.

### 5.2 Where the asymmetry comes from

Predicted side: picks up the boost (row 8 of §1).

Realized side (`backscore.py:158`): raw market price change. The
market does not know about PAM's `harvest_mass_ratio`. Boost is
never "cashed in" on the realized side.

### 5.3 Magnitude of effect

Per-tick: a 1.25× typical boost inflates predicted magnitude by 25 %.
The calibrator's fit target (even fixed per §4 of this document) will
then read systematically low by a factor of 1/boost on any tick where
boost > 1.0.

**Much smaller than the observed 4× apparent mismatch** — this alone
cannot explain `last_correction = 0.25`. But in combination with the
signed-median issue (§4), this is a second source of downward pressure
on the fitted gain that the calibrator cannot disentangle.

### 5.4 Design question

Is `conviction_boost` intended as:

- **(a) Sizing amplifier** — "we believe this trade is high-conviction
  so we'll trade a bigger size, but our *prediction* of the market
  move is unchanged." In this framing, the boost should live in the
  `decision_engine` (sizing side), not in the projector's
  `steps_bps`. Current code violates this separation.
- **(b) Prediction amplifier** — "when harvest mass is high, the
  council *predicts* a larger realized move." In this framing, the
  boost is a legitimate part of `expected_move_bps` and the calibrator
  should observe a matching uptick in realized magnitude during
  high-harvest conditions. The fact that it doesn't implies the
  harvest-mass feature is uninformative about actual market
  magnitude — which would argue for removing the feature, not
  fixing a calibration issue.

Either reading leads to a code change, not a calibration-floor change.
§4.4 Option A (lower MIN_GAIN) does not address this finding.

## 6. Non-findings (audit checks that came back clean)

- **Off-by-one on anchor array indexing.** Clean. Both sides consistent.
- **Units on bps computation.** Clean. Both sides use `×10_000 / entry_price`.
- **Entry price basis.** Clean. Both sides use the tick's `state.last_price`.
- **Regime multiplier double-application.** Clean. Applied once at
  `effective_gain`, not re-applied anywhere.
- **Selection filter on `MIN_PRED_BPS=0.5`.** Not a bug, but a bias
  source worth noting: small-magnitude predictions are excluded from
  calibration entirely. Should be flagged in Phase 1 ratio-distribution
  analysis; not a structural defect.
- **Skill multiplier `sk0`.** Applied once. Values currently ≈ 1.0
  across all tribes (tribe_skill.json EWMAs in [0.498, 0.502]); not a
  material contributor either way.

## 7. Proposed next actions

Given the two findings above, the path forward changes relative to
the original `WS3_SCOPE.md §4.4`:

**Step 1 — Fix the calibrator fit target.** Change
`kernel_calibrator.py:130` from signed-ratio median to a
direction-aware magnitude calibrator. Two candidate forms:

- **Option 1a:** Drop wrong-direction samples from the fit entirely.
  `ratios = [r / p for p, r in pairs if p != 0 and (p > 0) == (r > 0)]`.
  The calibrator then learns "given we called direction right, what
  is our magnitude bias?" Directional hit rate is a separate concern
  (H1/H3 territory). Median over the correct-direction subset is a
  clean magnitude estimate.
- **Option 1b:** Use `|r| / |p|`, unsigned. Mixes correct and wrong
  samples; correct-direction anchors contribute `|r|/|p|` and so do
  wrong-direction ones, but both are positive. This under-penalizes
  wrong-direction calls (a correct prediction of magnitude 5 bps in
  the wrong direction looks the same as a correct prediction in the
  right direction), but it's a clean magnitude-only fit.

Prefer **Option 1a** for Phase 1 measurement — cleaner semantics,
naturally pairs with "signal-quality is directional accuracy" framing
from WS1. Phase 1 is a measurement-only change (no mechanism change
that bites in production until intervention phase).

**Step 2 — Re-measure ratio distribution** (`WS3_SCOPE.md §4.2 Phase 1`)
using the corrected fit target. Will yield an honest answer to
"is realized magnitude systematically smaller than predicted?" Until
Step 1 ships, Phase 1 ratio measurements are the same clamped quantity
and carry no new information.

**Step 3 — Decide on `conviction_boost` at projector layer.** Either:

- Move it out of `chess.py:769-770` and into `DecisionEngine` sizing
  logic (cleaner separation, no calibration asymmetry).
- Keep it in the projector but require the calibrator fit target to
  divide out the per-tick boost value (requires persisting
  `conviction_boost` on the auditor record).
- Remove the feature if Phase 1 shows no high-boost/high-realized
  correlation.

This is independent of Step 1/2 but should land before any MIN_GAIN
change, since boost-asymmetry confounds the calibrator even after the
signed-median fix.

**Step 4 — Revisit H2 §4.4 Option A (lower `MIN_GAIN`).** Only after
Steps 1–3 ship and Phase 1 re-measurement shows a **clean magnitude
ratio** that genuinely sits below 1.0 with the floor binding. Without
that evidence, lowering the floor is addressing a symptom whose cause
has not been diagnosed.

> **Note (reviewer, 2026-04-22):** Post-H2 re-measurement may
> necessitate reordering or dropping H1 options A/B/C; revisit after
> snapshot. The conf-formula analysis in `WS3_SCOPE.md §3` assumed
> predictions were correctly magnituded. If Step 1 shows predictions
> are honestly smaller than assumed, the `directional` term in the
> conf formula behaves differently and H1's priority / option set
> may change. Do not preemptively restructure H1 based on speculation
> — defer the decision to post-H2 snapshot.

## 8. Verdict & handoff

**Verdict:** **suspected-bug, two candidates** (signed-median fit
target, unidirectional `conviction_boost`). Either is sufficient to
explain a large portion of the observed 4×-apparent mismatch; in
combination, they can explain all of it without any genuine
magnitude-calibration gap.

**Pause point:** Per Decision D2 and the reviewer's order-of-operations
instruction, pause here for review before any mechanism change
ships. The scope doc's §4.4 intervention options (lower MIN_GAIN,
decoupled shrinkage, ATR-driven) are **all shelved** pending the
Step 1 calibrator fix.

**Reviewer resolutions (2026-04-22):**

| # | Decision |
|:--|:---------|
| R1 | **Fit target = Option 1a** (drop wrong-direction samples). 1b lumps directional misses into magnitude fit; 1a keeps the calibrator on scale-only. Log both the correct-direction ratio distribution and the dropped-sample fraction — the dropped fraction cross-checks WS1's 44.5 % directional hit rate, divergence flags a selection bug. |
| R2 | **Move `conviction_boost` to `DecisionEngine` as a sizing amplifier.** Market doesn't care about harvest_mass_ratio; realized price is realized price. Projector outputs should be honest predictions. Implementation: pick one of *(a)* a distinct post-shrink amplifier step with clear labeling (minimum-diff), or *(b)* invert to "non-contrarian shrink" to preserve stack-uniformity in [0, 1]. Report choice + reasoning. Also verify the Task 4c Path-Integrity Cap (`driver_skill_avg < 0.70 → cap at 1.0`) interacts correctly with a >1.0 multiplier intentionally — do not inherit accidentally. |
| R3 | **Separate commits with attribution discipline.** Commit calibrator fix → replay day → snapshot → commit boost relocation → replay day → snapshot. 2000-sample rolling window contaminates attribution if bundled. If overnight cost is prohibitive, use 500-tick sprints for attribution, full days for final validation; harness supports both. |
| R4 | **Defer H1 reordering until post-H2 snapshot.** See §7 note above. |

**Standing instruction (applies to all remaining hypotheses):** For
each of H1 and H3, reserve time for a structural path audit before
any intervention. This H2 audit caught two bugs where we expected one.
Audit-before-tune is now standard procedure for the rest of WS3, not
a one-off.

*Audit completed 2026-04-22 on `feat/tensor-engine` at commit
`9cdd1dd`. Reviewer resolutions folded in same day. No code changed
by this audit. Next action: implement Option 1a in the calibrator,
commit, run attribution sprint, pause before `conviction_boost`
relocation.*
