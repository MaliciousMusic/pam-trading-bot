# WS3 H5 — Cumulative-path oscillation / `step_bps_list` construction

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item | Value |
|:--|:--|
| Status | Scope-first, opened 2026-04-24 after H3 Phase 0 escalated P4 out of H3 |
| Origin | `WS3_H3_path_audit.md` §P4 — SUSPECTED-BUG verdict, `abs_activity/|emove| = 99.000` exactly on 300 live rows |
| Predecessors | H1 (Phase 1 data-gated, 2026-04-28 earliest), H2 (closed 2026-04-23), H3 (Phase 0 closed, Phase 1 data-gated) |
| Operating rule | `MEASUREMENT_INFRA.md` §2 — H-acceptance reads `data/forecasts/<day>.jsonl`, not sprint |
| Acceptance bar | Aggregate directional hit ≥ 52 % @ 20 s across 3-day panel (`WS3_SCOPE.md` D3) |
| Standing rule | Audit-before-tune (`WS3_SCOPE.md` §9) — Phase 0 structural audit before intervention |
| Parallelism | Safe to draft + Phase-0 audit while H1 / H3 Phase 1 tapes accrue (scope overlaps chess.py:742-800, no shared edit in-flight) |

## 0. Headline

`directional_score = |expected_move_bps| / abs_activity` at
`chess.py:798` has been sitting on a **deterministic attractor at
1/99 ≈ 0.010101…** across 300+ live tape rows, and the attractor is
**exactly reproduced** by assuming `step_bps_list` is
pure-alternating under `confidence_decay = 0.98, n_steps = 250`:

```
decay = 0.98,  n_steps = 250
step_bps_list ≡ [+X, −X, +X, −X, …]
Σ (−1)^i · 0.98^i · X = X / (1 + 0.98)     = 0.5018·X
Σ |(−1)^i · 0.98^i · X| = X · (1 − 0.98^250)/(1 − 0.98) = 49.6798·X
ratio = 49.6798 / 0.5018 = 99.000
directional_score = 1 / 99 = 0.01010101…
```

Live jsonl on tip `484d9d5` (`feat/tensor-engine`, Bybit ingest)
shows the attractor **dominating** the post-venue-swap tape:

```
~50 % of rows:   emove ≈ –0.06 … –0.78 bps,  dir = 0.010101…,  base_conf ≈ sq / 99
~50 % of rows:   emove = 0.0 bps,            dir = 0.010101…,  base_conf ≈ sq / 99
                 (plus a separate resolved-anchor row-type with
                  dir=None / base_conf=1.0 — see §1 below)
```

**H5 claim.** The `step_bps_list` construction pipeline at
`chess.py:742-780` (inner `steps_bps` averaging loop, then
`cum − prev_cum` differencing) is producing **systematic
per-step sign alternation** of the consensus increment. The attractor
at 1/99 is a mechanical consequence of this oscillation, not an
emergent property of a correlated council.

**Why it matters more than H3 or H1.** `base_conf = signal_quality
× directional_score` is structurally bounded by `signal_quality /
99`. With `signal_quality` itself trending ~0.03 in live tape,
`base_conf` sits at ~3 × 10⁻⁴ — effectively floored at noise. Every
downstream multiplier (`diversity_mult ∈ [0.6, 1.0]`, session, agree)
operates on this floor. If P4 is the dominant confidence-gap driver,
closing H5 lifts `base_conf` by **one to two orders of magnitude**
before any correlation or calibration story lands on top.

**What H5 does *not* claim.** H5 does not claim the aggregator,
the calibrator, or the DNA simulation is buggy. It claims the
specific construction `step_bps_list = [cum_i − cum_{i−1}]` at
`chess.py:776-780`, fed by the per-tribe-averaged cumulative
`steps_bps`, is producing a pathological per-step sign pattern.

**Live pre-observations at scope time (Bybit tape, `data/forecasts/2026-04-24.jsonl`, ~2 k rows):**

- `directional_score` is `0.0101…` on every tick-forecast row that
  has a non-null `directional_score`.
- Ticks where `expected_move_bps = 0.0` still read `directional =
  0.0101`. That's self-contradictory under the published formula
  (`|emove|/abs_activity = 0/X = 0`), so there must be a numerical
  route to `dir = 0.0101` **that doesn't go through the published
  formula** — e.g. a separate code path, or a pre-clamp, or a
  sentinel.
