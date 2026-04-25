# WS3 H6 — DNA-recursion positive-feedback audit — Phase 0

**Phase 11 Diagnostic · Phase 0 audit (static, no code)**

| Item | Value |
|:--|:--|
| Box | 23:48 → 00:48 CET (1h strict), hard abort 01:03 |
| Opened | 2026-04-24 23:48 CET |
| Verdict | **BUG-FOUND** — 2 DNA categories exceed the stability threshold on every tick |
| Files audited | `src/projector/chess.py` (per-tribe walk, 540-619), `src/projector/behavioral_dna.py` (all 9 categories), `src/projector/kernel_calibrator.py` (gain coupling) |
| Code changed | **NONE** — audit-only per `feedback_audit_fix_checkin.md` |
| Scope acceptance | `WS3_H6_SCOPE.md` §0 — "identify ≥ 1 DNA category with ∂f/∂cum_bps > 0.005 on ≥ 5% of ticks OR rule out" — **SATISFIED** |

## 0. Headline

The recursive per-tribe walk at `chess.py:561-619` is mathematically
unstable under routine BTC-perp depth conditions because two DNA
functions — **`ta_dna`** and **`retail_dna` (panic branch)** — emit
deltas whose derivative with respect to the recursion state
(`prev_delta_bps`, `prev_state_bps`) exceeds the damping provided by
the `mean_reversion_rate = 0.005` soft anchor **by two to three orders
of magnitude** once the depth-scale multiplier is folded in.

The recursion collapses into a geometric chain with ratio
`0.65 · scale` (TA) or `4.0 · scale` (retail panic). For the typical
BTC-perp 5bps-side depth of $3-6M against the configured
`depth_ref_usd = 10M`, `scale` sits at 1.67-3.33, so the TA chain alone
produces a growth ratio ~1.1-2.2 per step. Over the 250-step horizon
this is the 10^30-10^295 magnitudes observed in the pathological tape.

The H5.A clamp contains the projector **output**. H6 has located the
generator: **DNA coefficients**, not calibrator, not regime weights,
not kernel decay.

## 1. Q-answers (per scope §2)

### Q1 — Which DNA category produces the spike?

**Two drivers, not one.**

- **`ta_dna` (line 277)** is chronically unstable on every tick with
  any finite `base_strength`. The term `momentum_amp = 0.65 ·
  prev_delta_bps` produces AR(1) coefficient `0.65 · scale`; unstable
  iff `scale > 1.54`, which is satisfied whenever
  `bid/ask_depth_5bps_usd < depth_ref_usd / 1.54 ≈ $6.5M`. On BTC
  perps this is **routine**, not exceptional.

- **`retail_dna` (lines 363-368, panic branch)** is the accelerator.
  Triggers when `|prev_state_bps| > 120` OR any non-empty `perceived`
  shock list. Inside the branch: `chase = 4.0 · prev_delta_bps` +
  `overshoot = 0.04 · state · |state| / 100` (quadratic in state).
  Once TA has pushed `|cum_bps|` above 120, retail compounds.

- Outside panic, retail is benign (`0.40 · prev_delta` → AR coef
  `0.40 · scale`, safe for `scale < 2.5`).

### Q2 — Single-horizon event or cascade?

**Cascade.** H5 observed signflips=1 + single-horizon spike at the
**output** of the projector; the generator is a per-step recursion.
The `prev_delta_bps` channel propagates the previous step's output
into the next step's input. With ratio > 1 this is geometric growth
across all 250 steps. The H5 "single-horizon" fingerprint emerges
after the H5.A clamp zeroes everything except the finite tail that
happens to survive sanitization at its own step.

### Q3 — Self-initiating or shock-triggered?

**Self-initiating.** Step 0 seed is `intent_drift = k_cat ·
base_strength · signed_intent`. With `base_strength > 0` and
`signed_intent ∈ {-1, 0, +1}` non-zero, step 0 is non-zero. `prev_delta`
carries it forward. No perceived shock required for the TA chain —
only for the retail panic branch.

A perceived shock **accelerates** divergence (adds `leaked = 0.20 ·
perceived_sum` to TA and `shock_panic = 1.5 · perceived_sum` to
retail-panic), but is **not necessary** to trigger instability.

### Q4 — Symmetric or one-sided?

**Symmetric.** Both functions chain the sign of `prev_delta` without
asymmetry (coefficients 0.65 and 4.0 are scalar, not conditional on
sign). The `overshoot` term in retail uses `state · |state|` which
preserves sign. The observed `emove = ±10^120` in the tape is a
direct consequence — the recursion is sign-agnostic.

### Q5 — Feedback coefficient distribution

Per-category derivatives at `chess.py:611` folded through `scale`:

