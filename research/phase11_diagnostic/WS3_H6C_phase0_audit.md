# WS3 H6.C Phase 0 audit — `dna_delta` output saturation (door #3)

**Status**: VERDICT — BUG-FOUND (door #3 = `dna_delta` output, NOT `event_bps` as
H6.B audit §8 hypothesised)
**Date**: 2026-04-25
**Author**: agent (live instrumentation + static trace)
**Box**: H6.C, opened 2026-04-25 (post H6.B FAIL on live)
**Predecessors**: H5.A (`f0076bd`), H6.A (`6332848`), H6.B (`e02f3a6`)

---

## 1. Question

Why does live 5-min restart **still** show 100% steady-state saturation
after H6.A (DNA recursion-input clamp ±100) AND H6.B (depth-scale cap 5×)
both landed and verified working?

---

## 2. Method

Phase 0 ≠ static analysis only. Three paths ran in parallel:

1. **Static trace** of `event_bps` source → `step_event_bps()` in
   `src/projector/event_wave.py`. Confirmed unbounded sum over
   `pending_events` (no cap on event count or per-event `impact_bps`).
2. **Static trace** of `dna_delta` magnitude assuming H6.A clamp in
   place. Best-case bound for `ta_dna`: `0.65 × 100 + 0.25 × base_strength
   × 1 + leaked × 0.20`. Bounded only if `base_strength` and `perceived`
   are bounded.
3. **Live instrumentation** added to `chess.py` per-step loop (working-tree
   only, NOT committed): track per-projection `max(|dna_delta|)`,
   `max(|event_bps|)`, `max(|total_bps|)`, `count(|total_bps| > 100)`.
   Logger.warning at end of each `project()`. Detached bot run: PID 5780,
   2026-04-25 22:33:56–22:37:56 local time (~4 min, 234 projections).

---

## 3. Findings

### 3.1 Live data — 234 projections aggregate

| Metric | Value |
|---|---|
| `max_event` per projection | **0.0 bps always** (234/234) |
| `max_dna` p10 | 65.3 bps |
| `max_dna` p50 | 1,193.4 bps |
| `max_dna` p90 | 2,103.2 bps |
| `max_dna` max | **3,364.1 bps** |
| `max_dna` mean | 1,132.1 bps |
| `steps_total > 100` per projection | mean 32.3 / max 60 (~13% of 240 (tribe, step) pairs) |
| `saturation_hit` warnings | **161** in 234 projections |

### 3.2 Trajectory (warmup → steady state)

Sorted first 10 projections (warmup, ticks 0-10):

```
65.6  65.6  65.6  65.9  66.2  68.8  70.8  74.8  79.9  84.7
```

Sorted last 10 projections (post-converge, ticks 224-234):

```
943  1086  1222  1420  1630  1791  2070  2103  2513  2744
```

`max_dna` grows monotonically from ~65 to ~2,700 bps over ~200 projections —
clear divergence pattern, NOT a steady-state with random fluctuations.

### 3.3 Verdicts on H6.B audit §8 candidates

- **#3a `event_bps`** — **REFUTED** in chop regime. `max_event = 0.0` on all
  234 projections. `pending_events` is populated only from `kind == "news"`
  classifier hits (`src/tribes/market_state.py:514`); chop regime had zero
  active events during the run. *Latent driver in news/cascade regimes
  per the original H6.B hypothesis — but not the active driver.*
- **#3b `base_strength` / `effective_gain`** — **CONFIRMED upstream cause**.
  `effective_gain = bps_per_unit_pressure × regime_multiplier` (default
  `bps_per_unit_pressure = 8.0`), then `base_strength_pre = conviction × sk0
  × effective_gain`. The **`sk0` term is `calibrator.skill_multiplier(tid)`**
  (`chess.py:518`, `src/projector/chess.py:351-352`), which is unbounded
  in code. As skill grows tick-by-tick (calibrator updates from prior
  predictions), `base_strength` grows, and every `intent_drift` term
  in every DNA function (coefficients 0.10 to **0.55** for whale_dna)
  grows linearly. This is the source of the monotonic 65 → 2,700 ramp.
- **#3c consensus aggregation weight concentration** — not assessed
  directly; not needed since #3b explains the entire ramp.

### 3.4 Why H6.A clamp does NOT prevent this

H6.A clamps the **recursion inputs** to `DNAContext` (`prev_state_bps`,
`prev_delta_bps`) at ±100 bps. The clamp limits the *prev_delta* and
*prev_state* terms inside DNA functions:
- ta_dna: `0.65 × clamped_prev_delta = ±65 bps max`
- arb_dna: `-0.18 × clamped_prev_state = ±18 bps max`
- mev_dna: `-0.55 × clamped_prev_state = ±55 bps max`

…but **does not** touch `intent_drift = c × base_strength × signed_intent`,
where `c ∈ {0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.55}` per category and
`base_strength` is unbounded. With `base_strength = 6,000` (skill 750 ×
conviction 1 × effective_gain 8), whale_dna's `intent_drift = 0.55 × 6000
× 1 = 3,300 bps` — exactly the magnitude observed.

### 3.5 Why H6.B cap does NOT prevent this

H6.B caps `scale = min(raw_scale, 5.0)`. With per-step `total_bps =
dna_delta = 3,364`, `step_bps = total_bps × 5.0 = 16,820 bps` — far above
H5.A's 500 bps consensus cap. H6.B was sound for the chain `(bounded_dna
+ bounded_event) × bounded_scale`, but `dna_delta` is itself unbounded.

---

## 4. Failure cascade (full saturation chain post-H6.A+B)

```
calibrator.skill_multiplier(tid) → ∞ over time
  → base_strength = conviction × skill × effective_gain → unbounded
    → intent_drift_in_DNA = c × base_strength × signed_intent → unbounded
      → dna_delta (DNA output) → 65 → 2,744 bps observed
        → total_bps = dna_delta + 0  (event_bps = 0 in chop)
          → step_bps = total_bps × scale, with scale capped at 5
            → step_bps ≤ 5 × 2,744 = 13,720 bps per (tribe, step)
              → consensus weighted mean → > 500 bps per consensus step
                → H5.A saturation_hit = TRUE → conf = 0 → 0 trades
