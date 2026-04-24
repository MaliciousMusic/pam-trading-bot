# WS3b — Warmup / persistence / anchor-horizon audit

Read-only structural audit per WS3_H1_SCOPE.md §11 parallel-workstream
policy. No code change, no perturbation of the confidence formula,
the auditor schema, or the regime-labeller.

## 0. Scope reframe

PLAN.md:183 is the only reference to "Weakness #4 from the original
WS3 scope": **"persistence-weighted anchor horizons, warmup
behaviour"**. The current committed `WS3_SCOPE.md` has no enumerated
weakness list — it enumerates **hypotheses** H1-H4, not weaknesses. The
numbering "#4" appears to be a residue of an earlier scope draft
(pre-commit `9cdd1dd`) that was re-structured into the hypothesis
layout we now have.

Rather than archaeology this audit takes PLAN's parenthetical as
authoritative and audits the two surfaces it names:
1. **Persistence-weighted anchor horizons** — whether anchor creation
   or scoring is weighted by persistence (regime duration, forecast
   stability, calibrator maturity).
2. **Warmup behaviour** — how calibrators and auditors behave when
   their sample counts are below their `min_samples` thresholds.

## 1. Persistence-weighted anchor horizons

### 1.1 Current semantics — fixed per-tick spec

Anchor horizons are a **static list** established once at engine
construction and applied unconditionally every tick:

- [engine.py:98-110](src/core/engine.py) (live):
  ```
  raw_anchor_specs = [
      ("15s",  1),
      ("1m",   max(1, 60  // max(tick_seconds, 1))),
      ("5m",   max(1, 300 // max(tick_seconds, 1))),
      ("10m",  max(1, 600 // max(tick_seconds, 1))),
  ]
  ```
- [harness.py:131-140](src/replay/harness.py) (sprint): identical
  structure.

The only filter is a projector-reachability guard
(`idx <= proj_steps`). Every surviving `(label, step_idx)` emits one
`PendingAnchor` per tick — regardless of regime age, forecast
stability, calibrator readiness, or prior anchor hit-rate.

### 1.2 What "persistence-weighted" would mean

Two reasonable readings:

- **Creation-time weighting.** Skip or up-rank anchors when the
  regime has just flipped (e.g. suppress the 10m anchor for the first
  N ticks after a `chop→trend` transition, since the anchor will
  straddle a label change and mis-score the projector). WS4 already
  documented that `chop↔normal` accounts for ~43% of all transitions
  (1,268 total in the sprint day) with median dwell = 5 ticks — so
  the 10m anchor at step_idx=40 routinely spans multiple regime
  labels.
- **Resolution-time weighting.** Discount resolved anchors whose
  `target_ts` crosses a regime boundary, or weight kernel-calibrator
  refits by the persistence-coherence of the pairs in the window.

### 1.3 Structural finding — neither exists

Grep of `src/projector/chess.py` for `persistence|persist_|
consecutive|lifetime|dwell` returns **zero matches**. The projector
and forecast auditor do not track regime-persistence, and neither the
`PendingAnchor` dataclass ([backscore.py:32-51](src/audit/backscore.py:32))
nor the `Resolved` dataclass ([backscore.py:55-77](src/audit/backscore.py:55))
carries a regime-label field at creation or resolution time. The
calibrator's refit loop ([kernel_calibrator.py:140-230](src/projector/kernel_calibrator.py:140))
likewise treats every pair in the 2,000-element window as having
equal evidentiary weight.

This is a **capability gap**, not a broken implementation — there is
nothing to fix, because there is nothing there. WS3b's surface area
for this weakness is therefore *instrumentation-first* (stamp the
regime label on the anchor at creation and resolution), not
intervention-first.

### 1.4 H1.I1 leverage

H1.I1 already persists five decomposition fields on
`PendingAnchor`/`Resolved`. Adding a `regime_label_at_creation` /
`regime_label_at_resolution` pair would be a one-commit extension of
the same schema pattern, would survive the auditor-schema protection
clause in WS3_H1_SCOPE.md §11 (additive), and would light up the
creation-time and resolution-time weighting questions against the
same 3-day tape panel H1 itself runs on.

## 2. Warmup behaviour

