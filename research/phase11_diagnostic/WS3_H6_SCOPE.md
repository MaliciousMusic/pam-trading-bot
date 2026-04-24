# WS3 H6 — DNA-recursion positive-feedback audit

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item | Value |
|:--|:--|
| Status | Scope-first, opened 2026-04-24 post-H5.A commit. Phase 0 **NOT started** (audit-before-fix discipline) |
| Origin | `WS3_H5_phase0_audit.md` §3 root-cause ladder row 6 — "Source of huge `curve[h]` — DNA-driven recursive amplification, 🟡 not audited in Phase 0" |
| Predecessors | H5.A committed (projector saturation clamp). H6 **depends** on that clamp being live: without it, restart drifts into 1e+295 territory inside ~hours and any DNA instrumentation reads are swamped |
| Operating rule | `MEASUREMENT_INFRA.md` §2 — H-acceptance reads `data/forecasts/<day>.jsonl`; for H6 the primary read is **per-tribe curve traces**, which are **not currently persisted** (→ I-chip dependency, see §3) |
| Acceptance bar | Identify ≥ 1 DNA category with measured `∂f/∂cum_bps > 0.005` (= mean_reversion_rate floor) on ≥ 5 % of ticks in the pathological baseline **or** formally rule out DNA-driven feedback as the mechanism |
| Standing rule | Audit-before-tune (`WS3_SCOPE.md` §9) — Phase 0 structural audit before any DNA-function change |
| Baseline | `data/baseline/pathological_2026-04-24_pre-H5A/` (committed) — 12,011 rows exhibiting the explosive dynamic pre-clamp |

## 0. Headline

H5.A installed a saturation clamp at the **output** of the projector
(`step_bps_list` sanitized to ±500 bps, `saturation_hit` stamp + forced
`conf=0`). That clamp is observational containment, not a cure. The
underlying dynamic that drives per-tribe `curve[h]` to magnitudes up to
~10^150 in a few hundred steps **is still live** inside the recursive
walk at `chess.py:561-619`.

**H6 claim.** At least one DNA function at `src/dna/*.py` (the set of
`dna_fn(DNAContext)` handlers invoked inside the per-tribe walk) is
emitting `dna_delta` such that the effective recursion

```
cum_bps[h+1] = cum_bps[h] · (1 − mean_reversion_rate) + total_bps · scale
total_bps    = dna_delta + event_bps
dna_delta    = dna_fn(DNAContext(prev_state_bps=cum_bps, …))
```

crosses `∂(cum_bps[h+1])/∂cum_bps[h] > 1` for a non-trivial fraction of
ticks. With `mean_reversion_rate = 0.005` that threshold is
`∂(dna_delta)/∂cum_bps > 0.005` — a very low bar for a function that
receives `prev_state_bps` directly as context.

**Why it matters post-H5.A.** With the clamp live, the bot will *hold*
on saturated ticks (zero trade risk), but every saturated tick is also
a tick the council cannot act on — silent coverage loss. If H6 proves
DNA-recursion is driving, say, 20 % of ticks into saturation, that's
20 % of the tape the projector is producing but the decision engine
cannot use. The fix is DNA-side, not projector-side, and H5.A buys
time but doesn't recover that coverage.

**What H6 does *not* claim.** H6 does not claim the mean-reversion
damping is wrong, the kernel decay is wrong, or the calibrator is
wrong. It claims the DNA-delta function family produces
feedback-positive outputs on some subset of the DNA categories, and
Phase 0 identifies which.

## 1. Antecedents

- **H5 Phase 0 (2026-04-24).** Established the mathematical-fingerprint
  evidence: step_bps_signflips=1-5 + directional=1/99 attractor is
  explained by single-horizon spike in `steps_bps`, inherited from
  `curve[h]` of one or more tribes. Weighted consensus at
  `chess.py:749-769` cannot amplify (ratio of weighted means with
  bounded weights); the magnitude must originate in per-tribe `curve`.
- **H5.A (2026-04-24).** Saturation clamp at `chess.py:808-833`. Does
  **not** touch per-tribe recursion or DNA. Explicitly documented as
  "observational containment only" — H6 is the curative track.
