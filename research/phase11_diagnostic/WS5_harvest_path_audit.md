# WS5 — Harvest-Mass Path Audit

**Audit type:** Forward code trace, pre-live-tape.
**Status:** Complete.
**Date:** 2026-04-24.
**Branch / base commit:** `feat/tensor-engine` @ `827040e` (post-H1.0, post-H1.I1 instrumentation).
**Companion docs:** `PLAN.md` §Q5, `MEASUREMENT_INFRA.md` §2 (B-arc closure), `WS3_H1_SCOPE.md` §11 (parallel-workstream policy).

---

## 0. Headline

**The harvest-mass → `conviction_boost` path cannot fire in sprint by construction.** It is not a quantitative gating problem (too-weak `net_pressure`, too-small boost, clamp elsewhere in the stack). It is a *categorical* absence: `ChessProjector.calibrator` is `None` in sprint, the `contrarian_tids` population loop at [chess.py:443-455](src/projector/chess.py:443) is guarded on that attribute, so the set is empty, so `harvest_mass = 0`, so `harvest_mass_ratio = 0`, so `conviction_boost = 1.0` on 100% of sprint opens.

The MEASUREMENT_INFRA §2 hypothesis ("B fires but ~400× weaker than sprint") is **superseded by a sharper one**: in sprint, B has no input — it is physically deaf, not quietly whispering. The remaining open question is whether the live path (where the calibrator IS wired) fires B at a rate that would materially change the conviction-boost distribution, and with what magnitude. That measurement is now the sole load-bearing test of this path; the instrumented auditor from H1.I1 is the vehicle.

---

## 1. Antecedents

- **R2 B closure (sprint)** established that across the ~1k-tick sprint-tape sample, `conv_mult = 1.00` on **100.0%** of Decision-engine opens. MEASUREMENT_INFRA §2 noted the symmetric pressure signal in sprint was ≈400× weaker than in live, and hypothesized that B *was* firing but scaled below detection threshold.
- **PLAN.md Q5** frames WS5 as the harvest-boost ablation: remove the amplification path, measure Δ. Deferred to post-H1/H3 on the grounds that H1-bound confidence means no conviction would be left to amplify anyway.
- **WS3_H1_SCOPE.md §11** opened the door for parallel WS-X audits on code paths that do not perturb the confidence formula, auditor schema, or regime-label state machine. The harvest path meets that bar: read-only forward trace through `ChessProjector.forecast` and `DecisionEngine.decide_open`, no instrumentation changes required.
- **H1.I1 just landed** with a determinism gate (short-window byte-identical, full-day running at draft time). The auditor now carries `signal_quality`, `directional_score`, `base_conf`, `diversity_score`, `diversity_mult` on every Forecast / PendingAnchor / Resolved row. **It does not yet carry** `conviction_boost` or `harvest_mass_ratio` — the H1.I1 extension was confined to the confidence-formula decomposition. Any WS5 follow-up measurement that wants to condition `conviction_boost` on calibrator state will need an H1.I3-style plumbing extension.

---

## 2. Forward trace — contrarian classification → harvest_mass → conviction_boost

### 2.1 Contrarian population ([chess.py:443-455](src/projector/chess.py:443))

```python
contrarian_tids: set[str] = set()
if self.calibrator is not None:
    projector_horizon_s = float(self.default_horizon_seconds)
    for t in self.tribes:
        try:
            cube_hit = self.calibrator.contrarian_mask(
                t.spec.id, self.regime_label, projector_horizon_s,
            )
            scalar_hit = self.calibrator.is_contrarian_candidate(t.spec.id)
            if cube_hit or scalar_hit:
                contrarian_tids.add(t.spec.id)
        except Exception:
            pass
```

- `contrarian_mask` ([calibrator.py:455-500](src/audit/calibrator.py:455)) is a 3-D lookup over `(tribe_id × regime_label × horizon_bucket)`. Returns True when the bucketed EWMA hit-rate is below `hit_rate_threshold=0.45` and sample count ≥ `min_samples=30`.
- `is_contrarian_candidate` ([calibrator.py:502-535](src/audit/calibrator.py:502)) is the scalar fast-EWMA fallback. Returns True when the tribe-level EWMA < `hit_rate_threshold=0.45` and sample count ≥ `min_samples=50`.
- Both require *confirmed* under-performance, not ambiguity — a tribe with no samples, or a 50-50 hit rate, is **not** flagged contrarian.
- `cube_hit OR scalar_hit` means cold-start (cube below 30-sample threshold) still fires the flip via the scalar path once 50 samples accumulate.

### 2.2 Harvest mass accumulation ([chess.py:727-740](src/projector/chess.py:727))

