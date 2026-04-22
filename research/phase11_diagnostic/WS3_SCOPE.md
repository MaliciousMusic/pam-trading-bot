# WS3 — Signal-Quality Diagnostic

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item                | Value                                                                |
|:--------------------|:---------------------------------------------------------------------|
| Status              | Scope agreed 2026-04-22; H2 path-audit is next action                |
| Predecessor         | WS1 (diagnosis B: weak signal, not exits)                            |
| Adjacent            | WS2.5 (regime gate — defensive; ships disabled pending multi-day)    |
| Dependencies        | Instrumentation commits `8df1666`, `2fab101` (WS1 §8 gaps closed)    |
| An-0 tape           | `data/events/2026-04-20.jsonl` (seed 42, 1 Hz, Merciless)            |
| Tape panel          | `data/events/2026-04-18.jsonl`, `-19`, `-20`, `-21` (all 3-day panel)|
| Determinism gate    | `scripts/verify_determinism.py --day 2026-04-20 --max-ticks 500`     |

## 0. Decisions (reviewer-confirmed 2026-04-22)

These answer the open questions that had been parked at the end of the
first-draft scope; they supersede any prior-probability ordering in §2.

| # | Decision |
|:--|:---------|
| D1 | **H2 first, then H1, then H3, then (conditional) H4.** Kernel gain is causally upstream of the confidence formula; any H1 measurement taken on top of an uncalibrated or mis-scaled projector is contaminated. |
| D2 | **Before any H2 intervention, audit the prediction→realized comparison path end-to-end for structural bugs.** A 4× systematic ratio (realized = 25 % of predicted) is more likely a double-applied regime multiplier, unit error, or `conviction_boost` leaking into prediction but not the realized-side comparison than it is a genuine calibration gap. Trace one forecast through projector → anchor creation → resolution; verify units and scaling at each step. Report findings **before** proposing the floor change. |
| D3 | **Acceptance bar = directional hit ≥ 52 % @ 20 s, aggregate across all regimes.** Explicitly *not* post-friction net PnL; coupling signal diagnosis to execution economics is deferred to a later workstream (working name "WS8 viability rerun"). Do not raise the bar beyond 52 % even if early measurements look strong — N=236 does not support tighter claims. |
| D4 | **H4 escalation is condition-based, not count-based.** Route to WS7 only if: (a) H2 is validated (path audit clean or fixed, floor change measured), AND (b) H1 interventions A / B / C are each measured in isolation and none clear 52 %, AND (c) H3 measurement + intervention also fails to clear 52 %. If any single H lifts aggregate to ≥ 50 % without clearing 52 %, pause and iterate on *that* H before moving to the next. |
| D5 | **Multi-day harness from day one.** Every H-measurement is run across the full 3-day tape panel (04-18, -19, -20, -21 with -20 as the An-0 anchor). WS2.5 v1-vs-v2 re-runs on the same panel land as a standalone section of whichever H intervention ships first. |

## 1. Headline

WS1 closed with diagnosis **(B)** — the council's aggregate signal carries no
frictionless directional edge at 20–250 s hold horizons, and confidence
never exceeded 0.25 over 86 399 ticks. WS3 opens the projector +
aggregator stack to find **where that collapse happens**, and produces a
ranked list of interventions that would raise signal magnitude /
confidence into an actionable range without introducing look-ahead.

**WS3 is diagnostic-first.** No production change ships from WS3 without
(a) an empirical measurement that identifies a mechanism, (b) a
minimally-scoped intervention targeted at that mechanism, (c) a
determinism-gate pass, and (d) evidence across the 3-day tape panel
that the intervention raises directional hit aggregate without
raising the false-positive rate on the WS1 counterfactual
(`direction_hit_20s < 50 %`).

Four hypotheses. Each gets its own § with: **(i)** measurement approach,
**(ii)** expected signature if the hypothesis is true, **(iii)**
intervention design if confirmed, **(iv)** kill criterion.

## 2. Hypotheses overview

| id  | short name                            | prior | exec | intervention class       | §  |
|:----|:--------------------------------------|:-----:|:----:|:-------------------------|:---|
| H2  | KernelCalibrator gain at floor        | HIGH  | 1st  | Path audit + unlock      | §4 |
| H1  | Confidence formula structurally caps  | HIGH  | 2nd  | Formula redesign         | §3 |
| H3  | Tribe vote correlation above 0.8      | MED   | 3rd  | Orthogonalize / weight   | §5 |
| H4  | Public tape signal-less               | LOW   | Last | Escalate to WS7          | §6 |

