# WS3 H5 — Phase 0 `step_bps_list` construction audit

**Audit box:** 2026-04-24 22:49 → 23:29 CET (40 min of the 1h box).
**Verdict:** **BUG-FOUND** — specifically an observability/saturation bug on the forecast output path. The deeper explosive dynamic driving per-tribe `curve[h]` magnitudes is present but characterised (not yet root-caused at the DNA level). H5.A can ship as a defensive clamp; a subsequent H-thread can chase the recursive dynamic.

**Baseline:** `data/baseline/pathological_2026-04-24_pre-H5A/` (SHA256 manifest + README).
**Scope questions answered:** Q1, Q2, Q3, Q5 from [WS3_H5_SCOPE.md §2](WS3_H5_SCOPE.md).

---

## 1. Headline findings

- **The 1/99 attractor is mathematically exact**, not an artifact: `directional_score = (1 − d)/(1 + d)` with `d = confidence_decay = 0.98` evaluates to `0.01010101…` — the ratio observed on 1,974 resolved anchor rows (all 0.00698–0.01154, median 0.01010).
- **The pattern isn't "pure alternation across n_steps"** — `step_bps_signflips` ranges 1–5, not 249. The attractor is produced by **steps_bps containing a single-horizon spike**: `steps_bps = [0, …, 0, +HUGE, 0, …, 0]` yields `step_bps_list = [0, …, 0, +HUGE, −HUGE, 0, …, 0]` — signflips=1, directional=1/99, abs_activity=HUGE.
- **No saturation exists anywhere** between projector math and auditor persistence. `math.isfinite / isinf / isnan` appear zero times across `chess.py`, `engine.py`, `backscore.py`.
- **Persisted resolved-anchor `expected_move_bps` stays bounded** (`max |emove| = 11.6 bps`, 0 rows ≥ 1e6) because anchors only sample `steps_bps[0]` and `steps_bps[11]` — and the spike lands at other horizons. The per-anchor emove field is **not** `forecast.expected_move_bps`; it's `forecast.steps_bps[step_idx-1]` (see engine.py:199,206). Two different quantities, which closes Q5.

## 2. Q-question answers

### Q1 — Is there a clamp in step_bps_list construction?

**No.** Trace:

- `chess.py:785-787` builds `step_bps_list` as first differences of `steps_bps` — no clamp.
- `chess.py:800,804` sums `step_bps_list * kernel` for emove + abs_activity — no clamp.
- `chess.py:908` writes `abs_activity` directly into the `Forecast` dataclass — no clamp.
- `engine.py:419` reads `forecast.abs_activity_bps` into `record_tick(...)` — no clamp.
- `backscore.py:224` writes it into `PendingAnchor` — no clamp.
- `backscore.py:280` copies it into `Resolved` on maturation — no clamp.
- `backscore.py:324` persists the raw value into the jsonl — no clamp.

The 6.5e+295 value reached the disk untouched. If Python had overflowed to `inf`, the default JSON encoder would have emitted `Infinity` or raised; we see finite huge numbers, so `float64` is holding the magnitude cleanly. **No pre-clamp, no post-clamp, no guard.**

### Q2 — Where are step_bps values computed before being added to the list?

Two stages:

**Stage 1 — per-tribe recursive walk** ([chess.py:561-619](src/projector/chess.py)):

```
step_bps = total_bps * scale                        # total_bps = dna_delta + event_bps
if mean_reversion_rate > 0 and cum_bps != 0:
    step_bps -= mean_reversion_rate * cum_bps
cum_bps += step_bps
curve.append(cum_bps)
```

With `mean_reversion_rate = 0.005` and depth-scaling capped at `depth_ref / depth_floor = 10M / 500k = 20×`, this is a damped AR(1) — `cum_bps[h+1] = 0.995·cum_bps[h] + total_bps·scale`. Stable with geometric decay 0.995, bounded to `O(200 · max(total_bps·scale))`. Cannot mathematically produce 1e150 magnitudes **unless** `total_bps` itself is unbounded.

**Source of unbounded `total_bps`:** `dna_delta = dna_fn(DNAContext(prev_state_bps=cum_bps, …))` — the DNA function receives the current `cum_bps` and may amplify it. **Not audited in Phase 0** (box budget). At least one DNA category produces delta ∝ cum_bps, making the recursion effectively `cum_bps[h+1] ≈ cum_bps[h] · (0.995 + K)` — exponential blowup for K > 0.005.

**Stage 2 — weighted consensus** ([chess.py:749-769](src/projector/chess.py)):

```
steps_bps[h] = Σ_tribes (curve[tid][h] · atw[tid][h] · sk_h[tid][h] · pool_w[tid])
             / Σ_tribes (atw[tid][h] · sk_h[tid][h] · pool_w[tid])
```

All weights are bounded (`atw ∈ [0.05, 2.5]`, `sk_h ∈ [0.6, 1.4]`, `pool_w ≥ 0`). The ratio is a weighted mean — cannot blow up on its own. If per-tribe `curve[h]` is huge, `steps_bps[h]` inherits it.

### Q3 — Is there a separate code path producing 1e120 values?

**No, same code path — different field.** Confusion arose from two distinct quantities both called emove:

1. **`forecast.expected_move_bps`** = `Σ step_bps_list · kernel` ([chess.py:800](src/projector/chess.py)). Logged at [engine.py:387](src/core/engine.py). Can reach ±1e290 bps when `step_bps_list` contains massive alternating entries.
2. **`predictions[h].expected_move_bps`** = `steps_bps[step_idx-1]` ([engine.py:199,206](src/core/engine.py)). Persisted into auditor anchors. Stays bounded because anchors only sample `step_idx=1` and `step_idx=12` (labels `15s` and `1m`), and the single-horizon spike typically lands outside those two horizons.