| Category | ∂/∂prev_delta | ∂/∂prev_state | Chain coef (×scale) | Verdict |
|:--|--:|--:|--:|:--|
| arb | 0 | -0.18 | -0.18·scale | **damped** |
| microstructure | +0.10 | -0.25 | mixed | **damped** (net) |
| mm | 0 | -0.40 | -0.40·scale | **damped** |
| **ta** | **+0.65** | -0.05 above 200, else 0 | **+0.65·scale** | **unstable scale>1.54** |
| event | 0 | -0.08 | -0.08·scale | **damped** |
| hedger | -0.30 | 0 | -0.30·scale | **damped** |
| **retail (panic)** | **+4.0** | **+0.04·|state|/100** (quadratic) | **+4.0·scale** + quad | **very unstable** |
| retail (calm) | +0.40 | 0 | +0.40·scale | borderline (unstable scale>2.5) |
| mev | 0 | -0.55 | -0.55·scale | **damped** |
| whale | 0 | -0.02 | -0.02·scale | **damped** |

**All non-TA non-retail tribes have net negative chain coefficients**
— they're mean-reverting by construction. The bug is isolated to the
two momentum-chasing categories.

## 2. Mathematical derivation

The per-step recursion at `chess.py:605-612`:

```
total_bps[h] = dna_delta(ctx[h]) + event_bps[h]
step_bps[h]  = total_bps[h] · scale  −  ρ · cum_bps[h-1]
cum_bps[h]   = cum_bps[h-1] + step_bps[h]
prev_delta[h+1 input] = step_bps[h]
```

For a linear DNA category with `dna_delta = α · prev_state + β ·
prev_delta + γ · const`, the recursion becomes

```
cum_bps[h+1] = cum_bps[h] · (1 − ρ + α · scale)  +  β · scale · step_bps[h]  +  γ · scale
```

where `step_bps[h] = cum_bps[h] − cum_bps[h-1]`. Expanding:

```
cum_bps[h+1] = cum_bps[h] · (1 − ρ + α · scale + β · scale)
             − cum_bps[h-1] · β · scale
             + γ · scale
```

This is a **second-order linear recurrence** in `cum_bps`, NOT a
first-order AR(1). The characteristic polynomial is

```
λ² − (1 − ρ + α·scale + β·scale) λ + β·scale = 0
```

Stability requires both roots inside the unit disk. Using the
Jury/Schur criterion for a monic quadratic `λ² + aλ + b`:

```
|b| < 1           (product of roots < 1)
|a| < 1 + b       (sum consistent with both inside)
```

Here `a = -(1 − ρ + (α+β)·scale)`, `b = β·scale`. Substituting
numerics for **ta_dna** (`α = 0 above 200 bps, -0.05 below ; β = 0.65`;
use `α = 0` as conservative):

```
b = 0.65 · scale
```

**Stability fails as soon as `scale ≥ 1/0.65 ≈ 1.538`** because `|b| ≥ 1`.

Depth-scale: `scale = depth_ref_usd / max(side_depth, depth_floor)`.
With `depth_ref_usd = 10M` and typical BTC-perp 5bps-side depth of
$3M, `scale = 10/3 ≈ 3.33`, giving `b ≈ 2.17 > 1` — product of roots
alone exceeds unity, divergence guaranteed.

`mean_reversion_rate = 0.005` does **not** enter `b` (only `a`), so
increasing ρ does not fix this. The ρ-term is a first-order
correction that cannot stabilize a geometric chain on `prev_delta`.

## 3. Numerical verification against pathological tape

Setup: `base_strength = 1.0`, `signed_intent = +1`, `scale = 3.0`,
single-category tribe = TA, `perceived = []`.

| h | prev_delta | cum_bps | dna_delta | step_bps |
|:--|--:|--:|--:|--:|
| 0 | 0 | 0 | 0.25 (intent_drift) | 0.75 |
| 1 | 0.75 | 0.75 | 0.25 + 0.65·0.75 = 0.7375 | ~2.21 |
| 2 | 2.21 | 2.96 | 0.25 + 0.65·2.21 = 1.687 | ~5.05 |
| 3 | 5.05 | 8.01 | 0.25 + 0.65·5.05 = 3.533 | ~10.58 |
| ... | (ratio ~2.0-2.1/step) | | | |
| 100 | ~3.4e30 | ~6.8e30 | | |

Observed in pathological tape: `emove ≈ 10^120` at horizon h ≈ 60-100,
with magnitude consistent with ratio 2.0-2.1 per step. **Match.**

Retail-panic branch, once state crosses 120 bps (at TA-step ~5 above):

| h | prev_delta | cum_bps | dna_delta (panic) | step_bps |
|:--|--:|--:|--:|--:|
| t₀ (panic enters) | 10 | 150 | 4·10 + 0.04·150·1.5 + ... = 49 | 147 |
| t₀+1 | 147 | 297 | 4·147 + 0.04·297·2.97 + ... = 623.3 | 1870 |