```python
harvest_mass = 0.0
for tid in contrarian_tids:
    mass = pool_w.get(tid, 0.0)
    if tid in top_quartile:
        mass *= hq_boost
    harvest_mass += mass
harvest_mass_ratio = (
    harvest_mass / total_mass if total_mass > 0.0 else 0.0
)
harvest_mass_ratio = max(0.0, min(1.0, harvest_mass_ratio))
conviction_boost = 1.0 + harvest_scale * harvest_mass_ratio
```

- `pool_w[tid]` is the per-tribe pool weight — mass that the council would have allocated to a tribe that is currently classified as *reliably wrong*. The flipped-vote already inverts the directional signal (chess.py:466-); the harvest is the *sizing* recognition of the same insight.
- `hq_boost = clamp(1.0, 4.0, self._harvest_top_quartile_boost)` — the inflation for contrarian tribes that also happen to be top-quartile by trading-score. Encoded in `_harvest_top_quartile_boost`, typically 1.5–2.0 in prod config. Used to weight the case where a *good* tribe has gone briefly wrong harder than the case where a *bad* tribe has gone predictably wrong.
- The final clamp to `[0, 1]` preserves the original 1.5× cap: with `harvest_scale = 0.5`, `conviction_boost ∈ [1.0, 1.5]`.
- `conviction_boost` is stamped onto the Forecast at [chess.py:887](src/projector/chess.py:887) and persists through to the decision engine.

### 2.3 Size amplification ([decision/engine.py:142-166](src/decision/engine.py:142))

```python
conv_mult = float(getattr(forecast, "conviction_boost", 1.0) or 1.0)
if conv_mult <= 0:
    conv_mult = 1.0
# Task 4c — Path-Integrity.
path_integrity_ok = True
dsa = getattr(forecast, "driver_skill_avg", None)
if dsa is not None and float(dsa) < 0.7:
    path_integrity_ok = False
    agree_mult = min(agree_mult, 1.0)
    crowd_mult = min(crowd_mult, 1.0)
    htf_mult = min(htf_mult, 1.0)
    horizon_mult = min(horizon_mult, 1.0)
    conv_mult = min(conv_mult, 1.0)
size_usd = (equity_usd * pct / 100.0 * self.leverage
            * sigma_mult * agree_mult * htf_mult
            * crowd_mult * horizon_mult * conv_mult)
```

- `conv_mult` is the only accelerator ∈ [1.0, 1.5] in the multiplier chain; every other multiplier is a brake ∈ [floor, 1.0].
- **Path-Integrity clamp**: when `driver_skill_avg < 0.7`, `conv_mult` is capped at 1.0. Rationale encoded in comment at engine.py:145-162: if the driving tribes are themselves calibrator-pinned as low-skill, take the direction but refuse the amplification. This is a **silent anti-harvest**: it nullifies the sizing benefit of a contrarian-flip when the *flipped* tribe is itself still below-trust. Guards against the double-dip where a tribe is both contrarian-flipped AND low-skill, which would otherwise compound to "trust the flipped direction enough to leverage into it."

---

## 3. Sprint-vs-live asymmetry — the categorical cause

### 3.1 Sprint ([replay/harness.py:324](src/replay/harness.py:324) vs module docstring [78-79](src/replay/harness.py:78))

- Projector constructed at harness.py:324 via `projector = ChessProjector(...)` with **no calibrator kwarg**. No post-construction attachment anywhere in the sprint path.
- The module docstring makes the design intent explicit:
  > "The tribe `calibrator` (TribeCalibrator) is still absent in sprint — it ships when the summary report needs per-tribe skill-shift metrics."
- So `self.calibrator is None` at chess.py:444 is True on every sprint tick, the `if` body is skipped, `contrarian_tids` stays empty, harvest_mass stays 0, `conviction_boost` stays at 1.0.

### 3.2 Live ([scripts/run_bot_ui.py:188](scripts/run_bot_ui.py:188))

- Live bootstraps a `TribeCalibrator` in its wiring block and assigns it post-construction: `projector.calibrator = calibrator`. The attribute is thus live-populated before the first tick.
- The calibrator accumulates hit-rate samples as resolved outcomes stream in from the runtime loop (the same resolution path that fills `Resolved` rows in the auditor).
- Once enough samples accrue for a tribe × regime × horizon cell to clear the 30-sample floor, the cube mechanism can begin flipping that tribe contrarian; the scalar fallback starts firing once the tribe-level fast-EWMA clears 50 samples.

### 3.3 Why this asymmetry is the right root-cause framing

- MEASUREMENT_INFRA §2's "400× weaker net_pressure in sprint" is real but **downstream** of the asymmetry. Even if sprint's pressure were 1× live, the harvest path would still fire 0.00% of the time, because the gating mechanism is absent, not undersupplied.
- The "400× weaker" finding is a correct observation about a *different* path: the symmetric pressure signal feeds the agreement / crowding multipliers, which are brake-only. The harvest accelerator is gated by `contrarian_tids`, which is gated by the calibrator.
- This means *fixing sprint-dead-B by changing pressure magnitudes in sprint is impossible by construction*. You would need to wire the TribeCalibrator in sprint (and provide it with a sample stream from historical outcomes) to produce any B firing at all. That's a larger scope than the current Phase-11 remit, and may not be worthwhile — see §5.