- Live log (`logs/bot_ui_2026-04-24_bybit.log`) shows `forecast.expected_move_bps`
  values in the `±10¹⁰⁰` range on some ticks (astronomical). The
  jsonl-persisted `expected_move_bps` on the same range of ticks
  stays bounded (±0.8 bps). There is a **silent divergence between
  `forecast.expected_move_bps` as logged at `engine.py:387` and as
  persisted by the auditor** — either a clamp runs between log
  and persist, or two different forecast objects exist. Either
  reading is in-scope for H5 Phase 0.

## 1. Antecedents — what's already known at H5 open

- **H3 Phase 0 verdict (§P4).** The 99.000 attractor is
  numerical, not a correlation artifact. Candidate mechanisms
  (from the H3 audit, deliberately not verified there):
  1. Per-tribe walk delta vs. cumulative is being double-differenced.
  2. A sign-flip in `decayed_by_step` accumulation.
  3. Tribe votes canceling each other one step out of phase due
     to a timestamping off-by-one.
- **P8 resolution.** H3 P8 resolved `directional_score ≈ 0` live-
  tape observation as `= 1/99 ≈ 0.01` under display rounding, i.e.
  P4 in disguise. No separate Bybit-ingest gap.
- **H1 Phase 0 (closed 2026-04-22).** `confidence_decay = 0.98`
  and `projector_steps = 250` were surfaced and are now confirmed
  load-bearing parameters for P4 (they produce the exact 99.000
  attractor).
- **H2 closed 2026-04-23.** Kernel gain ≈ 1.57 is inside the
  clamp range, so the per-tribe per-step deltas being fed into
  `steps_bps` are not saturated. H5 is therefore not a kernel-gain
  story.
- **Phase 11 confidence gap.** Aggregate directional hit sits in
  the 48-51 % band across prior panels. H3 would lift through
  correlation correction; H5 lifts through mechanical oscillation
  correction. Both can be non-zero. H5 is expected to dominate
  if the attractor holds across a full post-swap day.
