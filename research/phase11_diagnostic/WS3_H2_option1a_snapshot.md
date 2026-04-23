# WS3 H2 — Option 1a attribution snapshot

**Commit:** `564c050` (`feat(projector): calibrator correct-direction fit target (H2 Option 1a)`)
**Date:** 2026-04-22
**Reproduce:** `PYTHONPATH=. python -m scripts.ws3_h2_option1a_snapshot --days 2026-04-18 2026-04-19 2026-04-20 2026-04-21`
**Raw output:** `research/phase11_diagnostic/_ws3_h2_snapshots/option1a_20260422T160057Z.md`

> **Tape-source note (added 2026-04-23).** §§1-7 read the live-tape
> forecasts archive (`data/forecasts/<day>.jsonl`); no
> `MEASUREMENT_INFRA.md` caveat applies. §8 reads sprint-produced
> anchors from the Step 4 live-feedback driver and is explicitly
> caveated in §8.2. Going forward, WS3 H-measurement against live
> tape is the Phase 11 operating rule per `MEASUREMENT_INFRA.md`
> §2.

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

**Addendum #2 (2026-04-23) — Step 4 complete.** Calibrator now wired
into the sprint harness (feat `686164b`); full-day determinism on
2026-04-20 PASSes under live feedback (sha256=14d149feafe8ac7d…,
260 MB per run). Live-feedback attribution landed in `docs(research)`
commit `2a1bf26`; raw snapshot at
`research/phase11_diagnostic/_ws3_h2_snapshots/option1a_live-feedback_20260423T055545Z.md`.
Key delta: correct-direction p50 is *larger* under live feedback than
offline (1.69 / 3.90 / 3.27 / 4.28 vs 0.69 / 2.04 / 2.47 / 1.47),
and gain pins to the `MAX_GAIN=40.0` ceiling on every panel day.
New §8 covers the delta and the sprint-microstructure-vs-live
caveat. R2 sequencing refined in §7.

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

1. ✅ `docs(research): WS3 H2 Option 1a snapshot — plan update +
   sequencing` (`de9d1e6`) — §6 Plan Update + §7 Sequencing.
2. **[reviewer — local]** Spawned chip "Investigate 2026-04-21
   projector divergence." Output goes to
   `research/phase11_diagnostic/proj_divergence_20260421_audit.md`.
3. ✅ `chore(projector): log when calibrator correction hits a clamp`
   (`c81148f`) — telemetry-only, no behaviour change.
4. ✅ `feat(replay): wire kernel calibrator in sprint harness +
   determinism verification` (`686164b`) — SprintEngine accepts
   a `KernelCalibrator`, refreshes projector gain on each resolved
   tick. Full-day determinism PASS on 2026-04-20, sha256=
   14d149feafe8ac7d… (260,138,724 bytes per run; two runs
   byte-identical).
5. ✅ Re-ran attribution under live feedback via
   `scripts/ws3_h2_step4_live_attribution.py`; snapshot +
   per-day gain JSONs + fixtures landed in `docs(research)`
   commit `2a1bf26`. Delta table and caveat in §8.
6. **← current pause.** Report live-vs-offline delta and confirm
   revised R2 sequencing before starting the `conviction_boost`
   relocation.

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
  step-5 pause after live-feedback attribution data is in hand —
  see §8.

## 8. Live-feedback attribution (Step 4 completion)

SprintEngine now constructs a `KernelCalibrator` per run (at
`<run_dir>/calibration/projector_gain.json`, mirroring the auditor's
per-run forecasts dir) and feeds it every resolved anchor. The
projector's `bps_per_unit_pressure` is refreshed before each
subsequent tick. Full-day determinism on 2026-04-20 PASSes byte-
identically under this loop (sha256=14d149feafe8ac7d…, 260 MB per
run × 2). The wiring unblocks a proper offline-vs-live comparison
of Option 1a's fit target.

### 8.1 Per-day delta (offline → live-feedback)

Offline column re-stated from §1 (raw file `option1a_20260422T160057Z.md`).
Live column from `option1a_live-feedback_20260423T055545Z.md`, produced
by sprint runs against committed `BTCUSDT_<day>` fixtures.

| day | corr p50 offline | corr p50 live | Δ p50 | gain(opt1a) offline → live |
|:--|--:|--:|--:|:--|
| 2026-04-18 | 0.692 | **1.687** | +0.99 | 15.24 → 40.00 (ceiling) |
| 2026-04-19 | 2.036 | **3.898** | +1.86 | 37.03 → 40.00 (ceiling) |
| 2026-04-20 | 2.470 | **3.268** | +0.80 | 40.00 → 40.00 (pinned) |
| 2026-04-21 | 1.466 | **4.275** | +2.81 | 37.07 → 40.00 (ceiling) |
| **panel (3d accept)** | 1.7 med | **3.0 med** | +1.3 | ceiling every day |

Direction of move: corrections all grew under live feedback. Gain
pinned at `MAX_GAIN=40.0` on all 4 days, with end-of-day raw
corrections still clamped hi at 4.0× (per-day `projector_gain.json`
dump). In plain reading: **the live loop pushed gain to the ceiling
and the calibrator is still asking for 4× more.**

### 8.2 Sprint tape vs live tape — the dominant caveat

