# WS3 H1 — Confidence-formula compounding

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item | Value |
|:--|:--|
| Status | Scope-first, awaiting reviewer go-ahead (per PLAN §5.2 Q1 gate) |
| Predecessor | WS3 R2 (closed 2026-04-24; Commit A `fd400e8`, Commit B `20b53f6`) |
| Causal upstream | WS3 H2 (Option 1a live-tape snapshot closed 2026-04-23) |
| Operating rule | `MEASUREMENT_INFRA.md` §2 — H-acceptance reads `data/forecasts/<day>.jsonl`, not sprint |
| Acceptance bar | Aggregate directional hit ≥ 52 % @ 20 s across 4-day panel (`WS3_SCOPE.md` D3) |
| Standing rule | Audit-before-tune (`WS3_SCOPE.md` §9) — Phase 0 structural audit before intervention |

## 0. Headline

The ChessProjector confidence formula
(`src/projector/chess.py:791-812`) multiplies three bounded factors
and clamps to [0, 1]:

```
signal_quality  = Σ (1 − uncertainty_i) · kernel_i  / Σ kernel_i     ∈ [0, 1]
directional     = |expected_move_bps| / Σ |step_bps_i · kernel_i|    ∈ [0, 1]
base_conf       = clip(signal_quality × directional, 0, 1)           ∈ [0, 1]
diversity_mult  = 0.6 + 0.4 × diversity_score                        ∈ [0.6, 1.0]
final_conf      = clip(base_conf × diversity_mult, 0, 1)             ∈ [0, 1]
```

**H1 claim.** Three sub-1 factors compounded produce a structural
cap of `≈ 0.25` regardless of the underlying signal quality. The
multiplicative stack is what bounds confidence — not signal absence.

**Diagnostic weight.** HIGH prior (WS3 §2 H1 row). WS1 observed
`final_conf` never exceeded 0.2483 across 86,399 ticks; H1 is the
most direct structural explanation.

**What H1 does *not* claim.** H1 does not claim the projector
produces good directional predictions that the conf formula is
strangling. It claims that *whatever* the underlying signal, the
formula's output structure is the binding constraint on
downstream decisions.

## 1. Antecedents — what's already known at H1 open

Carried forward from closed/landed work. H1 does not re-derive these.

- **R2 closed.** `conviction_boost` no longer inflates prediction
  (`WS3_R2_step_A_attribution.md` §2.1 algebraic invariance proof).
  It is a pure sizing multiplier post-R2 commit B
  (`WS3_R2_step_B_attribution.md` §3). Consequence for H1:
  the confidence *formula* has not been materially perturbed by
  R2 — conf has always been computed from the raw consensus path,
  not the boosted one. H1 measures the post-R2 steady state without
  needing a pre-R2 contrast.

- **H2 Option 1a closed.** Production kernel gain sits at ~1.57, 25×
  below the 40.0 ceiling (`MEASUREMENT_INFRA.md` §4.1). Live-tape
  corrections sit in the 0.69-2.47 range. Calibrator is not pinned;
  any H1 decomposition reading against production emove values is
  reading a calibrated signal, not a clamp artifact.

- **Live-tape archive schema.** `data/forecasts/<day>.jsonl` persists
  `forecast_conf`, `expected_move_bps`, `realized_move_bps`,
  `net_pressure`, `directional_hit`, per-anchor metadata. It does
  **not** persist the conf decomposition — `signal_quality`,
  `directional`, `base_conf`, `diversity_score`, `diversity_mult`
  are computed in `ChessProjector.project` and discarded before the
  auditor's `record_tick` call. **H1 Phase 0 is instrumentation-first.**

- **Live directional hit.** 20 s directional hit on
  `data/forecasts/2026-04-20.jsonl` is 44.5 % aggregate (WS1). H1
  acceptance requires lifting the panel-aggregate to ≥ 52 % (D3).
  The 44.5 % → 52 % headroom is 7.5 pp; H1 either finds mechanism
  in that room or nulls and hands the bar to H3.