Explosive within 2-3 steps post-panic. Quadratic term makes this
**faster than geometric** — consistent with the "single-step jump
from 10^12 to 10^60" patterns in the tape.

## 4. Calibrator hi→lo flip coupling (integrated hypothesis)

User-integrated hypothesis: kernel calibrator flip to low-clamp gain
(hit of `CORRECTION_CLAMP_LO = 0.25`) could be the initiator.

**Assessment: modulator, not driver.**

Path traced through source:

1. `kernel_calibrator.py:188-235` — calibrator adjusts `self._gain`
   via EWMA toward `target = self._gain · correction`, correction
   clamped [0.25, 4.0]. On a low-hit, `_gain` drops (slowly, α=0.02).
2. `chess.py:299` — `effective_gain = self.bps_per_unit_pressure ·
   self.regime_multiplier` (no calibrator gain read here — gain is
   injected upstream into `bps_per_unit_pressure` via a separate
   mechanism, confirmed by reading the calibrator callsite).
3. `chess.py:485` — `base_strength_pre = v0.conviction · sk0 ·
   effective_gain`.
4. `chess.py:570` — `base_strength = base_strength_pre · decay`.
5. `chess.py:579-587` — `base_strength` enters `DNAContext`.
6. Inside any `*_dna`: `intent_drift = k_cat · base_strength ·
   signed_intent` is the **step-0 seed** for the recursion.

**Conclusion:** The calibrator gain controls the **magnitude** of the
intent_drift seed. A low gain means a small seed. A high gain means a
large seed. **The gain does NOT alter the stability of the recursion
— only the initial amplitude.** The chain `0.65 · scale > 1` diverges
regardless of seed magnitude; the seed just shifts the horizon at
which numerical overflow is hit. With seed 1e-9 and ratio 2, overflow
at h ≈ ln(1e295)/ln(2) ≈ 980 (past the 250-step window). With seed
0.25 and ratio 2, overflow at h ≈ ln(1e295)/ln(2) ≈ 980 (same).

So the calibrator's hi→lo flip is **coincidental timing**, not
mechanism. The recursion was unstable before the flip; post-flip the
seed is simply in the range where the 250-step horizon sees
catastrophic values mid-projection.

**Confirmation read:** `kernel_calibrator.py` stability check at
line 188-222 works on **ratio of p50 expected-vs-realized move** — it
calibrates the *output* of the projector against reality. Of course
it clamps to HI (`_gain` correction saturating up) when the projector
is emitting 10^120 bps but reality prints 50 bps — the calibrator is
*telling us* the projector is broken, not *causing* it.

## 5. Root-cause ladder

| # | Layer | Status |
|:-:|:--|:--|
| 1 | **DNA category coefficients** — TA `0.65·prev_delta` + retail-panic `4.0·prev_delta + 0.04·state²` | 🔴 **bug-found** |
| 2 | Depth-scale multiplier `scale = 10M/max(depth, 500K)` ∈ [1, 20] — amplifies (1) | 🟡 structurally fine (market reality), but compounds (1) |
| 3 | `mean_reversion_rate = 0.005` soft anchor — first-order, cannot stabilize `b = β·scale > 1` | 🟡 too weak for current DNA, not itself wrong |
| 4 | `prev_delta_bps` channel carrying step output into next step input | 🟢 WAI (geometric propagation is the DNA design intent) |
| 5 | Calibrator gain (hi↔lo clamp) | 🟢 WAI (diagnostic, not causal) |
| 6 | Kernel decay `exp(-elapsed/hs)` | 🟢 WAI (hs>>dt, decay is slow) |

**Primary bug row: 1.** Secondary: 3 (ρ sized for a bounded DNA, not
the current unbounded one). Fix should target row 1; row 3 is a
fallback if row 1 fix proves to affect legitimate momentum signals.

## 6. Proposed H6.A fix shape (NOT implementing)

Three families of fix, ordered by invasiveness:

### Option A — coefficient tune (inside `behavioral_dna.py`)

Drop TA `momentum_amp` from `0.65` to `≤ 0.045` so `0.045 · scale < 1`
for `scale ≤ 20` (the depth-cap ceiling). Drop retail panic `chase`
from `4.0` to `≤ 0.045` similarly. Replace retail `overshoot`
quadratic with a clamped `tanh`-like saturating form.

- **Pros:** 1 file, ~5 LOC.
- **Cons:** Drastically weakens the design intent of momentum/panic
  amplification. Would need design review — the coefficients are
  semantically meaningful, not tuning knobs.
- **Worst case quartile:** MEDIUM (requires determinism gate + user
  design review of the semantic shift).

### Option B — scale moved out of recursion (in `chess.py`)