- **H3 Phase 0 §P4 (2026-04-24).** First documented the 99.000 attractor
  as "numerical, not correlation". H5 closed the numerical path; H6 now
  chases the generator.

## 2. Q-questions

**Q1 — Which DNA category produces the spike?**
Hypothesis: one of the TA-cluster categories, because they are
historically the most conviction-driven and H3 established TA is over-
represented in the council. But speculative — need measurement.

**Q2 — Is the spike a single-horizon event (step h=h*) or a cascade
starting at h* and propagating?**
The H5 audit established signflips=1 which is consistent with a
single-step spike + zero-else. But `step_bps_list[h] = cum_bps[h] −
cum_bps[h−1]` — so a single non-zero entry in `step_bps_list` means
`cum_bps` jumped once then stayed flat. That's compatible with a
single DNA-fn call returning a HUGE `dna_delta` at step h*, after
which `mean_reversion` pulls it back toward zero in one step. Needs
verification.

**Q3 — Does the feedback require a price-spike input, or does it
self-initiate from noise?**
Critical for fix prioritization. If self-initiating, the DNA function
is broken regardless of upstream input quality. If spike-triggered,
H6 might close by rate-limiting `prev_state_bps` fed into
`DNAContext` rather than touching the DNA body.

**Q4 — Is the instability symmetric (positive AND negative cum_bps
explode) or one-sided?**
Tape evidence suggests both signs (`emove ≈ ±10^120`), but not
confirmed at tribe level. One-sided failure narrows the candidate
DNA function set.

**Q5 — What fraction of per-tribe `curve[h]` trajectories in the
pathological baseline exhibit `|curve[h+1] − 0.995 · curve[h]| >
some_threshold · |curve[h]|`?**
Direct measurement of the feedback coefficient on the committed tape.
Gives the Phase 0 acceptance metric a numerical target.

## 3. I-chip dependency — `tribe_curve_trace`

Per-tribe `curve[h]` trajectories are **not currently persisted**.
`chess.py:780-781` records per-tribe curves into a local `_ledger`
deque for weight updates but does not write them to disk. Phase 0
cannot proceed on the committed baseline alone — the baseline tape
has aggregated `steps_bps` only.

**Required before H6 Phase 0:**

- **I6.I1** — per-tribe curve sampler. New field on Forecast or a
  parallel `tribe_curves_<day>.jsonl` sink persisting
  `{tid → curve}` for every K-th tick (K chosen to stay under a size
  budget — curve is 250 × N_tribes floats = ~30 KB/tick uncompressed,
  a full day at 1 Hz would be unwieldy; sample every 10-60 s).
- Instrumentation can be **zero-branch additive** — persist under a
  new field default-None, drop in via the H5.I1 pattern.
- Estimated scope: 1-2h, 2 files (chess.py sampler wiring, new
  persistence path), + 1 unit test.

**Phase 0 cannot start until I6.I1 ships and produces ≥ 1 hour of
sampled tape on pathological input.** Restarting the bot without
H5.A → drift → useless data; restarting with H5.A → DNA still runs
upstream of the clamp → tape is clean. So the plan is:

1. H5.A committed (done).
2. Restart detached bot with H5.A live (upcoming).
3. **While that runs**, spec + implement I6.I1 (parallel to user-check
   review). Do NOT start Phase 0 audit without data.
4. Restart bot with I6.I1. Collect ≥ 1h of sampled curve tape.
5. Phase 0 reads that tape. Box: 1h-2h.

## 4. Phase 0 audit plan (when data is available)

**Box:** 2h strict. Audit-only, no code commit.

**Inputs:**
- `data/baseline/pathological_2026-04-24_pre-H5A/forecasts_2026-04-24.jsonl`
  — aggregated reference (pre-H5.A, unclamped magnitudes).
- I6.I1 tape (post-restart) — per-tribe curves on a fresh run.

**Structure (mirrors H5 Phase 0):**