### 2.1 KernelCalibrator — hard floor at `min_samples=50`

From [kernel_calibrator.py:87-100](src/projector/kernel_calibrator.py:87)
and [:136-138](src/projector/kernel_calibrator.py:136):

- `min_samples=50` (default; current usage inherits the default).
- Refit only fires once `self._n_seen >= self._min_samples`.
- Until then the projector's `bps_per_unit_pressure` holds at
  `initial_gain` verbatim.

With the four-anchor spec emitting up to 4 resolved anchors per tick
but **not all anchors mature on the same schedule** (15s matures in
1 tick at `tick_seconds=15`; 10m matures in 40 ticks), the calibrator
sees roughly `1 + 4 + 20 + 40 = 65` anchors over the first 40 ticks
— so warmup crosses roughly around tick 35-40 ≈ 9-10 min of wall
clock in the live engine, and nearly the same in sprint.

### 2.2 KernelCalibrator — post-warmup clamp saturation

The in-flight full-day determinism gate
(`_ws3_h1_I1_gate/full_day.log`, PID 1462, ~3h elapsed) shows the
calibrator's *very first* post-warmup refit immediately saturates at
the hi clamp (`bounds [0.25, 4.00]`) and **stays there** for the
entire log — raw correction values bounce in `[4.00, 4.09]` without
the smoothed gain ever leaving the ceiling:

```
WARNING [kernel-cal] correction clamped hi (raw=4.055 → 4.000; bounds [0.25, 4.00])
WARNING [kernel-cal] correction clamped hi (raw=4.089 → 4.000; bounds [0.25, 4.00])
...
```

This is the MEASUREMENT_INFRA.md §2 "400× weaker pressure" signature
in real time. The warmup behaviour compounds the §2 issue: the first
usable signal the calibrator *ever* receives is already far enough
out of equilibrium that the correction saturates, and sprint-tape
calibrator data cannot inform an intervention decision.

### 2.3 TribeCalibrator — four-layer warmup cascade

From [audit/calibrator.py:97-111, 399-435](src/audit/calibrator.py:97):

| layer                         | `min_samples` | fallback on                     |
|:------------------------------|:-------------:|:--------------------------------|
| per-horizon-bucket EWMA       | `horizon=20`  | per-regime EWMA                 |
| per-regime EWMA               | `regime=15`   | fast override if enabled        |
| fast EWMA override            | `fast=10`     | slow EWMA                       |
| slow EWMA (global)            | `slow=30`     | multiplier = 1.0 (no info)      |

**Warmup pathology:** the `(tribe × regime × horizon_bucket)` joint
cube is the richest estimator but requires ≥20 resolutions **per
cell**. With 3 tribes × 5 regimes × 3 horizon buckets = 45 cells, and
roughly 4 resolutions per tick, a sprint would need ~225 ticks per
cell uniformly distributed — but WS4's dwell histogram (54% of runs
≤5 ticks, cascade share ~1% in sprint) guarantees most cells never
warm up at all. The per-regime EWMA (`regime_min_samples=15`) is the
only realistic post-warmup layer for the short regimes.

### 2.4 Sprint = perpetual cold-start + calibrator absent

[harness.py:459-464](src/replay/harness.py:459):
```
calibration_dir = run_dir / "calibration"
calibration_dir.mkdir(parents=True, exist_ok=True)
projector_cal = KernelCalibrator(
    initial_gain=float(cfg["projector"]["bps_per_unit_pressure"]),
    directory=calibration_dir, ...)
```

Each sprint run creates a **per-run** calibration directory, so the
kernel calibrator always starts with `n_seen=0` and `gain=initial_gain`.
This is by design (determinism isolation) but has two side-effects
WS3b should flag:

1. Sprint never observes the **post-warmup steady state** because
   the first 50 anchors are paid every run.
2. Sprint harness, per [harness.py:78-79](src/replay/harness.py:78),
   does **not instantiate a TribeCalibrator at all** — WS5 already
   documented this for the harvest-mass path; the same absence kills
   all four warmup layers in §2.3 on sprint.

The verify_determinism.py script makes no special arrangements for
calibration state (grep of `scratch|directory=|KernelCalibrator|
projector_gain` returns no hits in the script itself) — isolation is
inherited entirely from the harness's per-run `run_dir` layout.