**Execution order is H2 → H1 → H3 → (conditional) H4** per Decision D1.
Kernel gain is causally upstream of the confidence formula, so an H1
measurement taken before H2 is resolved would be contaminated by any
scale / unit bug upstream.

H1 and H2 are testable from disk (`decisions.jsonl`,
`data/forecasts/…`, `data/calibration/projector_gain.json`) with no
new runs. H3 needs a per-tribe vote tape (add instrumentation, then a
sprint across the 3-day panel). H4 is a last-resort diagnosis — it
would mean PAM as currently specified cannot succeed on free-tier
data, which routes work to WS7 (premium flow data) and out of Phase 11
entirely.

## 3. H1 — Confidence formula is structurally conservative

### 3.1 Claim

The ChessProjector confidence formula multiplies three bounded-in-[0,1]
factors and clamps to [0,1]. With 24 archetype tribes that frequently
disagree, two of those factors compound toward zero on almost every
tick, producing the observed cap of ≈ 0.25 regardless of whether any
real directional signal is present.

The formula (`src/projector/chess.py:793-813`):

```
signal_quality  = Σ (1 − step_uncertainty_i) · kernel_i  / Σ kernel_i
directional     = |expected_move_bps| / Σ |step_bps_i| · kernel_i
base_conf       = signal_quality × directional             # ∈ [0, 1]
diversity_mult  = 0.6 + 0.4 × diversity_score              # ∈ [0.6, 1]
final_conf      = base_conf × diversity_mult               # ∈ [0, 1]
```

Where `step_uncertainty_i = 1 − |buy_mass − sell_mass| / active_mass` at
step *i*. Under balanced disagreement across 24 tribes, each step's
uncertainty hovers near 0.6–0.8, so `signal_quality` sits at 0.2–0.4.
`directional` is also sub-1 whenever any intra-path reversals exist
(it's the ratio of net displacement to cumulative path length — a coin
toss path has directional ≈ 1/√n). Multiplying two 0.2–0.4 factors and
then scaling by 0.6–1.0 produces ≈ 0.024–0.16. Observed max (0.2483)
fits this range exactly.

### 3.2 Measurement

Instrument the projector to emit, per tick, the decomposition:

- `signal_quality`
- `directional`
- `base_conf`
- `diversity_mult`
- `final_conf`

Add a one-line append to `data/forecasts/YYYY-MM-DD.jsonl` or a sibling
`data/diagnostics/conf_decomp-YYYY-MM-DD.jsonl`. Byte cost: ≈ 5 floats ×
86 400 ticks ≈ 3.5 MB/day. Determinism impact: none if logged after the
`final_conf` is computed (no reorder of state-mutating calls).

Then aggregate offline: mean / p50 / p95 / max of each factor, plus
their pairwise products, over the WS1 sprint day.

### 3.3 Expected signature if true

- `signal_quality` mean ≤ 0.40, p95 ≤ 0.55 across all 86 399 ticks.
- `directional` mean ≤ 0.60, p95 ≤ 0.80.
- `base_conf = signal_quality × directional` p99 ≤ 0.30.
- `final_conf` p99 ≤ 0.25 (matching the observed cap).
- On the 236 trade-firing ticks, `signal_quality` is **not** the
  binding factor — `directional` dominates the floor. (Distinguishes H1
  from a variant where the aggregator itself is the bottleneck.)

### 3.4 Intervention design if confirmed

**Option A — Flatten the multiplicative stack.**
Replace `signal_quality × directional × diversity_mult` with a
weighted-sum formulation where each factor contributes independently
and the final clamp only activates at a meaningful confidence floor:

```
conf_raw   = α · signal_quality + β · directional + γ · diversity_score
final_conf = max(0, min(1, conf_raw))
```

with α + β + γ = 1 chosen to preserve *ranking* of the top-decile
ticks vs. the current formula (regress old ranks on new, require
Spearman ρ ≥ 0.85). The headline effect: a tick where the council has
moderate signal on all three dimensions ends up at 0.4–0.6 instead of
0.03–0.10.

**Option B — Recalibrate only the `directional` factor.**
`directional` is currently net-over-cumulative-path. Replace with
`|expected_move| / max_abs_step`, which is bounded by n_steps and
doesn't penalize short-path wiggles around a monotone trend. Simpler
change; preserves the multiplicative structure.