## 2. Phase 0 — Confidence-formula structural audit (gating)

Before any measurement commit ships, read the conf-formula path
end-to-end for structural bugs. Verdict outcome gates Phase 1:
bug-found → fix commit → re-audit; clean or suspected-without-root-
cause → proceed to Phase 1.

**Method:** static code read, same pattern as
`WS3_H2_path_audit.md`. No sprint or live-tape measurement in Phase 0.
Output: `WS3_H1_path_audit.md` with the pair-check trace and a
verdict ∈ {bug-found, suspected, clean}.

**Pair checks to perform** (each either confirms an invariant or
flags a candidate bug; this list is the audit's target surface, not
the audit's output):

| # | Check | Why it matters |
|:--|:------|:---------------|
| P1 | `signal_quality` uses `step_aggs[i].uncertainty` which is computed from **decayed** per-tribe curves at step *i*. Is the uncertainty source the pre-decay votes, the per-step aggregate, or a mix? | If uncertainty is computed against already-decayed votes, it structurally underestimates confidence at long horizons (decay pulls votes toward zero → uncertainty rises → signal_quality falls), producing a fake "conf degrades with horizon" pattern that isn't in the underlying signal. |
| P2 | `directional = |emove| / Σ |step_bps_i · kernel_i|` is net-over-cumulative. For any path with internal sign reversals the ratio < 1 even when the terminal sign is certain. | This penalises zigzag-but-convergent paths identically to zigzag-and-divergent ones. If real BTC returns have intra-horizon reversals near the consensus sign (they do — minute-level chop), `directional` is structurally sub-0.5 for correctly-signed forecasts. |
| P3 | `diversity_mult = 0.6 + 0.4 × diversity_score` applies as a **floor × multiplier**, not a floor-when-diverse. A single-category bloc gets `0.6×` (40 % haircut) on already-small base_conf. | The floor was presumably chosen to keep single-bloc signals from dominating. In practice every step's bloc status compounds 0.6× on already-sub-0.25 base_conf → final_conf ≤ 0.15. |
| P4 | `consensus_sign` is computed **before** `diversity_mult`; `diversity_score` is computed using `consensus_sign`. Does `_diversity_and_breakdown` actually need `consensus_sign`, or is it symmetric? | If diversity is sign-conditional, the score is biased against sign-divided councils (where half the tribes vote opposite) — but a sign-divided council is arguably high-diversity, low-confidence (correct direction unclear), and the bias might double-count. |
| P5 | `kernel = [self.confidence_decay ** i for i in range(n_steps)]` — with `confidence_decay < 1` the weight at step 0 is 1.0 and at step N is ~ε. Is this kernel used only for confidence, or does it also weight `expected_move_bps` (line 793)? | It weights both. If `emove` is short-horizon-dominated (true by construction) but `directional` normalizes against the same kernel-weighted denominator, the ratio is kernel-invariant — but any discontinuity in how `signal_quality` uses the same kernel vs. a different normalizer would skew one side of the ratio. |
| P6 | `abs_activity = sum(abs(b) × w for b, w in zip(step_bps_list, kernel))`. Is `step_bps_list` the consensus **cumulative curve** or **per-step deltas**? From the H2 path audit trace (`chess.py:760-762`), consensus `steps_bps` is cumulative bps. | **Cumulative branch (active per H2 audit):** `abs_activity = Σ|cumulative_i| × kernel_i`. `|emove|` is the kernel-weighted *terminal* of the cumulative curve. The ratio `|emove| / Σ|cumulative_i|·kernel` has no textbook interpretation — denominator grows monotonically with horizon (cumulative magnitudes accumulate), numerator doesn't necessarily grow, so `directional` is structurally smaller at long horizons **regardless of path shape**. Bug-level concern: `directional` is reading "average conviction of the cumulative magnitude stream" not "fraction of path length that was net displacement." **Delta branch (hypothetical):** if `step_bps_list` were per-step deltas, `abs_activity = Σ|Δ_i|·kernel` = kernel-weighted path length, and `directional = |net displacement| / path length` is the classical path-efficiency quantity (bounded in [0,1] with a clean geometric meaning). H1 audit resolves which branch is active: if cumulative, that's a likely bug worth fixing before any Option B/C intervention; if deltas, formula is clean and H1 proceeds against a structurally-defensible normalizer. |

**Expected audit output.** Either:
- **bug-found.** Concrete discrepancy between the formula and its
  stated intent (e.g. P6 — if the cumulative-vs-delta confusion is
  real, the entire `directional` denominator is the wrong units).
  Fix commit lands as a standalone change; H1 Phase 1 re-opens
  post-fix against the corrected formula.
- **clean.** No structural bug; formula is computing what the
  docstring says. Proceed to Phase 1 with the null hypothesis "the
  multiplicative stack itself is the bottleneck."
- **suspected.** Behaviour plausibly wrong but requires live-tape
  measurement to confirm. Annotate in the audit and proceed to
  Phase 1 treating the suspected item as a candidate explanation
  for the Phase 1 findings.

**Landing discipline.** Per `WS3_SCOPE.md` §9, Phase 0 ships as a
docs commit only. Any bug-fix surfaces as a separate feature commit
with its own determinism gate. Stacking audit + fix is forbidden
(attribution discipline, same rule that forced R2 into A and B).

## 3. Phase 1 — Live-tape decomposition measurement

### 3.1 Instrumentation (gating commit)

The auditor does not currently persist the conf decomposition.
Phase 1 opens with a single instrumentation commit.

**Scope:** extend `PendingAnchor` and `Resolved`
(`src/audit/backscore.py:31-61`) with:

- `signal_quality: float`
- `directional_score: float` (not to be confused with the boolean
  `directional_hit`; name matters)
- `base_conf: float`
- `diversity_score: float`
- `diversity_mult: float`

Plumb these through `ForecastAuditor.record_tick` and extend
`_persist` to write them into `data/forecasts/<day>.jsonl`. Add
matching fields to `Forecast` dataclass if not already present.
Payload byte-cost: 5 floats × ~21 k records/day ≈ 1.3 MB/day —
cheap.

**Determinism cost:** zero if the new fields are derived from
values already computed in `ChessProjector.project` (they are —
see `chess.py:794-812`). Determinism gate must PASS on the
instrumentation commit before Phase 1 measurement ships.

**Backfill:** impossible. Historical `data/forecasts/<day>.jsonl`
does not carry these fields; H1 acceptance runs against post-
instrumentation days only. This is a real scope cost — the 4-day
panel (04-18 through 04-21) does not support H1 decomposition;
H1 runs forward against new production days from the
instrumentation-commit landing date.

### 3.2 Measurement

On the post-instrumentation live-tape archive, compute:

- Per-field distribution: mean, p10, p25, p50, p75, p90, p95, p99
  across all anchors for a day.
- Multi-day aggregation across ≥ 3 consecutive days (matching D5
  panel discipline) to separate day-specific regimes from structural
  formula behaviour.
- Pairwise products: `signal_quality × directional`
  (= `base_conf`), `base_conf × diversity_mult` (= `final_conf`).
  These are already in the raw quantities, but the decomposition
  lets us ask "which factor is the binding constraint at the p95
  final_conf tick?" — the highest-conf ticks are where H1's
  structural cap would be most visible.
- Conditional: on the 236 WS1 trade-firing ticks (post-
  instrumentation equivalent), which factor is lowest?
  Distinguishes H1 from the aggregator-is-bottleneck variant.
- **Horizon stratification (required, not optional).** Compute every
  distribution above separately per horizon label (15s / 1m / 5m /
  10m — whichever subset the post-instrumentation archive carries).
  Rationale: the R2 step A attribution surfaced a 15 s sub-random
  hit rate (39.95 % long / 42.04 % short, N=18,505) flagged as "H1
  input, two-phenomena framing to confirm or refute." H1 clearing
  aggregate 52 % while leaving 15 s sub-random would be a strange
  result to land without understanding — stratification catches that
  case and separates "H1 fixes a single mixed phenomenon" from "H1
  fixes one horizon and a separate mechanism governs the other."

**Script:** `scripts/ws3_h1_decomp_analyze.py` (new), reads
`data/forecasts/<day>.jsonl` for a day list, emits table to
stdout + optional JSON artefact to
`research/phase11_diagnostic/_ws3_h1_decomp/`. Stratification by
horizon label is a required dimension of the output, not an
optional flag.

### 3.3 Expected signatures if H1 true

From `WS3_SCOPE.md` §3.3, pinned here:

- `signal_quality` mean ≤ 0.40, p95 ≤ 0.55.
- `directional_score` mean ≤ 0.60, p95 ≤ 0.80.
- `base_conf` p99 ≤ 0.30.
- `final_conf` p99 ≤ 0.25 (matching WS1's observed cap 0.2483).
- On the trade-firing subset, `directional_score` (not
  `signal_quality`) is the binding factor — distinguishes H1 from
  an aggregator-quality bottleneck.

**Per-horizon expectation (H1-true):** if the multiplicative stack
is the bottleneck, the cap should be present across *all* horizons
— the formula does not depend on horizon label. Expected post-
intervention lift is therefore **roughly uniform across 15s / 1m /
5m / 10m** (proportional to each horizon's pre-intervention conf
distribution, not a one-horizon-only lift).

**Per-horizon differential signature (H1-true with horizon
interaction):** if post-intervention lift is concentrated at long
horizons (5m / 10m) and leaves 15 s unchanged, H1 is partially true
but interacts with a horizon-specific mechanism (plausibly the
decay kernel — P1 / P5 in the Phase 0 audit). Treat as "H1 true
with a caveat" rather than clean H1-true; flag for H3 or a
targeted kernel audit rather than claiming clean H1 acceptance.

**Per-horizon differential signature (H1-false for 15 s):** if 15 s
hit rate remains sub-random post-intervention (i.e. < 45 %) even
when 1m/5m/10m clear 52 %, 15 s is governed by a mechanism H1 does
not touch — almost certainly the anti-informative-at-short-horizon
finding surfaced in R2 step A. H1 may still land its aggregate 52 %
win, but the 15 s subset needs a separate post-H1 investigation
(out of H1 scope; flag in the closeout).

If any of the structural signatures reverse (e.g. `signal_quality`
p95 > 0.8 but `final_conf` still caps at 0.25), the bottleneck is
**not** the compounding; it's the last factor whose range doesn't
explain the observed cap. H1 nulls; note which factor and proceed
to H3.

### 3.4 Kill criterion

**H1 is falsified** if `signal_quality` mean > 0.55 AND
`directional_score` mean > 0.70 on the post-instrumentation live
tape, i.e. both top factors produce usable ranges on average. In
that case the compounding is not binding — final_conf is small
because the council's *underlying signal* is small, and the formula
is correctly reporting that smallness. H1 closes as null; H3 opens.

Any single factor sitting at its expected-if-true range with the
others not does not falsify H1 — it just identifies which
intervention option (A/B/C) to try first.

## 4. Intervention options (ship only one per H1 pass)

Per `WS3_SCOPE.md` §3.4, carried forward. Each option targets the
factor Phase 1 identifies as the worst offender. Do **not** bundle
options; ship the strongest one, re-measure on the live tape, and
re-open for B/C only if A fails D3.

### 4.1 Option A — Flatten the multiplicative stack

Replace the product with a weighted sum:

```
conf_raw   = α × signal_quality + β × directional_score + γ × diversity_score
final_conf = clip(conf_raw, 0, 1)
```

With `α + β + γ = 1`. Weights chosen to preserve **ranking** of the
top-decile-by-pre-change-`final_conf` tick subset. Rank-preservation
check: Spearman ρ ≥ 0.85 between pre-change and post-change ranks
**on that top-decile subset**, rationale being that sizing decisions
are most sensitive to top-decile ordering — the bottom 90 % doesn't
trade. If subset choice becomes load-bearing at measurement time
(e.g. trade-firing subset is smaller than top-decile under post-
change thresholds), document the rationale for the alternative
subset in the intervention attribution doc; do not switch silently.

Headline effect: a tick with moderate signal on all three
dimensions lifts from 0.03-0.10 into the 0.4-0.6 range. Downstream
sizing/entry reacts — H1's acceptance is measured on whether the
resulting tick set produces ≥ 52 % directional hit at 20 s, not
whether conf is higher in isolation.

**Parameter sweep bound.** Starting point α=β=γ=1/3. If Phase 1
signals a single dominant offender, weight that factor higher (e.g.
α=0.5, β=γ=0.25 if signal_quality is worst). **Cap: 3 weight
configurations per A pass.** If none of the 3 configurations clears
panel-aggregate 52 % at 20 s, Option A is declared null even if
monotonic progress is observed across the 3 — sweep-to-overfit is
not an acceptable path. The 3 configurations are logged in the
attribution doc with pre-measurement rationale; the selection is
not a post-hoc choice.

**Determinism cost:** single formula change, pure function.
Determinism gate trivially re-runs.

### 4.2 Option B — Recalibrate `directional_score` only

Replace the net-over-cumulative ratio with net-over-max-abs-step:

```
directional_score = |expected_move_bps| / max(|step_bps_i · kernel_i|)
```

Preserves the multiplicative structure; addresses the P2 audit
concern directly (no path-length penalty for zigzag-convergent
paths). Simpler change than A; preserves the kernel-weighted
structure so A's weight-picking question doesn't arise.

**Prefer B over A** if Phase 1 identifies `directional_score` as
the single binding factor (signal_quality and diversity_score both
sit in usable ranges). B is a minimal-surface-area fix for that
case.

### 4.3 Option C — Reshape `diversity_mult`

Replace `0.6 + 0.4 × diversity_score` with a monotonic-but-non-
floor form:

```
diversity_mult = 1.0 − 0.4 × (1 − diversity_score)²   # or
diversity_mult = max(0.8, diversity_score)
```

Removes the 40 % floor haircut for single-bloc signals; still
preserves the "diverse council ≥ monolithic council" ordering.

**Prefer C** if Phase 1 shows `diversity_score` mean > 0.5 (most
ticks are diverse) but the 0.6 floor is hitting the minority of
single-bloc ticks with disproportionate haircut. Low-surface fix.

### 4.4 Combined-option discipline

Per `WS3_SCOPE.md` §7 execution rules: one intervention commit per
H1 pass, full determinism gate, full live-tape re-measurement. If
Option A lifts aggregate to 50-52 %, pause on A and try param
sweep within A before switching to B/C (Decision D4). If A nulls,
ship B against the original (pre-A) formula; do not stack.

**Multi-factor-binding tiebreaker.** If Phase 1 shows two factors
simultaneously binding (e.g. `signal_quality` mean 0.35 *and*
`diversity_score` mean 0.45, neither alone explaining the final_conf
cap), default to **Option A** (flatten the stack). Rationale: A
addresses multiplicative compounding generically — it does not
require identifying which single factor is "the" offender, and both
binding factors lift simultaneously under a weighted-sum structure.
Reserve B and C for single-factor cases where Phase 1 identifies one
dominant offender and the other two factors sit in clearly usable
ranges. The tiebreaker prevents thrashing between B and C on a case
where neither is individually sufficient.

## 5. Acceptance criteria

Per `WS3_SCOPE.md` §10 (unchanged):

**(a)** H1 Option A, B, or C ships and panel-aggregate directional
hit at 20 s ≥ 52 % across (04-21-…-04-??) — the panel dates shift
because of the instrumentation backfill problem (§3.1). Run the
post-instrumentation 3-day panel under each option; measurement
determinism gate must pass and no panel day can materially
regress.

**(b)** All three options ship, each measured in isolation, and
none clears 52 %. H1 is written up as null; H3 opens.

**Partial-lift stopping rule.** If any option lands in the
**48-51 %** range (meaningfully above the 44.5 % baseline but short
of the 52 % bar), **pause before running the remaining options**.
Partial lift admits two competing readings:

- *H1 partially true, H3 additive.* Mechanism is real but
  incomplete; combining H1's fix with an H3 intervention is the
  right next step. In this branch, exhausting the remaining H1
  options first is wasted work — they're sweeping within a
  single-mechanism model that Phase 1 already shows is incomplete.
- *H1 near-exhausted.* The remaining options would give diminishing
  returns (e.g. A lifted to 50 %, B/C plausibly add another 1-2 pp
  but not 4 pp). Completing the sweep is defensible for closeout
  evidence but not as a path to acceptance.

Default is not "always run all three." Pause at 48-51 %, reopen
the scope doc with Phase 1's per-factor numbers, and decide
explicitly between (a) combine with H3 now vs (b) complete the
H1 sweep for closeout completeness. This decision is a D4-style
reviewer checkpoint, not an autopilot.

**Explicitly not an acceptance criterion:** post-friction net PnL.
H1 is signal diagnosis only (D3).

## 6. Instrumentation catalog (new work)

| Commit | Scope | Determinism impact | Lines |
|:--|:--|:--|:--|
| H1.I1 | Extend `PendingAnchor`, `Resolved`, `_persist` with 5 conf-decomp fields; plumb through `record_tick` | None (derived values, no reorder) | ~40 |
| H1.I2 | `scripts/ws3_h1_decomp_analyze.py` — read `data/forecasts/`, emit distribution tables | N/A (post-hoc analysis) | ~150 |
| H1.0 | `WS3_H1_path_audit.md` — Phase 0 audit report | Docs only | N/A |
| H1.A / H1.B / H1.C | One of the three options, if Phase 1 + audit justify | One commit each; gate between | ~20-40 |

All instrumentation commits ship **before** any mechanism change
(§9 standing rule, §7 attribution discipline). H1.I1 is a chore
commit; H1.I2 is a docs/analysis commit; H1.0 is a docs commit;
H1.A/B/C are feature commits.

## 7. Execution sequence

```
  H1.0 (Phase 0 audit) ── bug-found? ── yes ──▶ standalone fix commit
          │ no                                          │
          ▼                                             ▼
  H1.I1 (auditor instrumentation)                re-audit post-fix
          │                                             │
          ▼                                             │
  determinism gate pass                                 │
          │                                             │
          ▼                                             ▼
  3+ days of production run       (merge back into main flow once
  accumulate live-tape data       H1.0 verdict = clean/suspected)
          │
          ▼
  H1.I2 (analysis script) — Phase 1 measurement
          │
          ▼
  Signature matches §3.3? ── no ──▶ H1 null → H3 opens
          │ yes
          ▼
  Choose A / B / C per the worst-offender factor
          │
          ▼
  Land option commit; determinism gate; 3-day re-measure
          │
          ▼
  Panel hit ≥ 52 % @ 20s?
   ┌───────┴────────┐
  yes               no
   ▼                ▼
  WS3 done   option-swap per D4 (≤ 3 options before H1 exits null)
```

**Wall-clock expectation.** H1.0 audit is a few hours of code
reading. H1.I1 + determinism gate is one day. Post-instrumentation
panel accumulation is the critical path — 3 production days at
minimum before Phase 1 has signal (more if the production tape is
quiet). Option commits + re-measure are another 3 days each.
Total H1 duration realistically **2-3 weeks** at best, longer if
options stack.

## 8. Out of scope

- Any change to the **aggregator** (votes → consensus mapping).
  That's H3 territory. H1 treats the council's output as an input.
- Any change to `MIN_GAIN` / `MAX_GAIN` / the kernel calibrator.
  H2 closed; H1 does not re-open it.
- Any change to tribe pool composition, weights, or
  categorisation. H3 Option A (decorrelation weighting) touches
  aggregator-input weights; H3 Options B/C are deferred to post-
  Phase-11. H1 leaves all of these untouched.
- Sprint-tape measurement of H1 signatures. Per `MEASUREMENT_INFRA.md`
  D6, sprint produces 400× weaker pressure; conf decomposition on
  sprint is not quantitatively transferable. Sprint is retained
  only for the determinism gate on each commit.
- PnL-based acceptance. D3 is fixed at directional hit.

## 9. Relationship to post-R2 fix queue

`PLAN.md` §5.1.1 lists two post-R2 fixes (A: divergence telemetry,
C: sprint-side dna_delta hardening). Both are orthogonal to H1:
Fix A is defensive instrumentation, Fix C is a sprint-only numerical
guard. Neither changes the conf formula or the emove pipeline. H1
can open in parallel with the Fix A/C queue; ordering is a
reviewer decision, not a dependency.

## 10. Provenance

- Predecessor scope: `WS3_SCOPE.md` §3 (2026-04-22) — original H1
  framing; superseded here only in the instrumentation and
  measurement sections (live-tape D6 rule, R2 closure).
- Carried decisions: D1 (H2 → H1 → H3 → H4 ordering), D3 (52 % @
  20s acceptance), D5 (multi-day panel), D6 (live-tape
  measurement). All unchanged.
- Standing rules: `WS3_SCOPE.md` §9 (audit-before-tune), §7
  (attribution discipline: one commit per mechanism change,
  determinism gate between).
- Predecessor artefacts: `WS3_H2_path_audit.md` (format template
  for Phase 0), `MEASUREMENT_INFRA.md` (D6 operating rule),
  `WS3_R2_step_A_attribution.md` + `_step_B_attribution.md`
  (R2 closure evidence that H1 opens against).

## 11. Parallel-workstream policy during H1 forward accumulation

Post-H1.I1, the critical path is **calendar time** — 3+ production
days must elapse against the instrumented auditor before Phase 1
has signal. This window is long enough that blocking other
workstreams on it would waste reviewer/agent cycles. Policy:

**Can proceed in parallel:** bounded audits that do not touch the
conf formula or the auditor write path.

- WS2 shadow-parity audits (execution-layer divergence telemetry).
- WS4 regime-hysteresis audits (regime classifier behaviour,
  separate module from `ChessProjector`).
- WS5 harvest-mass ablation studies (post-R2 step B; targets the
  harvest mass radar, which is upstream of conviction_boost, not of
  `final_conf`).
- WS3b warmup audits (anchor warmup behaviour pre-steady-state;
  separate from conf-decomposition measurement).
- `PLAN.md` §5.1.1 post-R2 queue (Fix A divergence telemetry,
  Fix C sprint dna_delta guard) — already declared H1-orthogonal
  in §9 above.

**Standing caveat — H1-perturbation check.** Any finding from a
parallel workstream that could perturb H1 measurement **pauses and
consults H1 before landing**. Examples of perturbation:

- A change to `ChessProjector.project` or any of its inputs
  (step_aggs, step_bps, kernel, diversity_score computation).
- A change to `ForecastAuditor.record_tick` ordering, filtering, or
  the `_persist` path that could alter which ticks enter the
  live-tape archive or how they're labelled.
- A change to the regime classifier that would alter which ticks are
  marked "trade-firing" (the conditional subset in §3.2).
- Any change that would force a live-tape archive format migration
  mid-window — H1 requires schema stability across the forward
  accumulation period.

Parallel workstreams that touch none of the above (pure
instrumentation, pure analysis, module-isolated fixes) do not need
to pause for H1.

**Not parallel-safe:** H3 work. H3 is sequenced after H1 per D1 and
will either re-open (if H1 nulls) or land against a different
formula shape (if H1 lands an option). Opening H3 during H1 forward
accumulation creates a double-intervention window where attribution
becomes intractable.

---

*Drafted 2026-04-24 on `feat/tensor-engine`. Awaiting reviewer
go-ahead per `PLAN.md` §5.2 Q1. No commits land on this scope
before that go-ahead.*