### 2.5 Calibrator persistence — structural summary

| component         | on-disk state        | loaded at init?      | sprint behaviour                    |
|:------------------|:---------------------|:---------------------|:------------------------------------|
| KernelCalibrator  | `projector_gain.json`| yes if file present  | always cold (per-run `calibration/`)|
| TribeCalibrator   | `tribe_skill.json`   | yes if file present  | never instantiated in sprint        |
| ForecastAuditor   | `forecasts/<day>.jsonl` (append-only) | in-memory ring only | writes to per-run `forecasts/`      |

Live production loads kernel + tribe state across restarts (so
production warmup is paid once, during the cold boot of a process
generation). Sprint pays warmup on every run. This is the asymmetry
that MEASUREMENT_INFRA.md §2 revisit-trigger #2 already flagged as a
"sprint pressure weakness" — WS3b confirms the mechanism.

## 3. Blast radius

| surface                          | warmup impact                                  | persistence-horizon impact |
|:---------------------------------|:-----------------------------------------------|:---------------------------|
| Projector `bps_per_unit_pressure`| 9-10 min idle, then clamp-saturated (sprint)   | — (no regime label carried) |
| TribeCalibrator multipliers      | 30+ anchors per tribe before any multiplier ≠1 | — (horizon bucket blind to persistence) |
| Conviction-boost (harvest path)  | dormant in sprint (WS5) — warmup moot          | — |
| WS2.5 regime-entry gate          | unaffected (stateless, per WS4)                | — |
| ForecastAuditor schema           | no warmup; every anchor scored immediately     | missing regime-label field |
| Path-Integrity clamp             | unaffected (uses directional_score, not calibrator gain) | — |

**Bidirectional story.** Warmup in the kernel calibrator under-corrects
the projector for the first ~10 min (below the clamp saturation
regime). Warmup in the tribe calibrator under-weights skilled tribes
and over-weights noisy ones until each layer's threshold is crossed.
Persistence-horizon blindness affects both the *scoring* of anchors
that span regime changes and the *selection* of anchors worth
emitting in transition-dense windows.

## 4. Verdict + recommendations

**Not an H1 blocker.** Warmup and persistence-weighting gaps do not
perturb H1's confidence-formula-decomposition measurement — H1.I1
stamps the decomposition regardless of upstream calibrator state.
WS3b findings hold as post-H1/H3 follow-ups per PLAN Q6.

**Post-H1/H3 recommendations (priority order):**

1. **Add regime-label stamps to `PendingAnchor`/`Resolved`.** One
   commit, additive, enables persistence-span and
   regime-transition-weighted analysis against the same 3-day panel
   H1 uses. Schema-protection-compatible per WS3_H1_SCOPE.md §11
   (additive only).
2. **Adopt kernel-calibrator persistence for the live-tape
   measurement infrastructure.** MEASUREMENT_INFRA.md §2/§4.1
   already points this way; WS3b confirms the sprint cold-start is a
   second-order contributor to the clamp saturation observed on
   sprint tape.
3. **Defer TribeCalibrator warmup-cascade changes** until after the
   harvest-path live-tape measurement (WS5 §5). All four warmup
   layers are moot in sprint because the calibrator is never
   instantiated; any threshold change measured only against sprint
   is a measurement artefact.
4. **Do not introduce persistence-creation gating** (skip-anchor on
   transition) until (1) lands and produces a regime-transition
   histogram per anchor label. Intervention without measurement
   would churn the anchor stream against a change PLAN §5 has not
   yet scoped.

**Out of scope for WS3b (record only):**

- The "anchor horizon mismatch" framing in the WS3_SCOPE pre-commit
  draft (9cdd1dd lines 232-240) about `steps_bps[step_idx - 1]` —
  that is an H1 antecedent, not a WS3b weakness, and H1.0 already
  audited it as CLEAN (commit `827040e`).
- Retrofitting the `KernelCalibrator` sample window from pairs to
  triples `(pred, realized, regime_label)` — upstream of (1) and
  premature without measurement.

---
*Audit performed read-only while the full-day determinism gate
(PID 1462, `_ws3_h1_I1_gate/full_day.log`) was in flight.
No protected surfaces touched.*