**Option C — Make `diversity_mult` a multiplier only when diversity
is high, not an across-the-board 0.6× floor.**
Current `0.6 + 0.4 × diversity_score` means a single-category bloc
gets 0.6×, which is a 40 % haircut on already-small numbers. Change to
`1.0 − 0.4 × (1 − diversity_score)²` or `max(0.8, diversity_score)` —
preserves "diverse council ≥ monolithic council" ordering without
compounding toward zero.

Decision on A/B/C comes from the decomposition data: whichever factor
is the worst offender (lowest mean / highest false-zero rate) gets
recalibrated first. Do **not** combine all three interventions in one
commit; ship the strongest one and re-measure.

### 3.5 Kill criterion

If decomposition shows `signal_quality` mean > 0.55 and `directional`
mean > 0.70, the formula is NOT the bottleneck — the council is
emitting low-magnitude forecasts that correctly map to low confidence,
and H1 is falsified. Move on to H2 / H3 before any formula touch.

## 4. H2 — KernelCalibrator gain is pinned at its low-clamp floor

### 4.1 Claim

The `KernelCalibrator` rescales raw projector step predictions by a
fitted `bps_per_unit_pressure` gain in `[MIN_GAIN=1.0, MAX_GAIN=40.0]`
(`src/projector/kernel_calibrator.py`). Current on-disk state:

```json
{"updated_at": "2026-04-21T18:32:41", "gain": 1.5704, "n": 1595,
 "last_correction": 0.25}
```

`last_correction = 0.25` is the most recent EWMA of
`median(|realized| / |predicted|)` across the 1595-sample window. A
ratio of 0.25 means realized moves are only 25 % of predicted — the
calibrator is being asked to *shrink* predictions by 4× but is floored
at 1.0 (it cannot reduce below `MIN_GAIN`, and the prior gain was
~1.57, so it has crept down but is now near the floor). Every future
correction that asks for further shrinkage is silently clamped.

This matters because the floor was chosen as a **defensive guardrail**
against a degenerate case (calibrator near zero → forecasts collapse
to zero → zero trades). It was not chosen with evidence that 1.0 is the
correct minimum. If the true `bps_per_unit_pressure` on this regime is
~0.4, the projector is systematically outputting 2.5× the magnitude the
market actually realizes, which would *over-estimate* `expected_move`
and artificially *inflate* `directional` in the conf formula — this is
the opposite of H1's failure mode and would produce a different
signature.

### 4.2 Measurement

H2 has two phases. Phase 0 is a structural-bug audit that **must
complete and report before any intervention is proposed** (per
Decision D2). Phases 1+ are the measurements already scoped.

#### Phase 0 — Prediction → realized path audit (gating step)

A 4× systematic mismatch (realized = 25 % of predicted) is too large
to be attributable to a single miscalibrated EWMA. More plausible
root causes, in rough order:

- **Double-applied regime multiplier.** `ChessProjector.regime_multiplier`
  is set by the engine at tick time and used when composing
  `steps_bps`. If the same multiplier is also being applied by the
  decision engine (e.g. on `expected_move_bps` during intent
  construction) or by the executor on the resolved side, predicted
  carries it twice, realized carries it once (or zero times), giving
  a fixed 1/multiplier ratio.
- **`conviction_boost` leaking into prediction but not the realized
  comparison.** `steps_bps` is multiplied by `conviction_boost` in
  `chess.py:769-770`. If `ForecastAuditor.record_tick` stores the
  boosted `steps_bps` as the prediction but the realized side is
  computed against raw mid-price movement, the prediction is
  effectively inflated by `conviction_boost`.
- **Unit error.** `bps_per_unit_pressure` has units of
  bps / (unit of `net_pressure`). If `net_pressure` is in `[-1, 1]`
  but a downstream stage treats it as already in bps, one side loses
  a factor equal to the typical pressure magnitude.
- **Anchor horizon mismatch.** Prediction is the projector's
  `steps_bps[step_idx - 1]` at horizon *T*. Realized is
  `(price_at_t+T − price_at_t) / price_at_t × 10000`. If the anchor
  index is off-by-one (0-based vs 1-based) or the realized side reads
  the wrong tick, the comparison is between two different horizons —
  small horizon vs a 2–4× longer one produces exactly the kind of
  magnitude gap we're seeing.
