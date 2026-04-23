# Projector numerical divergence — 2026-04-21 audit

**Created:** 2026-04-23  |  **Author:** investigation chip (spawned from WS3 H2 snapshot §5)
**Branch / HEAD:** `feat/tensor-engine @ c73c370` (pre-fix; investigation-only)
**Scope:** trace / propose. No production code has been changed in this worktree.

## TL;DR

- **Sprint does not reproduce.** 172,736 sprint anchors on 04-21 with the wired calibrator (commit `686164b`) produced zero `|emb| > 1e8`; max absolute `expected_move_bps` is 27.3 bps. Live produced 69 contaminated (`>1e8`) anchors with max `8.86e+300` and 19 `None` predicted prices in a ~14-minute window.
- **Root cause is a conjunction**, not a single faulty value:
  1. **Trigger**: live tape on 04-21 has a 2h 54m silence from 08:44:59→11:39:01 UTC. The first tick after the gap re-enters the projector with a moderate seed and non-trivial forecast confidence (0.245 — ~27× the equivalent post-gap confidence on 04-20).
  2. **Amplifier**: `retail_dna`'s panic branch combines `chase = 4·Δstate` (geometric) with `overshoot = 0.0004·state²` (quadratic). Across the 12-step 1-minute horizon this is super-exponential once `|state| > 120 bps`.
  3. **Counterweight absent**: `mean_reversion_rate = 0.005` (lowered from 0.02 to stop erasing consensus) is far too weak to damp a quadratic term.
  4. **Perpetuation**: contaminated `expected_move_bps` writes into `ForecastAuditor.resolved`, so `sigma_by_horizon`'s 3600s residual window balloons, which inflates shock magnitudes via `sigma_terminal` in the next projector calls, re-triggering retail panic — the 14-minute duration matches the residual window aging out.
- **Not a fixture bug.** `data/fixtures/BTCUSDT_2026-04-21/bars.json` is clean across the contamination window (max |bar move| ≤ 75 bps).
- **Not a gap-alone bug.** 04-19 (569-min gap) and 04-20 (180-min gap) both have tape gaps but zero contamination. What differentiates 04-21 is the *initial post-gap forecast confidence*.
- **04-21 should stay diagnostic-only** until a fix lands AND is re-replayed. The 14-minute explicit contamination plus sigma-feedback tail likely invalidates 20–40 min of downstream anchors beyond the 69 explicit 1m explosions.

## §1. Path of the arithmetic — tribe consensus → `expected_move_bps`

Traced through `src/projector/chess.py` (1086 LOC) and `src/projector/behavioral_dna.py`.

### 1.1 Per-tribe seed
For each of the 17 tribes, at step 0:

```
v0           = tribe.project_vote(context)                 # signed conviction ∈ [-1, 1]
sk0          = skill_multiplier (bounded [0.6, 1.4])       # from calibrator history
effective_gain = bps_per_unit_pressure · regime_multiplier # 8.0 · regime ≈ 8–12
base_strength_pre = v0.conviction · sk0 · effective_gain   # bps/step seed
```

### 1.2 Per-tribe DNA recursion (`chess.py:554–612`)
For steps `k = 1..projector_steps=250`:

```
ctx      = DNAContext(prev_state, prev_delta, shocks, base_strength, horizon_k)
dna_delta = dna_fn(ctx)                   # dispatched by tribe DNA class
step_bps = (dna_delta + event_bps_k) · depth_scale_k        # depth_scale bounded at 20×
step_bps -= mean_reversion_rate · cum_bps  # only when rate > 0
cum_bps  += step_bps
```

### 1.3 `retail_dna` panic branch (THE EXPLOSION SOURCE, `behavioral_dna.py`)

```python
def retail_dna(ctx):
    intent_drift = 0.15 * ctx.base_strength * ctx.signed_intent
    panic = bool(ctx.perceived) or abs(ctx.prev_state_bps) > 120.0
    if panic:
        chase     = 4.0 * ctx.prev_delta_bps            # 4× Δ per step
        overshoot = 0.04 * ctx.prev_state_bps * abs(ctx.prev_state_bps) / 100.0  # 0.0004·s²
        shock_panic = _perceived_sum(ctx) * 1.5
        return intent_drift + chase + overshoot + shock_panic
    return intent_drift + 0.40 * ctx.prev_delta_bps
```

Hand-traced dynamics once panic latches (seed `delta=5`, `state=5`):