The same forecast object contains both a huge quantity (kernel-summed emove) and a bounded quantity (per-anchor emove). **Not two bugs — one bug × two fields.**

### Q5 — log-emove vs jsonl-emove discrepancy

**Closed.** The log at [engine.py:387](src/core/engine.py) prints `forecast.expected_move_bps` (kernel sum, field #1 above — can be 1e290). The jsonl persists `forecast.steps_bps[step_idx-1]` (per-anchor cumulative, field #2 — stays bounded because step 0 and step 11 happen to fall outside the spike). No silent clamp between them; they were never meant to be the same quantity.

The "sane jsonl emove" is not protective math — it's **observational selection**: the 2 anchors we sample are the 2 horizons where the spike is absent. A 3rd anchor at a horizon near the spike would show huge emove too.

## 3. Root cause ladder

| Layer | Status | Evidence |
|---|---|---|
| **Symptom** — auditor persists 1e+295 abs_activity_bps | ✅ reproduced | 1974/12011 rows with abs_act > 1e6 |
| **Mathematical fingerprint** — directional = 1/99 attractor | ✅ proven | `(1 − 0.98)/(1 + 0.98) = 0.01010101`; observed median 0.01010 |
| **Structural shape** — step_bps_list is single adjacent ±spike | ✅ inferred from signflips=1 + directional=1/99 | Only pattern that produces both |
| **Source of spike magnitude** — one horizon's `steps_bps[h]` is huge | ✅ corollary of above | steps_bps[h] = HUGE, neighbours = 0 |
| **Source of huge `steps_bps[h]`** — some tribe's `curve[h]` is 1e150 | ✅ corollary (weighted mean can't amplify) | |
| **Source of huge `curve[h]`** — DNA-driven recursive amplification | 🟡 **not audited in Phase 0** | `total_bps = dna_delta + event_bps`; DNA receives `cum_bps` as context, can amplify |
| **Which DNA category triggers the blowup** | ⛔ out of H5 Phase 0 scope | Needs per-tribe `curve` tracing across tape |

## 4. H5.A — recommended fix (defensive, not curative)

**Not yet committed — awaiting user check-in** per [feedback_audit_fix_checkin.md](../../../.claude/projects/C--Users-agent-Desktop-TRADING-BOT/memory/feedback_audit_fix_checkin.md).

**Proposed scope for H5.A:**

- Saturation clamp at the Forecast output in `chess.py:800,804` — any single-step `|step_bps_list[h]|` exceeding `MAX_STEP_BPS` (proposed: 500 bps, ~5% single-step move — already 25σ on BTC 5s-bar) triggers a NaN sentinel on `abs_activity_bps` and `expected_move_bps`, with a log warning.
- Downstream contract: NaN sentinel → `directional_score = None` → DecisionEngine already treats None as "incoherent council → hold". No new incoherence logic needed.
- Zero touch on per-tribe recursion, DNA, or calibrator. **The recursive blowup stays intact** but its output is prevented from corrupting the persisted tape.

**What H5.A explicitly does NOT do:**

- Fix the DNA recursion amplification. That's a separate H-thread (proposed: **H6** — "behavioral-DNA positive-feedback audit"). Scope TBD after H5.A demonstrates the clamp doesn't mask other downstream breakage.
- Touch `confidence_decay`, `mean_reversion_rate`, `depth_*` params. Those are calibrator/config choices; the bug is in saturation, not in parameter values.
- Touch `kernel_calibrator.py`. The clamp there works correctly — confirmed in the scope re-read.

**Files expected to change** (within the 3-file guardrail):
- `src/projector/chess.py` — 5-10 lines: isfinite check + saturation check + NaN sentinel emission.
- `tests/projector/test_chess_saturation.py` (new) — unit test reproducing the spike pattern and verifying the clamp kicks in without affecting sane forecasts.

## 5. Acceptance check against H5 scope

- [§2 gate] "bug-found → fix commit → re-audit" — **bug-found confirmed**, fix track is H5.A. ✅
- [§2 line 181] "docs commit only. Any bug-fix surfaces as a separate commit" — **this doc is audit-only**, no code touched. ✅
- [§4.1 Option A] "Construction fix (bug-found track)" — H5.A maps to the observability-clamp sub-track, not the DNA-recursion sub-track. ✅

## 6. Out-of-scope escalations (documented, not pursued)

1. **H6 candidate** — DNA-recursion positive-feedback audit. Mechanism: `dna_delta = f(cum_bps)` with `∂f/∂cum_bps > mean_reversion_rate`. Needs per-tribe curve tracing across the pathological baseline tape.
2. **Q-chip** — anchor coverage. Currently only 2 anchor horizons are persisted (15s, 1m). Sampling at step_idx ∈ {1,6,12,25,50} would have surfaced the spike at audit time rather than through log forensics.
3. **Equity exposure** — live run pre-kill showed `eq=$48,984` vs starting (~50k), `open=12 positions`, `net_exp=$−244,696` at `conf=0.79` with `emove=NaN` in logs. How does `conf=0.79` with NaN emove pass the decision gate? Separate finding — escalate to decision/engine.py audit.

## 7. Box accounting

- Phase 0 box: 60 min.
- Used: ~40 min (snapshot, kill, construction trace, attractor math, persistence audit, report).
- Remainder: 20 min reserved for user check-in response + any adjustments before H5.A starts.
