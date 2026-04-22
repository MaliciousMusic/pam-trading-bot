# WS3 H2 — Option 1a attribution snapshot

**Commit:** `564c050` (`feat(projector): calibrator correct-direction fit target (H2 Option 1a)`)
**Date:** 2026-04-22
**Reproduce:** `PYTHONPATH=. python -m scripts.ws3_h2_option1a_snapshot --days 2026-04-18 2026-04-19 2026-04-20 2026-04-21`
**Raw output:** `research/phase11_diagnostic/_ws3_h2_snapshots/option1a_20260422T160057Z.md`

## 0. TL;DR

- **Pre-fix is a clamp artifact, confirmed.** Signed-median correction
  snaps to 0.250 on every day (the `max(0.25, …)` floor), gain collapses
  to `MIN_GAIN=1.0` within ~3,500 samples. The on-disk
  `last_correction=0.25` is not a measurement — it is the clamp.
- **Post-fix recovers a real signal.** Correct-direction median ranges
  0.69 – 2.47 across days, with p25/p75 spreads that are genuinely
  data-dependent (e.g. 04-18 → 0.29 / 0.69 / 1.43; 04-20 → 0.68 /
  2.47 / 5.70).
- **The correction's sign flipped.** Pre-fix said shrink; post-fix says
  grow on 3 of 4 days. The council's magnitude predictions were
  systematically **too small**, and pre-fix was reinforcing that by
  driving gain to the floor.
- **Cross-check holds.** Dropped fraction (46 – 55 %) matches the
  ForecastAuditor's independent directional-hit rate (44 – 51 %)
  within a few points. No dataflow divergence between the two
  measurements.