| step | chase (4·Δ) | overshoot (0.0004·s²) | state after | delta after |
|------|------------:|----------------------:|------------:|------------:|
| 1    |        —    |                    — |           5 |           5 |
| 3    |        80   |                 0.25 |         105 |          80 |
| 5    |     1,296   |                73.6  |       1,799 |       1,370 |
| 7    |    27,096   |              29,394  |      65,063 |      56,490 |
| 9    |   7.6e6     |               1.6e9  |        1.6e9 |       1.6e9 |
| 12   |             |        ≈ 10¹⁰–10¹³   |             |             |

This matches the observed 15s (step 3) vs 1m (step 12) divergence: on 04-21 post-gap the 15s anchor was 0.14 bps but the 1m was `4.45e+10` bps — an amplification of ~3e11 in 9 recursion steps, consistent with the quadratic overshoot dominating once `|state|` breaches ~1000 bps.

### 1.4 Consensus and anchor projection (`chess.py:742–794`)

```
steps_bps[h] = Σ_i ( curve_i[h] · atw_i · sk_i · pool_w_i ) / Σ weights      # weighted mean
steps_bps    = [b · conviction_boost for b in steps_bps]                     # ≤ 1.5×
kernel[i]    = confidence_decay ** i
expected_move_bps(label) = Σ step_bps_list[0..step_idx-1] · kernel[0..idx-1]
```

So **all labels on a given tick share one projector call and the same `steps_bps` array**. The 15s label samples up to step 3; 1m samples up to step 12. That is why 15s stays small on 04-21 while 1m explodes: the quadratic feedback needs ~7–9 recursion steps to run away, and 15s doesn't reach that far in.

Confirmed by `src/core/engine.py:172–208` (`_compute_predictions`): a single projector forecast is fan-out to labels via `cumulative_by_step[step_idx - 1]`.

## §2. Reproduction status

**Sprint does NOT reproduce.**

```
sprint 04-21  n=172,736  contaminated(>1e8)=0  null=1  max|emb|=2.730e+01
live   04-21  n= 13,548  contaminated(>1e8)=69 null=19 max|emb|=8.863e+300
```

(sprint artefacts: `research/phase11_diagnostic/_ws3_h2_step4_forecasts/2026-04-21.jsonl`,
live artefacts: `data/forecasts/2026-04-21.jsonl`.)

### Why sprint misses it

- Sprint synth tape has no multi-hour gaps; every tick is adjacent.
- Sprint's synthetic book is ~400× weaker on aggregate pressure than the live book (WS3 H2 snapshot §8.2).
- Sprint's dense cadence prevents the initial base_strength seed that panic needs; the projector call runs with well-damped `prev_state`/`prev_delta` because prior ticks warmed up incrementally.
- The calibrator's Option 1a robust median absorbs residual outliers correctly in sprint (correction saturates at the 4.0 ceiling on 04-21 but does not produce explosion output — the ceiling protects it).

### What would reproduce it

A harness that (a) feeds a multi-hour tape silence into the live pipeline, (b) uses live-equivalent book pressure and shock schedule, and (c) keeps the ForecastAuditor + calibrator state warm across the gap. None of the current sprint harnesses do all three.

## §3. Root cause

**Live-path-specific numerical runaway in `retail_dna`, triggered by a tape-gap resume with non-trivial forecast confidence.**

### 3.1 Trigger event — the 2h 54m gap
```
2026-04-21: n_ticks=6821 gaps_gt_5min=1
  gap 174.0min  08:44:59 → 11:39:01 UTC
```
First contaminated anchor in `data/forecasts/2026-04-21.jsonl` is at `tick_ts=1776771541.40` — exactly the first tick after the gap. Contamination window: 830s, 61 unique tick_ts, 69 `>1e8` anchors + 19 `None` predictions.

Comparable gaps on other panel days did NOT explode:

| day        | largest gap     | contaminated anchors | max \|emb\|   |
|------------|-----------------|---------------------:|---------------|
| 2026-04-18 | none > 5 min    |                    0 | 1.31e+02 bps  |
| 2026-04-19 | 569.4 min       |                    0 | 3.49e+01 bps  |
| 2026-04-20 | 180.4 min       |                    0 | 8.27e+01 bps  |
| 2026-04-21 | 174.0 min       |                   69 | 8.86e+300 bps |

So the gap is **necessary but not sufficient**. The 04-21 differentiator:

| day        | first post-gap 15s emb | first post-gap 1m emb | forecast_conf |
|------------|------------------------:|----------------------:|--------------:|
| 2026-04-20 | 1.19e-04 bps            | -0.22 bps             | 0.009         |
| 2026-04-21 | 1.40e-01 bps            | **4.45e+10 bps**      | **0.245**     |

