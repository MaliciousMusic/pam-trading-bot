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
clamp artifacts. Proceed to conviction_boost relocation (R2) as
planned. Do not adjust `MIN_GAIN` in this commit.

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

## 6. Next step

Per R3 (attribution discipline) and the reviewer's order-of-operations
"Step 3. Pause. Report findings before Step 3", this report completes
Step 2. **Pausing for review before beginning the `conviction_boost`
relocation.**

Open questions / checkpoints for the reviewer:

- **Q1:** Is the correction distribution enough to unblock R2, or do
  you want the calibrator wired into `SprintEngine` first so the
  attribution can run through a full replay-loop (the true feedback
  measurement) before moving on? `harness.py:66` has the `# No
  auditor / calibrator / kernel_calibrator in phase 2` TODO; wiring
  it is a small, self-contained change.
- **Q2:** The 04-21 projector divergence is flagged as a separate
  task. Do you want to drop 04-21 from the WS3 acceptance-bar panel
  until it's audited, or keep all 4 days?
- **Q3:** Option 1a's expected gain landing zone (§3 table) lands
  mostly in [5, 20]. This is below the current MAX_GAIN=40 ceiling
  but well above the old MIN_GAIN=1.0 stuck position. Any appetite
  to narrow the gain clamp now (e.g. `[2.0, 25.0]`) as a belt-
  and-braces guard against the next clamp artifact, or leave the
  bounds alone until a real binding event is observed?

---

*Written after Option 1a commit `564c050`; determinism gate
PASS (sha256=a9049de23892632e…); conviction_boost relocation not
started.*