- **One unrelated anomaly surfaced.** 2026-04-21 has 73 anchors with
  `predicted_price ≈ 10^300` plus 19 null-predicted anchors; all other
  days are clean. Spawned as a separate task ("Investigate 2026-04-21
  projector divergence") — Option 1a's robust median handles the
  contamination gracefully, so the calibrator path stays unblocked.

**H2 intervention verdict:** Option 1a produces a measurable,
signed-correct magnitude signal where signed-median produced only
clamp artifacts. **Direction of bias is the opposite of what the
pre-audit framing assumed** — see §6 Plan Update. R2 sequencing
re-examined in §7.

**Reviewer addendum (2026-04-22):** The determinism PASS on this
commit is a false-positive because `SprintEngine` does not wire the
calibrator (`harness.py:66`). Live-feedback attribution and a wired
determinism check are prerequisites before R2 lands — see §7 ordering.

## 1. Per-day end-state (3-day panel + all-continuous)

Offline replay over `data/forecasts/<day>.jsonl`. Parameters match
production: `window=2000`, `min_samples=50`, `halflife_resolved=300`,
`MIN_PRED_BPS=0.5`, correction clamp `[0.25, 4.0]`, gain clamp
`[1.0, 40.0]`. Initial gain 8.0 (matches `settings.projector.bps_per_unit_pressure`).

| day | n_used | dir-hit% | dropped% | signed corr | correct-dir corr (p25/p50/p75) | gain(signed) | gain(opt1a) |
|:--|--:|--:|--:|--:|:--|--:|--:|
| 2026-04-18 | 13,502 | 51.1 % | 46.6 % | 0.250 | 0.288 / 0.692 / 1.430 | 1.00 | 15.24 |
| 2026-04-19 |  1,582 | 43.9 % | 55.4 % | 0.250 | 0.559 / 2.036 / 5.151 | 1.22 | 37.03 |
| 2026-04-20 |  4,185 | 50.1 % | 48.0 % | 0.250 | 0.683 / 2.470 / 5.703 | 1.00 | 40.00 |
| 2026-04-21 |  1,098 | 48.5 % | 49.6 % | 0.250 | 0.178 / 1.466 / 3.347 | 1.67 | 37.07 |
| **all (continuous)** | 20,367 | 50.2 % | 48.6 % | 0.250 | 0.208 / 1.521 / 3.826 | 1.00 | 40.00 |

Reading the table:

- `n_used` counts anchors surviving `|expected| ≥ MIN_PRED_BPS`. Of
  61,290 total anchors across the panel, only 20,367 (33 %) pass the
  filter. The rest are too small to carry scale information, so they
  never reach the calibrator. This is unchanged by Option 1a; both
  paths see the same filtered stream.
- `signed corr` is the pre-fix signed median(realized/predicted) at
  the last window, post-clamp. It is 0.250 on every day — i.e. every
  day ends with the raw median below 0.25, clamped up to the floor.
- `correct-dir corr p50` is the post-fix fit target. On 3 of 4 days
  it exceeds 1.0 (meaning realized magnitude > predicted magnitude
  inside the correct-direction subset). Day 04-18 is the outlier at
  0.69 — realized smaller than predicted on that tape.
- `gain(signed)` ≤ 1.67 everywhere — gain is stuck at or near the
  `MIN_GAIN` floor. `gain(opt1a)` hits `MAX_GAIN` or nearby on 3 of 4
  days, reflecting "the correction keeps saying grow", but the
  offline replay has no feedback (see §3).

## 2. Cross-check: directional-hit rate vs dropped fraction

`last_dropped_fraction` is the wrong-direction share of non-zero pairs
in the trailing 2000-sample window at the last refit.
`directional_hit` is the per-anchor boolean from `ForecastAuditor`
over the full day. Two independent code paths; they should agree on
the same underlying population up to `MIN_PRED_BPS` filtering and
window truncation.

| day | dir-miss% (100 − dir-hit%) | dropped% (opt1a) | delta |
|:--|--:|--:|--:|
| 2026-04-18 | 48.9 % | 46.6 % | +2.3 |
| 2026-04-19 | 56.1 % | 55.4 % | +0.7 |
| 2026-04-20 | 49.9 % | 48.0 % | +1.9 |
| 2026-04-21 | 51.5 % | 49.6 % | +1.9 |

Deltas ≤ 2.3 % across four tapes — consistent with the `MIN_PRED_BPS`
filter that drops small-magnitude anchors from the Option 1a
computation but not from the auditor's overall hit rate.

**Cross-check passes.** The `last_dropped_fraction` diagnostic is
trustworthy as a live proxy for the auditor's directional-hit rate.

## 3. Offline-replay caveat — why gain(opt1a) hits the ceiling

The attribution streams already-resolved forecasts through the
simulated calibrator with no feedback loop. In production:

1. Calibrator refits → gain rises.
2. Next tick's projector emits larger `expected_move_bps` (linear in
   gain).
3. Subsequent resolved anchors have larger `predicted`.
4. Next correction trends toward 1.0 as predictions catch up to
   realizations.

In offline replay step 3 does not happen — every window sees the
same frozen predictions emitted under the *old* gain. So if the true
magnitude bias is (say) 2×, the offline simulation shows correction
stuck at ~2.0 indefinitely, and gain ratchets up multiplicatively
until the MAX_GAIN clamp binds.

**Implication:** the `gain(opt1a) = 40.00` column is not a forecast
of where production would settle. The production convergence target
is better estimated as `initial_gain × correct-dir corr p50` applied
once, then letting feedback do the rest. Rough landing zones:

| day | initial_gain × p50 | feedback-adjusted estimate |
|:--|--:|:--|
| 2026-04-18 | 8.0 × 0.69 = 5.5 | stable near 5–6 |
| 2026-04-19 | 8.0 × 2.04 = 16.3 | stable near 13–17 (regime-dependent) |
| 2026-04-20 | 8.0 × 2.47 = 19.8 | stable near 15–20 |
| 2026-04-21 | 8.0 × 1.47 = 11.8 | stable near 10–13 (04-21 contaminated, see §5) |

**These are order-of-magnitude estimates, not predictions.** The
binding measurement is the correction distribution, not the point
estimate of where gain lands.

## 4. What this changes about H2

The path audit (`WS3_H2_path_audit.md`) identified two bugs in the
calibration path:

1. Signed-median clamp artifact — **fixed** by this commit.
2. `conviction_boost` unidirectional inflator — still present in
   `src/projector/chess.py:769-770`. Queued for Step 3 (R2).

Option 1a's production expectation, after conviction_boost relocation
lands:

- Calibrator fits real magnitude bias, ratcheted through feedback to
  a gain that makes `expected_move_bps ≈ realized_move_bps` in
  magnitude on the correct-direction subset.
- `DecisionEngine` sees a correctly-scaled emove instead of a
  1.5×-inflated one shrunk by the gain floor back to ~1×.
- **H2's original 52 % directional-hit acceptance bar (D3) is
  unchanged.** Option 1a fixes magnitude calibration, not direction.
  We expect a modest indirect lift if the correctly-scaled emove
  changes the entry-selection gating in DecisionEngine, but the
  first-order prediction is "no change in directional hit, large
  change in realized-bps magnitude per trade."

## 5. 2026-04-21 contamination (separate issue)

Count:

- 73 anchors with `|expected_move_bps|` in 10^170 – 10^301 range.
- 19 anchors with `predicted_price = None`.
- All on 2026-04-21 (label=`1m`, horizon_s=60). Days 04-18, 04-19,
  04-20 are clean.
- One example: idx 9194, `entry=76437.80`, `predicted=6.78e+301`,
  `expected_bps=8.86e+300`, `forecast_conf=0.837` (high-confidence
  garbage).

Max `forecast_conf` on 04-21 is 0.9048, versus 0.2483 / 0.3005 / 0.3457
on 04-18 / -19 / -20 respectively. Indicates a per-day projector
instability, not a calibrator issue.

Option 1a's robust median handles the contamination: the exploded
samples produce `r/p ≈ 0`, which the median absorbs without pulling
the point estimate far. The spawned task investigates the projector
path.

## 6. Plan Update — direction of H2 bias is reversed

**What this overturns.** The original H2 framing assumed the calibrator
was seeing inflated predictions and clumsily trying to shrink them —
`last_correction=0.25` read as "realized magnitude is only 25 % of
predicted." Post-fix data overturns that reading. The correct-direction
median is **above 1.0 on 3 of 4 days** (0.69 / 2.04 / 2.47 / 1.47);
the council's magnitude predictions are systematically **too small**,
and the signed-median code was reinforcing that shortage by floor-
clamping gain. Predictions aren't "too big to trust" — they're "too
small to act on," and the pre-fix calibrator was quietly pushing them
further in the wrong direction. This reversal is the operative finding
from H2 and should be read before H1/H3 scoping.