`forecast_conf` on 04-21 post-gap is ~27× higher than 04-20's. That confidence feeds into `base_strength_pre = v0.conviction · sk0 · effective_gain` and seeds `prev_state` large enough for retail's `|state| > 120` panic gate to arm within the first 2–3 recursion steps of the 12-step 1m horizon.

### 3.2 Amplifier — retail's super-exponential panic branch
Per §1.3 walkthrough. Two problems compound:

1. `chase = 4 · prev_delta` is geometric with ratio 4.
2. `overshoot = 0.0004 · state²` is quadratic, dominating once `|state| > 100 bps`.

Either alone is manageable; together they overwhelm the 0.005 mean-reversion.

### 3.3 Counterweight missing
`config/settings.yaml: projector.mean_reversion_rate = 0.005` — comment in the file explicitly notes it was lowered from 0.02 because the stronger value "was erasing consensus too hard." At 0.005/step a 10,000 bps state reverts by 50 bps/step, which is a rounding error next to a quadratic overshoot producing thousands of bps/step.

### 3.4 Perpetuation — auditor sigma feedback
`src/audit/backscore.py::sigma_by_horizon` (lines 266–290) computes sigma from residuals in a rolling 3600s window. Once any anchor resolves with a huge `expected_move_bps`, the residual `realized_move - expected_move` becomes astronomical; sigma balloons; `sigma_terminal`-derived shock magnitudes balloon; retail perceives shocks above its 15 bps `panic_threshold`; retail explodes again.

Observed ~14-minute contamination window is consistent with the 3600s window aging out the first contaminated samples (panic keeps re-arming on every new tick that still sees inflated sigma). This also explains the 19 `predicted_price=None` rows: the contaminated `price · (1 + bps/10000)` overflows to `inf`, and the auditor coerces non-finite to `None` downstream.

### 3.5 Not the suspects we ruled out
- **Fixture**: `data/fixtures/BTCUSDT_2026-04-21/bars.json` max |close−open| = 44.8 bps, max OHLC range = 75 bps across the full day. 11:35–11:42 UTC bars around contamination onset are `−2..+8 bps`. Clean.
- **`net_pressure` anomaly**: contaminated tick's `net_pressure` max = 0.037 vs clean-day max 0.062. Inputs not anomalous.
- **Calibrator**: `skill_multiplier` is bounded `[0.6, 1.4]`; `correction` `[0.25, 4.0]`; `gain` `[1.0, 40.0]` (`src/audit/calibrator.py:100-118, 385-437`). None can produce 10³⁰⁰. Option 1a correction reached 4.0 on sprint 04-21 without explosion — the ceiling held.
- **`conviction_boost`**: bounded `[1.0, 1.5]`. Not the source.
- **Depth scale**: bounded at 20×. Not the source.

## §4. Proposed fix — each item is a SEPARATE commit

Attribution discipline: these are ordered by (defense-in-depth value, reversibility). Land them one commit per item and re-replay 04-21 after each to attribute the effect.

### Fix A (recommended first landing) — per-step `step_bps` clamp in the projector loop
`src/projector/chess.py:~580` — cap `step_bps` to ±`max_step_bps` before it enters `cum_bps`.

Suggested constant: `max_step_bps = 400.0` (5× the largest legitimate single-step observed across 04-18/04-19/04-20, which is 82 bps). The clamp is loud (emit a counter/log when hit) so downstream sees the early warning rather than silent truncation.

Pros: touches exactly one line in the hot path; protects against *any* DNA runaway, not just retail; makes the system numerically safe by construction. Cons: hides the bug if you clamp silently — **must** bump a counter and log the tribe/horizon responsible.

### Fix B — replace retail's quadratic overshoot with a saturating term
`src/projector/behavioral_dna.py::retail_dna` — swap:
```
overshoot = 0.04 * prev_state * abs(prev_state) / 100.0
```
for a tanh-shaped saturator:
```
overshoot = 40.0 * math.tanh(prev_state / 400.0)   # asymptote ≈ ±40 bps/step
```
and cap `chase` at 4× with an absolute ceiling `min(4·prev_delta, 200.0)`.

Pros: fixes the mechanism, not the symptom; keeps retail's directional panic behaviour while killing the super-exponential term. Cons: changes retail's realized tape fit; needs the full WS3 H2 sprint re-evaluated to confirm no regression in panic-regime realism.