- **Sentinel-row observation (new at scope time, 2026-04-24).**
  A subset of jsonl rows has `directional_score = None, base_conf
  = 1.0, expected_move_bps ≠ 0`. These do not match the
  projector's documented formula: `base_conf = max(0, min(1,
  signal_quality × directional))` with `signal_quality` ∈ [0, 1]
  would give `base_conf ≤ 1` only when `directional` is non-null
  and `signal_quality × directional ≥ 1`. `signal_quality ≤ 0.23`
  on these rows, so `base_conf = 1.0` is impossible under the
  published formula. Likely these are **resolved-anchor records
  from the auditor**, not live-tick forecasts — audit must
  disambiguate in Phase 0.

## 2. Phase 0 — `step_bps_list` construction audit (gating)

Before any intervention ships, read the full `step_bps_list`
construction pipeline end-to-end. Verdict outcome gates Phase 1:
bug-found → fix commit → re-audit; clean or suspected-without-root-
cause → Phase 1 measurement.

**Method:** static code read, same pattern as H1 / H3 Phase 0.
No sprint; no live-tape measurement beyond the tape samples
already in hand. Output: `WS3_H5_path_audit.md` with the
pair-check trace and a verdict ∈ {bug-found, suspected, clean}
per check.

**Scope file set:**

- `src/projector/chess.py:742-800` — `steps_bps` averaging,
  `step_bps_list` differencing, `expected_move_bps` / `abs_activity`
  / `directional` / `base_conf` computation.
- `src/projector/chess.py:~400-720` — per-tribe walk construction,
  `per_tribe_curves`, `decayed_by_step`, DNA pipeline entry points.
  Scope the upstream flow only as far as needed to trace where
  per-step sign alternation is introduced.
- `src/projector/behavioral_dna.py` — per-tribe DNA step function,
  specifically any sign-dependent term (shock perception, soft-
  limit anchor, mean-reversion ρ).
- `src/core/engine.py:380-395` — logger format that writes the
  astronomical emove. Confirm the log value and the persisted
  jsonl value come from the same `forecast` object.
- `src/audit/*.py` (resolved / backscorer path) — identify whether
  the `dir = None / base_conf = 1.0` rows are resolved-anchor
  records vs. live-tick records. If resolved-anchor: confirm the
  schema is distinct and document it, not conflate with P4.

**Pair checks to perform**:

| # | Check | Why it matters |
|:--|:------|:---------------|
| Q1 | `step_bps_list.append(cum − prev_cum)` at `chess.py:776-780`. Input `steps_bps` is the weighted average of per-tribe **cumulative curves** at each step. If `per_tribe_curves[h]` is itself a cumulative quantity (not a per-step delta), the `cum − prev_cum` outer differencing is correct. If `per_tribe_curves[h]` is already a **per-step delta**, the outer differencing is a double-difference: `delta_{h} − delta_{h−1}`, which will alternate sign under any mildly oscillating process. | This is candidate (1) from the H3 audit. Confirm by reading `per_tribe_curves` construction: is `curve[h]` a cumulative path position or a per-step increment? The call site at `chess.py:746-762` does `num += curve[h] * wv` as if `curve[h]` is the state at step h — if curves are deltas, the steps_bps loop is summing deltas and the outer diff is a double-diff. |
| Q2 | `decayed_by_step` construction upstream of `aggregate_votes(decayed_by_step[i], …)` at `chess.py:787`. If any per-step decay flips sign (e.g. `(-0.98)^i` instead of `0.98^i`, or a subtraction that should be a multiplication), the council's read of "agreement at step i" alternates, and the `step_aggs` consumers (signal_quality) pick up the alternation. | This is candidate (2) from the H3 audit. Trace the `decayed_by_step` assembly, looking for any factor whose sign depends on `i` parity. Particularly: soft-limit anchor / mean-reversion rate ρ at the behavioral-DNA docstring lines 26-27 — a pull toward zero that over-shoots creates sign alternation. |
| Q3 | `behavioral_dna.dna_for` per-tribe step function. If `Delta_{i,h} = f(prev_state, prev_delta, …)` uses `prev_delta` with a negated sign (a "mean-reverter" tribe that over-corrects), individual per-tribe curves will oscillate. When averaged across 17-24 tribes with near-identical signed contributions, the average preserves the oscillation. | This is candidate (3) reframed — not a timestamping off-by-one but a per-tribe step function that produces tick-level sign flips. 24 alternating-sign tribes average to an alternating-sign consensus. Audit the DNA step for any `-prev_delta` or `ρ × (0 − state)` terms. |
| Q4 | Timestamping / tick dispatch. `aggregate_votes(decayed_by_step[i], start_ts + timedelta(seconds=(i+1)*dt_s))` at `chess.py:787` constructs per-step timestamps. If any downstream-dependent value keys on `i % 2` (e.g. an even-odd shock dispatch, or a perceived_shocks list indexed with `(i + 1) % N`), a parity artifact emerges. | This is candidate (3) from the H3 audit. Trace the per-step shock dispatch in `behavioral_dna.py` — specifically whether `perceive_for_tribe(ts, ...)` produces different outputs on even vs. odd `i` for structural reasons (latency windows, visibility caps). |
| Q5 | `forecast.expected_move_bps` logged at `engine.py:387` reads `±10¹⁰⁰` bps on live ticks while jsonl persists reasonable ±0.8 bps values. There's a clamp or a second `expected_move_bps` computation between log and persist. | Potentially a separate bug — but critical to disambiguate before writing off P4 as "just numerical." If log-emove is the true consensus and jsonl-emove is clamped, the attractor may be a clamp artifact, not a cumulative-path artifact. If log-emove is a red herring (e.g. stale-forecast reference), the jsonl value is canonical and P4 stands. |
| Q6 | `signal_quality = Σ (1 − uncertainty) · 0.98^i / k_sum`. With `decay = 0.98, n_steps = 250, k_sum = 49.68`, the first 20 steps carry 31 % of the total kernel weight. If `uncertainty[i]` is systematically higher at short `i` (council not yet coherent), `signal_quality` is structurally low regardless of P4. | Not a sign-alternation check but a same-lens check: P4 attracts `directional` to 1/99; the same decay-250 pair may attract `signal_quality` to a structurally low value, independent of correlation. Both multiply into `base_conf`. Worth a separate reading. |
| Q7 | Non-tick forecast rows (`dir = None, base_conf = 1.0`) in jsonl. Confirm they are resolved-anchor records (auditor backscoring), not live-tick forecasts that bypass the projector. Document the schema difference in the audit report. | Avoids conflating two row-types in any downstream analysis. If `dir = None` rows are being sampled into P4's 300-row filter, the attractor statistics are biased. Re-run the P4 analytical check on tick-rows only (filter by row-type, not by `|emove| > 0.01`). |

**Expected audit output.** Either:
- **bug-found.** Concrete discrepancy (most likely Q1, Q2, or Q3).
  Fix commit lands as a standalone change with a fresh determinism
  gate; H5 re-opens to Phase 1 post-fix to confirm the attractor
  breaks.
- **clean.** No structural bug; the attractor is genuinely an
  emergent property of the DNA + averaging + decay = 0.98 /
  steps = 250 triple. Proceed to Phase 1 treating the attractor
  as the canonical behaviour and evaluate intervention options
  (§4) against it.
- **suspected.** Behaviour plausibly wrong but requires live-tape
  measurement to confirm. Annotate candidate mechanisms; Phase 1
  measures which one dominates.

**Landing discipline.** Per `WS3_SCOPE.md` §9, Phase 0 ships as a
docs commit only. Any bug-fix surfaces as a separate commit with
its own determinism gate. Stacking audit + fix is forbidden.

## 3. Phase 1 — Live-tape cumulative-path measurement

### 3.1 Instrumentation (gating commit — H5.I1)

The live archive currently persists `expected_move_bps`,
`directional_score`, `signal_quality`, `base_conf`, and a subset
of upstream scalars. It does **not** persist `step_bps_list` or
`abs_activity` per tick. Phase 1 opens with an instrumentation
commit.

**Scope:** extend the tick-forecast record with two additions:

- `abs_activity_bps`: the scalar `Σ |step_bps_i| · decay_i` at
  `chess.py:797`. 1 float / record ≈ 0.15 MB / day.
- `step_bps_signflips`: count of per-step sign changes in
  `step_bps_list` (scalar, integer in `[0, n_steps − 1]`).
  1 int / record ≈ 0.15 MB / day.

Both are already-computed-in-the-projector values, so no
re-computation cost. Optional **Shape A** (full `step_bps_list`
array, 250 floats / record ≈ 40 MB / day) is **rejected** as
over-budget at scope time; the two scalars above are enough to
drive the Phase 1 analysis and fit cleanly in the jsonl budget.

**Determinism cost:** zero (both values derived from extant
projector state).

**Backfill:** partial. `abs_activity` and `signflips` cannot be
reconstructed from historical jsonl — new fields only. H5 Phase 1
panel dates shift forward from H3 / H1 dates by the H5.I1
landing delta.

### 3.2 Measurement

On the post-H5.I1 live archive, compute:

- **`dir_score` distribution.** Histogram of `directional_score`
  across a full post-instrumentation day. Expected under the pure-
  alternation attractor: a Dirac spike at `1/99 ≈ 0.01010…` with
  near-zero dispersion. Any dispersion > 10⁻³ around the spike
  is evidence the attractor is not universal (different per-tick
  regime produces different attractor values).
- **`signflips / n_steps` ratio** per tick. Expected under the
  pure-alternation attractor: ≥ 0.99 (almost every consecutive
  pair flips sign). If measured ratio sits at 0.45-0.55, the
  attractor is an *average* pattern, not a per-step rule, and
  the 99.000 ratio is approximate.
- **Joint distribution of (`signal_quality`, `abs_activity_bps`,
  `|emove|`).** If `abs_activity_bps` scales linearly with the
  per-tick macro volatility but `|emove|` scales only with
  `signal_quality`, the two are structurally decoupled. That's
  the P4 fingerprint.
- **Bucketed by regime label.** Is the attractor universal, or
  only in `chop` / `normal`? A `trend`-specific break would
  indicate the DNA's mean-reversion term is the driver.

**Script:** `scripts/ws3_h5_path_analyze.py` (new), reads
post-H5.I1 `data/forecasts/<day>.jsonl`, emits the four
distributions above + a short verdict file to
`research/phase11_diagnostic/_ws3_h5_attractor/`.

### 3.3 Expected signatures if H5 true

- `directional_score` histogram is a Dirac at `1/99 ± 10⁻⁵` on
  ≥ 95 % of tick-forecast rows.
- `signflips / n_steps` ≥ 0.95 on ≥ 80 % of rows.
- `abs_activity_bps` / `|emove|` ratio median ≥ 50 across the
  full day (vs. the analytical 99 for pure alternation).
- The attractor holds across all regime labels (chop, normal,
  trend, cascade) — it's mechanical, not regime-dependent.

### 3.4 Kill criterion

**H5 is falsified** if `directional_score` histogram has
dispersion > 0.05 and a visible bulk below `1/99`, AND the
`signflips / n_steps` median is ≤ 0.6 on the full-day panel. In
that case `step_bps_list` is **not** pure-alternating; the 99.000
attractor in H3's 300-row sample was a filter artifact (the
`|emove| > 0.01` filter preferentially selected attractor-rows).
H5 closes as null; P4 reopens for a different diagnostic framing
or writes up as an explained filter bias.

## 4. Intervention options (ship only one per H5 pass)

Per `WS3_SCOPE.md` §7. Options contingent on Phase 0 verdict.

### 4.1 Option A — Construction fix (bug-found track)

If Phase 0 finds a concrete discrepancy (Q1 / Q2 / Q3 / Q4 /
Q5), the fix is the intervention. Examples:

- **Q1-track:** Remove the outer `cum − prev_cum` differencing at
  `chess.py:776-780` if `per_tribe_curves` are already deltas.
  Replace with direct assignment `step_bps_list = list(steps_bps)`.
- **Q2-track:** Fix the sign-flip in `decayed_by_step` composition.
- **Q3-track:** Audit the DNA step function and remove the
  over-correcting mean-reversion term, OR clamp the per-step
  delta sign to prevent oscillation.
- **Q5-track:** If log-emove is true and jsonl-emove is clamped,
  remove the silent clamp OR (more likely) fix whichever upstream
  term is exploding into 10¹⁰⁰.

**Determinism cost:** non-zero — `expected_move_bps`,
`directional_score`, `base_conf`, and `forecast_conf` output
distributions all shift. Determinism gate re-runs; new golden file.

**Prefer A-Q1** if the audit finds the double-diff reading. It's
a 2-line change and immediately falsifies the attractor if the
bug is real.

### 4.2 Option B — Parameter recalibration (clean-verdict track)

If Phase 0 finds no bug and the attractor is a `decay = 0.98,
steps = 250` emergent property, the intervention is a parameter
change. Under `decay = 0.96, steps = 120`:

```
Σ 0.96^i = (1 − 0.96^120) / 0.04 = 24.75
Σ (−1)^i · 0.96^i = 1 / 1.96 = 0.51
ratio = 24.75 / 0.51 = 48.5
```

Less extreme, but still a 1/48.5 ≈ 0.0206 floor. Under `decay =
0.90, steps = 60`:

```
Σ 0.90^i = 9.85
Σ (−1)^i · 0.90^i = 0.526
ratio = 18.7
→ floor = 1 / 18.7 = 0.053
```

A 5× lift of `directional_score`'s floor. Combined with H3 or H1,
this could close a material fraction of the aggregate-hit gap.

**Determinism cost:** high — every downstream confidence read
changes, calibrator re-anchors. Plausible 3-5 day re-anchor
window before the post-change panel is comparable.

**Prefer B** only if Phase 0 rules A out cleanly.

### 4.3 Option C — Dual-score exposure

Expose both `|emove|` and `abs_activity` directly to the decision
engine rather than collapsing to `directional = |emove|/abs_activity`.
Lets the decision layer decide whether high-activity / low-emove
means "wait for direction to resolve" or "the council is
genuinely oscillating — stay out."

**Determinism cost:** schema change (new forecast fields) + all
downstream decision-engine reads. Larger than A or B.

**Prefer C** only if A and B both underperform acceptance, and
the decision-layer has bandwidth to consume two-scalar confidence
instead of one.

### 4.4 Combined-option discipline

One intervention commit per H5 pass. A first; if A clean-verdict
and B doesn't close acceptance alone, open a scope re-read before
bundling B+C.

## 5. Acceptance criteria

Per `WS3_SCOPE.md` §10:

**(a)** H5 Option A or B (whichever Phase 0 indicates) ships and
panel-aggregate directional hit at 20 s ≥ 52 % across (post-H5.I1
≥ 3-day panel). Measurement determinism gate passes and no panel
day regresses.

**(b)** A and B both ship, each measured in isolation, and
neither clears 52 %. H5 is written up as null; remaining H-list
carries Phase 11 forward.

**Partial-lift stopping rule.** Same as H1 §5: if A lands in
48-51 % range, pause before running B. Partial lift admits two
readings (P4 partially true, additive elsewhere vs. P4 near-
exhausted). Do not autopilot through the option list.

**Explicitly not an acceptance criterion:** post-friction net PnL.
H5 is signal diagnosis only (D3).

## 6. Instrumentation catalog (new work)

| Commit | Scope | Determinism impact | Lines |
|:--|:--|:--|:--|
| H5.0  | `WS3_H5_path_audit.md` — Phase 0 audit report | Docs only | N/A |
| H5.I1 | Persist `abs_activity_bps` + `step_bps_signflips` on tick forecasts | Zero (derived values) | ~20 |
| H5.I2 | `scripts/ws3_h5_path_analyze.py` — attractor analysis | N/A (post-hoc) | ~140 |
| H5.A  | Construction fix (bug-found track) — Q1/Q2/Q3/Q5 specific | Non-zero; new golden | 2-40 |
| H5.B  | Parameter recalibration (clean-verdict track) | High; calibrator re-anchor | ~10 |
| H5.C  | Dual-score exposure (decision-engine track) | Schema change | ~60 |

All instrumentation commits ship **before** any mechanism change.

## 7. Execution sequence

```
  H5.0 (Phase 0 audit) ── bug-found? ── yes ──▶ H5.A (construction fix)
          │ no                                             │
          ▼                                                ▼
  H5.I1 (auditor instrumentation)               re-audit post-fix
          │                                                │
          ▼                                                │
  determinism gate pass                                    │
          │                                                │
          ▼                                                ▼
  3+ days of production run             (H5 closes if post-fix
  accumulate live-tape data              directional_score no
          │                               longer at 1/99 attractor)
          ▼
  H5.I2 (analysis script) — Phase 1 measurement
          │
          ▼
  Signature matches §3.3? ── no ──▶ H5 null → scope re-open
          │ yes
          ▼
  Choose intervention per Phase 0 verdict:
     bug-found → A (already landed above; evaluate panel)
     clean     → B or C per council review
          │
          ▼
  Ship intervention → measurement determinism gate
          │
          ▼
  Panel-aggregate ≥ 52 % @ 20 s? ── yes ──▶ H5 closes accepted
          │ no                               │ partial (48-51 %)
          ▼                                  ▼
  fall-through to next option           pause, reviewer D4
```

## 8. Parallelism with H1 and H3

H5 Phase 0 reads `chess.py:742-800` (the step_bps_list region)
plus a controlled touch into upstream DNA / per_tribe_curves paths.

- **H1 Phase 1** consumes `data/forecasts/<day>.jsonl` only — no
  code edits required. Safe parallel.
- **H3 Phase 0** (closed) read the aggregator + diversity + category
  paths — disjoint from H5's step_bps_list scope.
- **H3.C** (imminent) edits `chess.py:70-88` and `:1001` only —
  disjoint from H5's :742-800 scope. Safe to ship H3.C before H5
  Phase 0 writes.
- **H3 Phase 1** once it opens will add `tribe_votes` persistence
  in the resolved-anchor path; disjoint from H5's tick-forecast
  field additions.

Recommended sequencing: H3.C ships first (P6 bug, tiny), then H5
Phase 0 audit lands, then H5.I1 instrumentation. H3 Phase 1 and
H5 Phase 1 then run their ≥ 3-day clocks in parallel.

## 9. Open questions flagged for the audit

1. **per_tribe_curves — cumulative or delta?** Q1's outcome hinges
   entirely on this. Confirm by reading the DNA → walk-builder →
   curves path and documenting the contract explicitly in the
   audit report. The docstring at `chess.py:22-23` says "DNA
   returns Delta_{i,h} → State_{i,h} = State_{i,h-1} + Delta_{i,h}"
   — if `per_tribe_curves[h]` holds `State` (cumulative), the outer
   diff is correct; if it holds `Delta` (per-step), it's a
   double-diff. This is the single most important pair-check.
2. **Log-vs-jsonl emove divergence.** Confirm whether the
   `±10¹⁰⁰ bps` log values and the `±0.8 bps` jsonl values come
   from the same `forecast` object. If they do, a clamp exists
   between `engine.py:387` and the auditor persist — find and
   document it. If they don't, there are two forecast objects in
   play and one is pathological.
3. **Resolved-anchor row schema.** Confirm `dir = None / base_conf
   = 1.0` rows are auditor backscore records and not projector
   outputs. Document the schema in the audit report so future
   analyses don't conflate row-types. Likely out of H5 Phase-0
   scope per se, but needed to clean the tape before §3.2
   measurements.
4. **Decay/steps interaction.** Is `decay = 0.98, steps = 250`
   the product of a deliberate calibration (H2 posterior) or an
   inherited default? If deliberate, Option B is politically
   costly — we'd be unwinding calibration work. If inherited,
   Option B is a free recalibration. Flagged for Phase-0 context,
   not a pair-check.

---

*Scope landed 2026-04-24 alongside H3 Phase 0 closure. Phase 0
audit is the next H5 commit — gated only on H3.C landing first
(both edit chess.py; H3.C is trivial and ships in minutes).*