The offline snapshot read from `data/forecasts/<day>.jsonl`, written
by the **live TradingEngine** with its persisted gain (1.57 on
2026-04-21). The live-feedback snapshot read from **sprint-produced
anchors**, where the calibrator walked gain from 8.0 up to the 40.0
ceiling in the first ~2,000 resolved ticks and stayed there. Raw-data
deltas reveal the tapes are not the same population:

| metric | offline (live tape) | live-feedback (sprint tape) | ratio |
|:--|--:|--:|--:|
| `|expected_move_bps|` median | 0.143 | **0.009** | 16× smaller |
| `|expected_move_bps|` p90 | 3.540 | **0.335** | 10.6× smaller |
| `|realized_move_bps|` median | 1.704 | **0.392** | 4.4× smaller |
| `|realized_move_bps|` p90 | 8.446 | **6.478** | 1.3× smaller |
| anchors total | 61,290 | 686,263 | 11× more |
| anchors kept (≥ MIN_PRED_BPS) | 20,367 (33 %) | 34,888 (5 %) | |
| label mix | 15s / 1m / 5m / 10m | 15s / 1m only | sprint projector horizon limit |

The sprint's synthetic order book + synth trade stream produces
pressure signals materially weaker than the Binance BTCUSDT tape —
net_pressure back-solved from emove/gain is ~400× smaller per tick
in sprint at gain=40 than live at gain=1.57. The calibrator's
response is textbook: push gain as high as possible to close the
prediction-vs-realized magnitude gap; when the ceiling binds, the
residual surfaces as persistent 4× raw corrections.

### 8.3 Three clean readings that fall out of 8.1 + 8.2

1. **Direction of bias is the same on both tapes and both reviews.**
   Corrections > 1.0 on 3 of 4 days offline, all 4 days live.
   §6 Plan Update's "predictions are systematically too small"
   finding is confirmed on the bigger, live-feedback-converged
   dataset.
2. **The 40.0 `MAX_GAIN` ceiling is not an acceptance gate on sprint
   tapes.** Every panel day saturates it. Whether it binds on live
   production depends on whether the production pressure signal is
   closer to live-tape magnitudes (0.091 median) or sprint-tape
   magnitudes (0.0002). The persisted production gain of 1.57 implies
   live data is closer to the offline-tape dynamics, consistent with
   the 1.57 ≫ MIN_GAIN gap. A sprint-only H-measurement would
   miscalibrate the ceiling question; H2 acceptance must be measured
   on either live-tape replays (the existing offline path) or on a
   higher-fidelity sprint synth-book.
3. **Absolute magnitude of the deflation is tape-dependent but the
   sign isn't.** Live-tape says predictions are 0.5×–2.5× too small;
   sprint-tape says they are 2×–4× too small. Either way, the correct
   intervention is "let gain grow" and the pre-fix signed-median
   clamp at 0.25 was actively moving the projector in the wrong
   direction.

### 8.4 Implications for R2

The §7 revised R2 sequencing held that `conviction_boost` relocation
is measurement-hygiene, not directional-lift. The live-feedback
numbers sharpen that reading in two ways:

- **Measurement hygiene is more urgent, not less.** The asymmetric
  `[1.0, 1.5]` boost adds up-to-1.5× inflation to predictions, which
  in live-tape terms shifts a true correction of 2.0 down to a
  reported 1.33. With the calibrator now surfacing "real" corrections
  in the 3-4× range on sprint tapes, a 1.5× inflation artifact on top
  is genuinely noisy. R2 is a prerequisite for trusting any H1/H3
  measurement that depends on the magnitude channel.
- **Quantitative lift on directional hit remains small.** R2 does
  not move the direction-hit tape; it moves the magnitude one. The
  §7 estimate of "correct-direction median corrections rise by
  roughly 10–25 % after R2" stands — refined, given the sprint-tape
  correction distribution sits 2-3× above the live-tape one, that
  translates to ~0.3–1.0 absolute units on the p50 depending on day.

**Revised R2 recommendation (decision point for the reviewer).** R2
(conviction_boost relocation) stays next in WS3, but the commit
message should anchor on "measurement hygiene unblocks H1/H3
magnitude measurement" rather than any standalone directional-hit
target. D3 (directional-hit @ 20s, 52 % panel average) should be
expected to shift by **less than 1 percentage point** as a direct
consequence of R2, and the WS3 acceptance gate language for R2
should say so explicitly to avoid reading R2 as a standalone fix.

**Open question back to the reviewer (not pre-decided):** Does the
sprint-vs-live microstructure delta in 8.2 need its own hypothesis
slot (call it H4: "sprint synth book under-represents live pressure
by O(100×); sprint-based H-measurement under-detects magnitude
mechanisms")? Or is the correct answer to simply continue H2/H1/H3
measurement against the live-tape forecasts archive and treat the
sprint harness as a determinism/regression tool only? A H4 slot
would add measurement surface but also adds a dependency on
synth-book replacement work that may not be in Phase 11's scope. The
determinism-gate-only interpretation is cheaper and bounds the
scope of WS3 cleanly.

---

*Written after Option 1a commit `564c050`; offline determinism gate
PASSed (sha256=a9049de23892632e…) but acknowledged as false-positive
pending `SprintEngine` wiring. Wiring landed `686164b`; determinism
re-PASSes (sha256=14d149feafe8ac7d…). Live-feedback attribution in
`2a1bf26`. R2 (`conviction_boost` relocation) not started; sequencing
confirmed in §7/§8 and awaiting reviewer sign-off at the step-6
pause.*