- **Step accumulation vs instantaneous step.** Prediction might be
  cumulative at horizon *T* (sum of first *k* steps) while the anchor
  resolution expects one-step-ahead. Or vice versa.

**Audit procedure.** Instrument the projector and auditor temporarily
to emit, for a single deterministic tick (e.g. the 1000th tick of the
An-0 day) a trace record with every intermediate quantity named and
valued:

```
tick_ts
state.last_price
state.last_funding_rate
regime_state.multiplier
per_tribe_raw_curves       # before any gain/boost
kernel_calibrator.gain
conviction_boost
steps_bps_post_gain_pre_boost
steps_bps_final             # what decision + auditor see
anchor_spec                  # (label, step_idx)
anchor_prediction_bps       # steps_bps_final[step_idx - 1]
anchor_ts_target            # tick_ts + step_idx * tick_seconds
price_at_target
realized_bps                # computed at anchor resolution
realized_formula            # literal string of what ForecastAuditor evaluates
```

Cross-check each pair by hand:
- Does the regime multiplier appear *exactly once* on the prediction
  side and *never* on the realized side?
- Does `conviction_boost` appear on prediction and not on realized?
  (If yes — is that intended? The current ledger records
  `conviction_boost` on the intent but the realized side of the
  auditor doesn't scale by it. Gap.)
- Does `anchor_prediction_bps` use `step_idx − 1` (0-based array
  index) while `price_at_target` uses `+ step_idx × tick_seconds`
  (1-based time)? Off-by-one check.
- Are both bps quantities normalized to the same price basis
  (mid vs. last-trade vs. mark)?

Output: one-file report at
`research/phase11_diagnostic/WS3_H2_path_audit.md` with the raw trace
table, the pair-check findings, and a verdict of **{bug-found,
suspected, clean}**. If `bug-found`, describe the bug, propose the
fix, and **stop** — the floor change is shelved until the bug is
resolved and the 0.25 ratio is re-measured post-fix. If `clean` or
`suspected without root cause`, proceed to Phase 1.

This phase ships as a small instrumentation commit (trace logging
behind a flag, one-shot) plus a docs commit with the audit report.
No mechanism changes.

#### Phase 1 — Ratio distribution (post-audit only)

Pull every record in `data/forecasts/2026-04-20.jsonl` with a
resolved `realized_bps`. Compute `realized / predicted` per record
(preserve sign — we care whether the calibrator is biased or just
over-magnituded). Tabulate mean, median, p10/p90 by horizon label
(`15s`, `1m`). Repeat across the 3-day panel (04-18, -19, -21) to
check whether the 0.25 ratio is An-0-specific or structural.

#### Phase 2 — Calibrator trajectory

Read `projector_gain.json.bak_*` back through whatever history we
have; chart `gain` over time. If it decayed monotonically from a
prior larger value and now is stuck at ~1.57 with `last_correction`
stuck at 0.25, the floor is binding.

#### Phase 3 — Counterfactual (only if audit clean and Phases 1-2 confirm floor binding)

Re-run across the 3-day panel with `MIN_GAIN=0.2` (temporary patch).
Measure: does `|expected_move_bps|` mean *drop* (predictions shrink to
match realized) and `directional_hit @ 20s` *rise* (the calibrated
direction becomes more reliable even as magnitude shrinks)?
Confidence may rise or fall depending on H1 dominance.

### 4.3 Expected signature if true

- Query (i): median `realized / predicted` ≈ 0.25 ± 0.10, consistent
  across horizons.
- Query (ii): gain series monotonically decreasing, recent weeks
  pinned at or within 5 % of `MIN_GAIN`.
- Query (iii): `|expected_move_bps|` mean drops by ~4× (the pinned
  correction); `directional_hit` at 20 s rises from 44.5 % (WS1) to
  ≥ 50 % — because the projector's magnitude was over-driving the sign
  in low-signal ticks.

### 4.4 Intervention design if confirmed

If Phase 0 surfaces a structural bug, the "intervention" is the
bug-fix and no floor change is needed. The options below apply only
if the audit is clean and Phases 1–2 confirm the floor is binding on
a genuinely-shrunk realized distribution.

**Option A — Lower the floor.**
Drop `MIN_GAIN` from 1.0 to 0.2. Add a separate guard: if the
calibrator would ask for `gain < MIN_GAIN`, log a warning but let it;
only clamp to 0 if `gain < 0.05` (numerical sanity). One-line change in
`src/projector/kernel_calibrator.py`; low-risk because calibrator is
EWMA-smoothed so a floor drop doesn't immediately destabilize.

**Option B — Add an `expected_move` shrinkage decoupled from `gain`.**
Keep `MIN_GAIN=1.0` for the per-tribe curve shape, but add a final
`expected_move_bps *= shrinkage_factor` term driven by a slower EWMA
of the realized/predicted ratio. Cleaner separation: shape vs. scale.
Higher complexity; wait for A to fail before considering B.

**Option C — Drop the gain entirely.**
If the gain was always a fudge factor, replace `predicted × gain` with
`predicted × empirical_atr_ratio` where `atr_ratio` is computed from
the last N minutes of bar range. Bypasses the calibrator when there's
live volatility data; fallback to gain only in gaps. Most invasive —
only if A and B both fail.

Determinism note: changing `MIN_GAIN` alters the gain that gets
persisted after each session, so byte-identical determinism on
subsequent days requires pinning the gain file to a known snapshot or
disabling calibration persistence during verify_determinism runs. The
existing determinism harness needs a small patch (frozen
`projector_gain.json` fixture) before this intervention can be gated.

### 4.5 Kill criterion

Two ways H2 exits the pipeline as resolved:

- **Audit finds a bug.** Fix lands as a separate feature commit; post-fix
  `realized / predicted` median is re-measured. If the fixed ratio is
  ≥ 0.7 and gain sits comfortably above `MIN_GAIN`, H2 is resolved via
  bug-fix rather than floor-change. H2 intervention (§4.4) is not
  needed; proceed to H1.
- **Phase 1 shows `median(realized / predicted) ≥ 0.7`** or gain is
  well off the floor (≥ 2.0 and stable). Calibrator is not binding;
  H2 is falsified. Proceed to H1.

If the floor-change intervention ships and the An-0 directional hit at
20 s fails to clear 52 % on its own, H2 is not "killed" but it's no
longer the binding constraint — proceed to H1 with the post-H2 gain
in place.

## 5. H3 — Tribe vote correlation exceeds the diversity threshold

### 5.1 Claim

The memory note from 2026-04-18 (`project_council_redundancy.md`)
flagged that "3 tribes produce near-identical signals." If the
24-tribe pool is substantially correlated (pairwise signed-vote
correlation > 0.8 for large subsets), the aggregator is effectively
running with ≪ 24 independent voters, which has two downstream
consequences:

- `diversity_score` is *high* numerically (all tribes active) but not
  *informational* (they're all reading the same underlying signal).
  Confidence math rewards the inflated diversity.
- `signal_quality` (1 − uncertainty) appears moderate because
  correlated tribes agree, but the agreement is not independent
  evidence — it's the same bet counted 24 times.

Observation supporting this: tribe skill EWMAs in
`data/calibration/tribe_skill.json` are all in `[0.498, 0.502]`, i.e.
every tribe hits exactly at random. With 24 genuinely independent
random voters you'd expect the aggregate to beat chance via variance
averaging; we observe it doesn't (WS1: 44.5 % hit). Consistent with
heavy correlation.

### 5.2 Measurement

Add per-tribe vote logging to the sprint tape. Simplest form: append
`{tid, side, conviction, weight}` for each of the 24 tribes to the
decision record (one-tick cost: ~300 bytes, ~25 MB/day). Then
offline:

**(i)** Pairwise signed-vote correlation matrix (24×24). Signed vote =
`+conviction` if BUY, `−conviction` if SELL, 0 if ABSTAIN. Compute
Pearson ρ across all 86 399 ticks. Any pair with ρ > 0.8 is
redundant.

**(ii)** Effective number of independent voters — ratio of sum of
eigenvalues of the correlation matrix to the largest eigenvalue, or
equivalently `N_eff = (Σ λ)² / Σ λ²`. Target: how close is N_eff to
24 vs. to 1?

**(iii)** Category-level correlation. Group tribes by archetype
category (TA, order-flow, flow-buyer, options, arb, degen, …) and
measure within-category vs. across-category ρ. High within-category
ρ is expected ("TA day-trader" + "CTA trend" both read momentum);
high across-category ρ is the surprise that breaks the diversity
assumption.

### 5.3 Expected signature if true

- ≥ 30 % of the 276 pairs have ρ > 0.8.
- `N_eff` ≤ 8 (i.e. the 24-tribe council behaves like an 8-voter
  council under independence assumptions).
- At least one across-category pair with ρ > 0.9 (truly redundant
  tribes across what should be orthogonal features).

### 5.4 Intervention design if confirmed

**Option A — Decorrelation weighting.**
Replace `weight_i = fixed_prior` in the aggregator with
`weight_i = 1 / Σ_j (1 + ρ_ij)`, normalized. Tribes with many
high-correlation neighbors get down-weighted. Preserves all 24 tribes
(don't delete model capacity), adjusts how the aggregate combines them.

**Option B — Prune + reassign.**
For each cluster of tribes with internal ρ > 0.8, collapse to a
representative. Reassign the freed weight to under-represented
archetypes. Harder to justify individually; deletes council diversity
in form even while preserving it in function.

**Option C — Replace redundant tribes with orthogonal inputs.**
Per the council-redundancy memory: the long-term fix is adding tribes
that read features no existing tribe reads (option flows,
cross-asset, derivatives positioning). This is WS-next scope, not
Phase 11 — flagged here for continuity.

Prefer A (least invasive; reversible by setting all weights equal) for
the Phase 11 intervention. B and C are staged follow-ups.

### 5.5 Kill criterion

If the correlation matrix shows ≤ 5 % of pairs with ρ > 0.8 and
`N_eff ≥ 18`, the 24 tribes are genuinely orthogonal and the problem
is not in the aggregator — move to H1/H2 as the true binding
constraints.

## 6. H4 — Public tape is (approximately) signal-less at 5–60 s horizons

### 6.1 Claim

Final-resort hypothesis. If H1, H2, H3 all fail to recover positive
frictionless edge, the remaining explanation is that **the data feeds
the bot currently ingests** (Binance public 1 s klines + funding +
open-interest + liquidations) **contain no predictable directional
signal** at PAM's 5–60 s operating horizon under merciless execution.

Empirical pre-evidence:

- `data/forecasts/2026-04-20.jsonl`: directional hit at 5 s = 43.72 %
  (anti-informative). At 60 s = 49.68 % (indistinguishable from
  coin flip).
- All 24 tribe skill EWMAs sit in `[0.498, 0.502]`. No tribe has
  individual edge on this data.
- Mean `|realized|` at 1 m ≈ 4.2 bps vs. mean `|expected|` ≈ 0.64 bps
  — the projector is under-predicting magnitude, but the *direction*
  is no better than chance, so magnitude calibration alone (H2) can't
  turn a negative-edge signal positive.

### 6.2 Measurement

This is confirmed **by elimination** under Decision D4, not by a
direct test. The condition for escalation is:

1. **H2 is validated** — either the Phase 0 audit found a bug and the
   fix landed, or Phases 1–3 shipped the floor change — and the
   post-H2 sprint still fails the 52 % bar.
2. **AND H1 interventions A, B, and C are each measured in isolation**
   (one commit each, one re-measure each), and none clears 52 %
   aggregate on the 3-day panel.
3. **AND H3 measurement + intervention** (decorrelation weighting or
   prune+reassign) also fails to clear 52 %.

If any single H lifts aggregate to ≥ 50 % without clearing 52 %,
pause and iterate on *that* H (swap to a different option within it,
re-measure) before moving to the next hypothesis. Escalation fires
only when the full sequence is exhausted.

Direct test (optional, only if elimination is ambiguous): take the raw
Binance BTCUSDT 1 s bar close-return series for 2026-04-20, fit
straightforward baselines (ARIMA(1,1,1), LSTM next-5-second direction,
simple momentum over last-30-seconds) on the first half of the day and
test on the second. If none of these exceed 50.5 % directional
accuracy on out-of-sample 5–60 s forward returns, the tape itself is
the limit and PAM's signal is bounded above by whatever noise the
public feeds contain.

### 6.3 Expected signature if true

- Post-H2 + post-H1 (all of A/B/C) + post-H3 aggregate directional hit
  at 20 s across the 3-day panel remains < 52 % and, for full
  confirmation, also < 50 % (no residual signal lifted any H into the
  50–52 % pause band).
- Council `|expected_move_bps|` may rise but realized direction
  quality does not follow.
- Optional direct test: baseline ARIMA / momentum on raw bars also
  produces ≤ 50.5 % out-of-sample directional accuracy on any panel
  day.

### 6.4 Intervention if confirmed

**Not in Phase 11.** WS7 (orthogonal inputs — premium order-flow data,
CEX books, on-chain flows) is the structural fix. WS3 closes with a
written "stop-loss" determination for Phase 11 and a handoff to WS7
scope. This path means PAM-as-currently-specified cannot be made
profitable on free-tier public data; that's an honest finding worth
recording, not a failure.

Budget implication (per `project_budget_constraint.md`): WS7 is
blocked on a conversation with the user about paid data. WS3 surfaces
the question; it doesn't unilaterally answer it.

### 6.5 Kill criterion

Any H2 / H1 / H3 intervention raising panel-aggregate directional hit
at 20 s to ≥ 52 % kills H4 and closes WS3 via acceptance path (a). A
50–52 % result pauses the pipeline on that H for an option-swap
iteration (Decision D4), not an H4 escalation.

## 7. Execution order & decision tree

Run the hypotheses in the order **H2 → H1 → H3 → (conditional) H4**
(Decision D1). H2 opens with a mandatory path audit (Decision D2)
before any measurement or intervention. Each hypothesis has its own
measurement commit(s) **before** any intervention commit, so that we
can re-measure after an intervention fires and distinguish "the
intervention worked" from "the measurement was wrong."

```
  H2 Phase 0 (path audit) ── bug found? ── yes ──▶ fix commit
          │ no                                          │
          ▼                                             ▼
  H2 Phases 1-2 (disk measurements)             re-measure ratio
          │                                             │
          ▼                                             ▼
  floor-binding?  ── no ──▶ H2 falsified ─┐     ratio ≥ 0.7?
          │ yes                           │           │ yes
          ▼                               │           ▼
  H2 Phase 3 intervention                 │     H2 resolved via fix
          │                               │           │
          ▼                               ▼           ▼
        hit@20s ≥ 52 % aggregate across 3-day panel?
          ┌────────────────┴────────────────┐
         yes                                no
          ▼                                 ▼
   publish WS3 report                 H1 measure + intervene
                                            │
                                            ▼
                                     hit@20s ≥ 52 % ?
                                     ┌──────┴──────┐
                                    yes            no
                                     ▼             ▼
                              publish         H3 measure
                                                   │
                                                   ▼
                                            hit@20s ≥ 52 % ?
                                            ┌──────┴──────┐
                                           yes            no
                                            ▼             ▼
                                       publish      H4 escalation
                                                    (WS7 request)
```

**Rules:**

- At most one intervention commit ships per hypothesis without a
  full determinism-gate re-run and a fresh sprint metric check against
  WS1 baseline. We do **not** stack H1 + H2 + H3 changes in a single
  sprint; attribution becomes impossible.
- "hit@20s" is **aggregate across all regimes** on the 3-day tape
  panel, not An-0 alone, not `trend_up` alone (Decisions D3, D5).
- If any single H lifts aggregate to ≥ 50 % without clearing 52 %,
  pause and iterate on that H (different option — A / B / C) before
  moving to the next hypothesis (Decision D4).

## 8. Out-of-scope for WS3

- **Exit logic.** WS1 closed exits. Any re-open of exit work must cite
  new evidence, not WS3 work product.
- **Regime gate tuning.** WS2.5 shipped, disabled, pending multi-day.
  WS3 does not re-tune the gate; if signal quality rises under a WS3
  intervention and makes the gate less useful, we decide what to do
  about the gate *after* the WS3 result is measured.
- **Tribe replacement or new archetypes.** H3 Option C is flagged but
  deferred. WS3 diagnoses redundancy; replacement is WS4+.
- **Premium data sourcing.** H4 Option is deferred to WS7.
- **Model replacement (transformer / RL).** Out of Phase 11 scope.
- **Live trading.** Sprint-only until WS3 ships a positive-edge result
  that survives cross-seed + multi-day on the WS2.5 holdout tapes.

## 9. Instrumentation & tooling required before WS3 starts

Already in place (from `8df1666`, `2fab101`):

- Ledger close/open events carry `regime_label` + `steps_bps` +
  `peak_favorable_s`.
- SprintEngine wires ForecastAuditor → per-run
  `forecasts/YYYY-MM-DD.jsonl` inside each sprint's `run_dir`.
- Projector anchor specs filter unreachable horizons at 1 Hz tick.

Needs adding in WS3:

- **For H2 Phase 0 (path audit):** one-shot trace instrumentation —
  projector + auditor emit the full intermediate-quantity list from
  §4.2 for a single configured tick. Behind a `--trace-tick N` CLI
  flag on a dedicated audit script; no change to production run code
  paths. ≤ 40 lines.
- **For H2 Phase 3 (counterfactual):** a `MIN_GAIN` override on the
  `KernelCalibrator` constructor (don't patch the module constant —
  the calibrator is loaded once from disk on engine init; an override
  parameter is cleaner and keeps determinism-gate behaviour frozen
  via fixture). ≤ 15 lines. Plus a frozen
  `projector_gain.json` fixture for the determinism harness so the
  on-disk calibrator state doesn't drift during verify runs.
- **For H1:** per-tick conf decomposition log (new
  `data/diagnostics/conf_decomp-*.jsonl` or extension of
  `decisions.jsonl`). ≤ 40 lines, one commit.
- **For H3:** per-tick per-tribe vote log (new
  `data/diagnostics/tribe_votes-*.jsonl`). Bigger — ≈ 25 MB/day.
  Ship behind a `diagnostic_mode` config flag so it doesn't bloat
  production runs.

Each instrumentation change is a standalone chore commit that must
pass `verify_determinism.py`. No measurement infrastructure ships in
the same commit as a mechanism change — they must be separable for
attribution.

## 10. Acceptance criteria for "WS3 delivered"

WS3 is complete when at least one of the following is true:

**(a)** A single intervention (H2 bug-fix, H2 floor change, H1
mechanism change, or H3 decorrelation) ships that raises **aggregate
directional hit at 20 s across the 3-day tape panel to ≥ 52 %**, with
determinism gate passing and no materially worse outcome on any panel
day. The 52 % bar is fixed per Decision D3 — do not raise it even if
early measurements look strong.

**(b)** A written null result for H2 + H1 + H3 (each H measured and at
least one intervention attempted per H that did not clear 52 %), that
elevates H4 and formally requests WS7 scope via the condition in
Decision D4.

Explicitly **not** an acceptance criterion: post-friction net PnL on
An-0. Coupling signal diagnosis to execution economics is deferred to
a later workstream (working name "WS8 viability rerun"). WS3 measures
signal; whether that signal is large enough to clear friction is a
separate question.

Either outcome is a Phase 11 deliverable. We do **not** keep WS3 open
indefinitely hunting for incremental wins; the purpose is
diagnosis + one shot at the binding constraint per hypothesis, not an
open-ended tuning loop.

## Appendix — reproduction (to be implemented)

```bash
# H2 Phase 0 — path audit (one-tick trace, no mechanism change)
PYTHONPATH=. python -m scripts.ws3_h2_path_audit \
    --day 2026-04-20 --seed 42 --trace-tick 1000
# → writes research/phase11_diagnostic/WS3_H2_path_audit.md

# H2 Phase 1 — ratio distribution across panel
PYTHONPATH=. python -m scripts.ws3_h2_ratio_dist \
    --days 2026-04-18 2026-04-19 2026-04-20 2026-04-21

# H2 Phase 2 — calibrator trajectory (reads backup snapshots)
PYTHONPATH=. python -m scripts.ws3_h2_gain_trajectory

# H2 Phase 3 — floor-change counterfactual across panel
PYTHONPATH=. python -m scripts.ws3_h2_min_gain_sweep \
    --days 2026-04-18 2026-04-19 2026-04-20 2026-04-21 \
    --seed 42 --min-gains 0.2 0.5 1.0 2.0

# H1 — conf decomposition sprint + analysis (post-H2)
PYTHONPATH=. python -m scripts.ws3_conf_decomp --days ... --seed 42
PYTHONPATH=. python -m scripts.ws3_conf_decomp_analyze \
    --run-dir data/replays/ws3_h1_measure/

# H3 — tribe-vote correlation sprint + analysis (post-H1)
PYTHONPATH=. python -m scripts.ws3_tribe_votes --days ... --seed 42
PYTHONPATH=. python -m scripts.ws3_correlation_matrix \
    --run-dir data/replays/ws3_h3_measure/

# Determinism gate after any mechanism change
PYTHONPATH=. python -m scripts.verify_determinism \
    --day 2026-04-20 --max-ticks 500
```

---

*Drafted 2026-04-22 on `feat/tensor-engine`. Reviewer decisions D1-D5
folded in same day. Next action: H2 Phase 0 path audit — report to
`research/phase11_diagnostic/WS3_H2_path_audit.md`, pause for review
before any H2 intervention.*