1. **Mathematical expectation.** Solve the AR(1) recursion analytically
   for each DNA category's dominant coefficient. If `dna_delta ≈
   k · prev_state_bps + noise`, then `cum_bps[h+1] ≈ (0.995 + k · scale) ·
   cum_bps[h] + event_bps · scale`. Stable iff `0.995 + k · scale < 1`,
   i.e. `k < −0.005 / scale`. With `scale ≤ 20` (depth cap), stability
   requires `k < −2.5 × 10^−4`. Very tight.
2. **Per-tribe feedback coefficient estimation.** Regress
   `curve[h+1] − 0.995 · curve[h]` against `curve[h]` on the I6.I1
   tape. Per-tribe, per-DNA-category. Identify outliers.
3. **Instability triggers.** For tribes with `k > 0`, measure what
   market condition precedes the explosive trajectories — cross-ref
   timestamps with orderbook depth, event markers, HTF regime.
4. **Simulation check.** Replay the exact `DNAContext` snapshots
   through each DNA function in isolation (no tribe interactions),
   confirm the feedback coefficient matches the regressed estimate.

**Exit conditions:**
- **Bug-found** on ≥ 1 DNA category → scope H6.A fix. Likely shape:
  bound the output of the offending DNA function to some max multiple
  of `prev_state_bps`, or replace a multiplicative term with additive.
- **Numerical bug** (e.g. a missing sign, overflow in intermediate
  calc) → H6.A is a 1-file fix, analogous to H5.A.
- **No DNA category exceeds threshold** → escalate to H7 (deeper
  council-interaction dynamics). Low expectation but must be
  entertained.

## 5. Scope boundaries

**In scope:**
- Phase 0 static audit of DNA functions.
- I6.I1 instrumentation (dependency chip).
- Characterization of the feedback coefficient distribution across
  DNA categories.

**Out of scope:**
- Changes to DNA functions (Phase 0 is audit-only; fix is H6.A+).
- Changes to `mean_reversion_rate`, `confidence_decay`,
  `depth_floor/ref` — those are parameter choices, not DNA mechanics.
- Changes to the H5.A clamp itself — that clamp is working as
  designed (observational containment). Only touch if H6 reveals a
  case where the clamp is also suppressing legitimate signal.
- Calibrator (kernel or tribe) — confirmed WAI in H5 Phase 0 scope
  re-read.

## 6. Parallelism & sequencing

- **H1 Phase 1:** data-gated, independent, parallel-safe.
- **H3 Phase 1:** data-gated post-H5.A restart. Partially blocked —
  the post-H5.A tape is the *new* H3 input. H6 I6.I1 can piggy-back on
  the same restart.
- **H5 Phase 1:** opens after 24h of post-H5.A tape; checks clamp
  trigger frequency and whether it's converging or diverging.
- **H6 Phase 0:** as per §3, blocked on I6.I1. Earliest start depends
  on how long it takes to collect a pathological-looking hour post-
  restart. Pathology may reproduce within minutes (H5.A doesn't
  suppress per-tribe `curve`, only the output), but if not, may need
  to run fresh until the DNA recursion re-engages.

## 7. Open questions for check-in before Phase 0 starts

1. **Sampling rate for I6.I1.** Per-tick is ~30 KB × 86400 s = 2.5 GB/day,
   too heavy. Proposed: 1 sample per 10 s (~250 MB/day). Open to
   halving/doubling based on storage budget.
2. **Isolation test setup.** Running DNA functions in isolation
   requires a `DNAContext` fixture. Acceptable to build one in
   `tests/dna/test_dna_isolation.py`, or prefer a one-off script under
   `scripts/` not committed to tests?
3. **Phase 0 pre-read.** Should the 2h Phase 0 box start with a
   formal pre-read of `src/dna/*.py` (possibly 30-45 min of the box)
   or is it fair to enter the box with the file set already read
   through (extracted from recent git blames)?

Check-in required before Phase 0 starts per
`feedback_audit_fix_checkin.md`.

## 8. Box accounting

- Scope doc: 20 min (box set by user @ commit time, post-H5.A).
- Used: ~20 min.
- Phase 0 estimate: 2h when data is ready.
- I6.I1 implementation estimate: 1-2h.
- **Total from H6 open to potential H6.A commit (best case): 5-6h
  across 2-3 distinct boxes.**