### Fix C — pre-write sanity check on `expected_move_bps` at anchor enqueue
`src/core/engine.py::_compute_predictions` — if `abs(bps_at) > MAX_REASONABLE_BPS` (e.g., 2000 bps) or non-finite, skip the anchor and increment a `forecaster.anchor_dropped` counter.

Pros: last line of defense; keeps the ledger / auditor clean even if §4.A and §4.B both fail. Cons: silent-drop risk — wire to the same counter bus as Fix A.

### Fix D — projector state reset on detected tape gap
In the live-loop driver, when `tick_ts - last_tick_ts > 300s`, reset:
- `ForecastAuditor.resolved` window truncation (drop pre-gap residuals that will no longer be representative)
- `conviction_history` warm-start buffers
- `calibrator.last_gain`/`last_correction` flagged as "post-gap; use cold-start defaults for one projection"

Pros: addresses the trigger directly. Cons: changes operational semantics (what "running live" means across silences); should be behind a flag until WS3 R2 validates it.

### Fix E — strengthen mean-reversion
`config/settings.yaml: projector.mean_reversion_rate: 0.005 → 0.015` (or make it horizon-aware: stronger at long horizons).

Pros: one-line YAML, instantly damps runaways. Cons: the existing comment explicitly warns this was *dialed down* because stronger reversion "was erasing consensus too hard." Needs WS3 H2 + WS3 R2 re-runs to prove the trade-off.

### Attribution discipline

1. Land **Fix A** first alone. Re-run live-replay on 04-21 → expect explicit clamp counter > 0 and no 10³⁰⁰ anchors.
2. Land **Fix C** second alone. Expected: no new effect on 04-21 once A is in (it's defense-in-depth).
3. Land **Fix B** third. Expected: clamp counter drops to 0 on 04-21; retail panic still visible but bounded.
4. Defer **Fix D** and **Fix E** until WS3 H2 has a clean post-A/B/C re-baseline; treat those as WS3 R2 candidates with their own attribution sprints.

Each commit message should reference this audit (`research/phase11_diagnostic/proj_divergence_20260421_audit.md`) and, for A and C, include the log/counter wiring so the diagnostic surface isn't silenced.

## §5. Decision input — can 04-21 re-enter the acceptance panel?

**Recommendation: 04-21 stays diagnostic-only until Fix A (minimum) lands AND 04-21 is re-replayed post-fix.**

Reasoning:

- The 69 explicit `>1e8` anchors are unambiguously invalid inputs to any downstream stat (realized-residual variance, skill MLE, calibrator fits).
- The **sigma-feedback tail** likely contaminates more than 69 anchors. The 3600s residual window means even "clean-looking" anchors in the 14-minute contamination window have inflated shock magnitudes baked in. A rough envelope: `≈ 14 min × ~6 ticks/min × 2 horizons = ~160 anchors` need audit; the 69 explicit + 19 None = 88 are only the ones that *visibly* overflowed.
- Even beyond the explicit window, the auditor's 3600s residual window means the next ~45 min of anchor-scoring inherits inflated sigma. A conservative cut would treat 2026-04-21 08:44 → 13:00 UTC as quarantine.
- Once Fix A lands and 04-21 re-replays clean (zero clamp events OR bounded clamp events that are traceable), the day should be allowed back into the panel under a `rebaseline_date` marker in `WS3_H2_option1a_snapshot.md` §5.

**Permanent-exclude only if** a re-replay under A+B+C still shows pathology — i.e., the gap itself (not just retail's panic response) produces a fundamentally non-recoverable projector state. Current evidence does not support that: the live pipeline on 04-18/19/20 recovered cleanly from gaps up to 569 min.

## Appendix — evidence artefacts

- Live forecasts: `data/forecasts/2026-04-{18,19,20,21}.jsonl` (analysed in §3.1).
- Sprint forecasts: `research/phase11_diagnostic/_ws3_h2_step4_forecasts/2026-04-{18,19,20,21}.jsonl` and `*_gain.json`.
- Tape-gap scan output: §3.1 table, derived from `data/forecasts/*.jsonl` tick_ts sequences.
- First-contaminated-anchor coordinates: `tick_ts = 1776771541.40` (11:39:01 UTC 2026-04-21), `expected_move_bps = 4.446e+10`, `label = 1m`.
- Explosion walkthrough: §1.3 hand trace, consistent with `behavioral_dna.retail_dna` panic-branch code as of HEAD `c73c370`.
- No production code changed in this worktree (`git status` clean).