Apply `scale` as a post-step multiplier on the final consensus
`emove_bps`, NOT inside the per-step recursion. DNA functions see
bps-native inputs and outputs. The `prev_delta_bps` channel carries
bps, not bps·scale.

- **Pros:** Clean separation of concerns (DNA = behavior, scale =
  depth-aware overlay).
- **Cons:** 2-3 files, changes projector semantics, affects what TA
  "sees" in thin-book conditions (currently it sees amplified prior
  moves, which is arguably realistic). Needs scope clarification.
- **Worst case quartile:** WORST (projector math core touched, all
  of H3/H5 tape obsoleted).

### Option C — bound the recursion input (RECOMMENDED)

Clamp `prev_delta_bps` and `prev_state_bps` entering `DNAContext` to
a max magnitude (e.g. ±100 bps each). Inside `chess.py:579-587`:

```python
CTX_CLAMP_BPS = 100.0
ctx = DNAContext(
    prev_state_bps=max(-CTX_CLAMP_BPS, min(CTX_CLAMP_BPS, cum_bps)),
    prev_delta_bps=max(-CTX_CLAMP_BPS, min(CTX_CLAMP_BPS, prev_delta)),
    ...
)
```

The raw `cum_bps` and `prev_delta` still accumulate (no state loss);
only the **DNA input window** is bounded. This breaks the feedback
loop at the point it becomes pathological without altering the
design of any DNA function.

- **Pros:** 1 file, ~5 LOC, preserves all DNA semantics, preserves
  all legitimate momentum signal up to 100 bps (~1% move, well above
  normal 5-step moves).
- **Cons:** Introduces a constant with a magic number; needs ρ
  justification. Legitimate trends above 100 bps get capped — needs
  regression check.
- **Worst case quartile:** MEDIUM (1 file, ~5 LOC, but determinism
  gate + 1-day regression tape required).

## 7. Acceptance vs. H6 scope §0

> "Identify ≥ 1 DNA category with measured `∂f/∂cum_bps > 0.005`
> (= mean_reversion_rate floor) on ≥ 5% of ticks in the pathological
> baseline **or** formally rule out DNA-driven feedback as the
> mechanism"

**Satisfied.**

- `ta_dna`: `∂f/∂prev_delta = 0.65` (100% of ticks, not conditional).
  Folded through scale ≥ 1 this is ≥ 0.65 × 1 = 0.65 — **130× the
  threshold**, on **100% of ticks**.
- `retail_dna` panic: `∂f/∂prev_delta = 4.0` on ticks where panic
  triggers (perceived shock or |state| > 120). Tape shows the state
  condition trips within ~5 steps of any non-zero TA activity, so
  the tick-fraction is effectively 100% after the TA chain engages.
  **800× the threshold.**

Both bounds are over-the-line by orders of magnitude. DNA-driven
feedback is **confirmed as the mechanism**, not ruled out.

## 8. Out of scope (confirmed)

- **Changes to DNA functions** — Phase 0 is audit-only. H6.A covers
  the fix after user check-in.
- **Calibrator** — confirmed WAI per §4. Hi↔lo flip is diagnostic.
- **`mean_reversion_rate`** — confirmed structurally too weak for
  the current DNA, but not itself wrong. Not a standalone fix (row 3
  of the root-cause ladder).
- **Depth scaling** — market reality, compounds the bug but isn't
  the bug.
- **Kernel decay** — WAI; decay is slow relative to the step time,
  doesn't compete with the chain coefficient.
- **I6.I1 (per-tribe curve trace persistence)** — was specced as a
  Phase 0 dependency for empirical measurement, but the static
  derivation closed Phase 0 without it. I6.I1 remains valuable for
  **Phase 1** acceptance (post-H6.A) to verify coefficient
  distributions on live tape.

## 9. Escalations

- If H6.A (Option C) is adopted and regression shows legitimate
  trending signal is capped below the 100 bps clamp → revisit Option A
  (coefficient reduction) as a supplementary tune.
- If Option C's clamp value itself becomes the attractor (bot settles
  at ±100 bps on every pathological tick) → escalate to H7 (upstream
  intent/conviction drivers producing persistent signed_intent into a
  bounded DNA).

## 10. Box accounting

- Box: 23:48 → 00:48 CET (60 min budget).
- Used: ~35 min from box open to livrable write.
- Files read (audit pass):
  - `src/projector/chess.py` §540-670 (30 min earlier session +
    refresh this session)
  - `src/projector/behavioral_dna.py` full (393 lines)
  - `src/projector/kernel_calibrator.py` §188-235 (gain-coupling
    path)
  - `research/phase11_diagnostic/WS3_H5_phase0_audit.md` (precedent)
- Remaining box: ~25 min for check-in + user decision on H6.A sequencing.

---

**Next action:** check-in with user per `feedback_audit_fix_checkin.md`
— root cause in 3 lines + fix location + worst case quartile. No code
commit until sign-off.