**Downstream implication for H1.** The original H1 framing was
"confidence formula compounds three sub-1 factors on top of inflated
emove, producing a cap near 0.25 regardless of underlying signal." With
the sign reversed — emove is deflated, not inflated — that mechanism
can't be the story. The 0.25 confidence cap must come from elsewhere:
formula conservatism (e.g. `signal_quality × directional` likely
lands in `[0.3, 0.5]` independent of magnitude), tribe correlation
collapsing apparent agreement (H3's territory), or both. H1 and H3
remain in scope; the assumption that their inputs are over-sized
should be dropped from their hypothesis statements before measurement.

**Implication for R2.** Relocating `conviction_boost` out of the
projector is still architecturally correct — it's the asymmetric
mixed-sign multiplier that pollutes calibrator fits — but its expected
lift on directional hit @ 20 s shrinks. First-order impact is now
"cleaner magnitude calibration," not "restore a suppressed signal."
Sequencing proposal in §7.

## 7. Sequencing — post-review plan

**Reviewer resolutions (2026-04-22).**

| # | Question | Decision |
|:--|:--|:--|
| Q1 | Wire calibrator into `SprintEngine` before R2? | **Yes.** The current determinism PASS is a false-positive; `harness.py:66` leaves the calibrator out of the sprint, so the refit path was never exercised. Wire it, re-run determinism, then re-run attribution with live feedback. If determinism breaks, it means the calibrator has a non-deterministic refit path and that is the blocker — fix before any WS3 H-measurement. |
| Q2 | Drop 04-21 from acceptance panel? | **No — reclassify.** Acceptance panel is the 3 clean days (04-18, 04-19, 04-20). 04-21 still runs in the H-measurement harness but is **reported separately as diagnostic only** with a "numerical anomaly under investigation" flag, pending the spawned divergence-audit. Post-audit it either joins acceptance or is permanently excluded based on root cause. |
| Q3 | Narrow `[MIN_GAIN, MAX_GAIN]` clamp? | **No — add telemetry instead.** Current `[1.0, 40.0]` is informative (the floor is what surfaced the directional-contamination bug). Add a warning log when correction is clamped by either bound, including the pre-clamp value, for live visibility without behaviour change. Reconsider narrowing only after several days of live post-fix operation show a bounded distribution. |

**Accepted order of operations.**

1. **[this commit]** `docs(research): WS3 H2 Option 1a snapshot — plan
   update + sequencing` — §6 Plan Update + §7 Sequencing amendment.
2. **[reviewer — local]** Start the spawned "Investigate 2026-04-21
   projector divergence" chip. Output goes to
   `research/phase11_diagnostic/proj_divergence_20260421_audit.md`.
3. **[next commit]** `chore(projector): log when calibrator correction
   hits a clamp` — small telemetry-only change in
   `src/projector/kernel_calibrator.py`, no behaviour modification.
   Logs both the `correction` clamp `[0.25, 4.0]` and the gain clamp
   `[MIN_GAIN, MAX_GAIN]`. Carries Q3 forward.
4. **[next commit]** `feat(replay): wire kernel calibrator in sprint
   harness + determinism verification` — SprintEngine instantiates
   the calibrator, pushes resolved anchors on each tick, reads
   `current_gain()` before projecting the next one. Determinism gate
   must PASS post-wiring under the same day+seed inputs. If it does
   not, that's a blocker and gets worked before step 5.
5. Re-run `scripts/ws3_h2_option1a_snapshot.py` against the
   live-feedback sprint output on all 4 days (acceptance panel
   reported as 3-day, 04-21 reported separately per Q2). Commit the
   new snapshot as `docs(research): WS3 H2 live-feedback attribution
   delta`. Report offline-vs-live delta.
6. **Pause.** Report live-vs-offline delta and confirm revised R2
   sequencing before starting the `conviction_boost` relocation.

**R2 sequencing — revised proposal.** After step 5 lands, R2 still
goes next in WS3 before H1/H3 measurement begins, but on
*measurement-hygiene* grounds rather than expected directional-hit
lift. Rationale:

- The unidirectional `[1.0, 1.5]` conviction_boost inflates predicted
  magnitude only. Under Option 1a the calibrator is already filtering
  out the worst artifact (wrong-direction samples), but the remaining
  correct-direction ratios are still depressed by up to 1.5× on any
  tick where conviction_boost binds. That makes every calibration
  reading harder to interpret and every downstream H-measurement
  less trustworthy than it needs to be.
- Expected quantitative effect: correct-direction median corrections
  rise by roughly 10–25 % after R2 (empirical conviction_boost
  distribution is concentrated in `[1.0, 1.3]`, not always at the
  1.5 ceiling). Gain-landing-zone estimates in §3 get a
  proportional uplift. First-order effect on directional hit: small.
- Alternative the reviewer may prefer: run R2 and H1's first
  measurement **in parallel** — they touch different modules
  (projector vs `DecisionEngine`/conf formula diagnostic), so
  commits don't collide; R2 hygiene and H1 measurement land in the
  same sprint. This would compress WS3 timeline but adds one extra
  review loop.
- **Decision deferred to the reviewer**, to be confirmed at the
  step-5 pause after live-feedback attribution data is in hand.

---

*Written after Option 1a commit `564c050`; offline determinism gate
PASSed (sha256=a9049de23892632e…) but acknowledged as false-positive
pending `SprintEngine` wiring. R2 (`conviction_boost` relocation) not
started and re-scoped per §6 Plan Update.*