---

## 4. Path-Integrity clamp interaction

- The clamp at engine.py:155-162 silently guts `conv_mult` whenever `driver_skill_avg < 0.7`. It is intentionally asymmetric: agrees, crowd, htf, horizon brakes are all capped at 1.0, which they already respect — the clamp is a no-op for them unless something upstream has momentarily pushed them above 1.0. For `conv_mult` specifically, the cap is load-bearing: it is the only multiplier that is natively > 1.0.
- **Consequence for WS5 measurement**: in live, the conviction-boost path can fire (calibrator wired), but the *effective* sizing boost conditional on a fired boost depends on `driver_skill_avg`. Live-tape analysis will need to bucket by `path_integrity_ok`:
  - **`dsa ≥ 0.7` AND `conviction_boost > 1.0`**: true harvest hit. Sizing amplified up to 1.5×.
  - **`dsa < 0.7` AND `conviction_boost > 1.0`**: latent harvest hit. Boost computed but clamped to 1.0. In the log as `conv_mult = 1.0` identically with the no-harvest case.
- **Consequence for the sprint-dead finding**: the Path-Integrity clamp is *not* why sprint's `conv_mult = 1.00` on 100% of opens. Sprint has `conv_mult = 1.00` because `conviction_boost = 1.00` at the source, pre-clamp. The two are indistinguishable in the existing logs.

---

## 5. Verdict and recommendations

### 5.1 Verdict

- **Sprint cannot fire B by construction.** The "400× weaker signal" hypothesis for sprint-dead conv_mult is superseded by the categorical absence of the TribeCalibrator. Sprint logs are uninformative for B-arc measurement by design.
- **The live path is the sole measurement surface for whether B matters.** The H1.I1 auditor does not yet carry `conviction_boost` or `harvest_mass_ratio`, so the measurement requires an H1.I3-style plumbing extension (add the two fields to `Forecast → PendingAnchor → Resolved`) before any production data is interpretable.
- **No pre-H1 code change required.** The harvest path is clean-by-design; the sprint-dead observation is diagnostic of sprint's wiring, not of the path itself. WS5 ablation (remove the harvest path entirely, measure Δ) remains a valid post-H1/H3 experiment, unchanged in scope.

### 5.2 Recommendations

1. **Do not backport the calibrator to sprint.** Cost/benefit is poor: sample accumulation over a single sprint (~6h of ticks) is unlikely to clear even the 30-sample cube floor for most tribe × regime × horizon cells, and the scalar-50 threshold probably takes a full sprint plus part of a second before firing. Sprint stays deaf; this is acceptable because sprint's job is *tape realism*, not *calibrator feedback realism*.
2. **Sequence an H1.I3 plumbing commit after H1.I2 landing** (no conflict with the in-flight H1 phase-1 measurement — the new fields are additive decomposition, not a formula change). Pass-through of `conviction_boost` and `harvest_mass_ratio` into the auditor is trivial; adding `path_integrity_ok` is also worth it since the Path-Integrity clamp masking effect is otherwise invisible.
3. **Live-tape WS5 measurement, once ≥ 3 days of post-H1.I3 production data accrue:**
    - Bucket opens by `(harvest_mass_ratio > 0)`, `(conviction_boost > 1.0)`, `(path_integrity_ok)`. Expect four cells with meaningful mass.
    - Report: firing rate (% of opens where `conviction_boost > 1.0`), clamp rate (% of firings where the clamp applied), effective-boost distribution (histogram of final `conv_mult` conditional on firing), and PnL Δ between fired/not-fired (matched on regime and signal decomposition).
    - Compare against the symmetric "400× weaker pressure" framing: if firing rate is <1%, the B arc is effectively dead live as well, and MEASUREMENT_INFRA §2's intuition turns out to have been right for the wrong reason. If firing rate is ≥10%, B is meaningfully live and the sprint-dead finding is a sprint-artifact only.
4. **Do not gate H1 closure on this.** WS5 is independent. The H1 forward map (§5 in PLAN.md) keeps its own critical path; this audit removes WS5 from the confidence-is-possibly-bug column and moves it to the post-H1 ablation column unchanged.

### 5.3 Scope discipline note

Per `WS3_H1_SCOPE.md` §11, this audit performed no changes to the confidence formula, the auditor schema, the regime-labeller, or the decision engine. It is a read-only forward trace with a verdict, matching the parallel-workstream policy for WS-X audits that can run concurrently with the H1 determinism gate. The H1.I3 plumbing extension flagged in §5.2.2 is non-blocking and may itself land in parallel with H1.I2 analysis work.

---

*Filed in service of: R2 B closure (sprint leg), WS5 pre-live-tape scoping, H1.I3 plumbing requisition.*