```

Door #1 (H6.A clamp inputs), door #2 (H6.B cap scale), door #3
(`dna_delta` itself) — only door #3 closure breaks the chain.

---

## 5. Fix options

### Option A — cap `dna_delta` directly (RECOMMENDED)

```python
DNA_DELTA_CLAMP_BPS: float = 100.0  # symmetry with H6.A input clamp

dna_delta_raw = dna_fn(ctx)
dna_delta = max(-DNA_DELTA_CLAMP_BPS,
                min(DNA_DELTA_CLAMP_BPS, dna_delta_raw))
if dna_delta != dna_delta_raw:
    dna_delta_clamp_hits += 1
```

**Why**: Single-knob, ≤8 LOC, mirrors H5.A/H6.A/H6.B pattern. Bounds the
DNA output regardless of which internal term explodes (skill,
regime_multiplier, perceived shocks, retail panic). Future-proofs against
any new DNA category whose math we haven't audited.

**Math**: with cap = 100 and H6.B scale cap = 5, per-tribe `step_bps ≤
500 = MAX_STEP_BPS`. Consensus weighted mean ≤ 500 strictly, with ample
dispersion-induced margin. H5.A `saturation_hit` should fire only on
adversarial alignment — expected steady-state rate << 5%.

**Reversible**: tune DNA_DELTA_CLAMP_BPS if signal is strangled
(bump to 150) or saturation persists (drop to 75).

**Risk**: LOW. Unit-testable, deterministic, mirrors three landed clamps.

### Option B — cap `base_strength_pre` upstream

```python
BASE_STRENGTH_MAX = 30.0  # ~conviction(1) × skill(2) × effective_gain(15)
base_strength_pre = min(BASE_STRENGTH_MAX, v0.conviction * sk0 * effective_gain)
```

**Why not**: roots out the source, but doesn't bound `perceived_sum` or
`retail panic` paths. Could still leak bps via shocks. Less robust than A.

### Option C — cap `calibrator.skill_multiplier`

**Why not**: requires changing the calibrator interface — out of scope.
Also leaves the design assumption "skill is unbounded" intact, which is a
real semantic question for a separate task (H6.D? skill calibration audit).

### Option D — combined A + cap on regime_multiplier

**Why not**: bigger surface area, multi-knob. Defer until A is verified
insufficient.

**Verdict**: Option A.

---

## 6. Implementation cadre (Phase 1)

| Phase | Budget |
|:--|--:|
| Revert P0 instrumentation in `chess.py` | 5 min |
| Add `DNA_DELTA_CLAMP_BPS` constant + cap line + `_dna_delta_clamp_hits_last` counter + logger.warning | 10 min |
| Unit tests `tests/projector/test_dna_delta_clamp.py` (~6 contracts) | 15 min |
| Determinism gate `--max-ticks=1000` on 2026-04-20 | 10 min |
| Chop regression: saturation 0/1000 ticks | 10 min |
| **Total Phase 1** | **50 min** |

Phase 2 live gate: 15 min. Phase 3 close: 15 min. Total H6.C box: ~80 min
remaining within 2h30 strict.

---

## 7. Acceptance for Phase 1

1. **Tests**: 6/6 new contracts PASS, all existing tests PASS.
2. **Det gate**: `--max-ticks=1000` byte-identical PASS on 04-20 (with both
   H6.A and H6.B already merged, expect same byte output as `e02f3a6`
   determinism baseline).
3. **Chop regression**: saturation < 5% on 1000-tick replay 2026-04-20.

## 8. Acceptance for Phase 2 (live gate)

- Live 5-min steady-state saturation (ticks ≥ 100) **< 5%**.
- If still ≥ 5% → STOP, check-in, candidate door #4 = `perceived` shocks
  pipeline (then audit `_world_shocks_for_tribe`).

---

## 9. Door #4 watchlist (only if H6.C live FAIL)

- **`perceived_sum` upstream** — `_world_shocks_for_tribe` magnitudes.
  If world shocks have unbounded amplitude, `arb_dna.shock_response =
  0.55 × perceived_sum` could re-explode dna_delta even with cap. Cap
  `perceived` at the source.
- **calibrator skill update rule** — if skill compounds positively on
  bot-self-prediction-matches-bot-self-move, the calibrator is broken
  semantically. Separate audit (H6.D candidate scope).

---

## 10. One-line summary

H6.C door #3 = `dna_delta` itself, not `event_bps`; `intent_drift = c ×
base_strength × signed_intent` with calibrator-driven unbounded `skill`
amplifies whale_dna (c=0.55) past 3,000 bps in chop steady-state; fix =
cap `dna_delta` at ±100 bps in `chess.py` post-`dna_fn(ctx)`, ≤8 LOC,
single-knob reversible, identical pattern to H5.A/H6.A/H6.B.
