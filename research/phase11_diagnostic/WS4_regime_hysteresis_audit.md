# WS4 — Regime-Classifier Hysteresis Audit

**Audit type:** Forward code trace + empirical dwell/transition characterization.
**Status:** Complete (measurement-lite — sprint tape only).
**Date:** 2026-04-24.
**Branch / base commit:** `feat/tensor-engine` @ `c46670a` (post-WS5).
**Companion docs:** `PLAN.md` §Q4, `WS2.5_regime_gate.md` §7 (regime sensitivity raised), `WS3_H1_SCOPE.md` §11 (parallel-workstream policy).

---

## 0. Headline

**Classifier is stateless by design** ([classifier.py:76-157](src/regime/classifier.py:76)); every tick is independently labelled from 300 s rolling features, no dwell / Schmitt / EWMA-on-label is applied. On the sprint-2026-04-20 baseline (16,811 ticks), this produces **1 transition every 13 ticks** and **54% of regime runs last ≤ 5 ticks**. 43% of all transitions are `chop ↔ normal` pairs and they arrive near-perfectly bidirectional (274 vs. 271), the signature of threshold flicker rather than genuine regime alternation. **However**, because of the skewed run-length distribution, only ~21% of *ticks* sit inside short (≤10-tick) bursts — 79% of time is inside stable long runs.

**Verdict:** the classifier is flicker-prone near thresholds, but the practical damage is bounded by the heavy-tailed dwell distribution. Three concrete damage paths exist:

1. **Calibrator cube sample dilution** — the (tribe × regime × horizon) cells at [calibrator.py:367-379](src/audit/calibrator.py:367) accumulate samples under whatever label was stamped at vote time. Short flicker bursts in labels split what is structurally one regime across two cells, delaying the 30-sample firing threshold for `contrarian_mask`. Affects ~21% of tick-attributed samples.
2. **`regime_multiplier` tick-to-tick swing** — chop→normal flickering swings the projector's `effective_gain` by ±40% (0.6 vs. 1.0); trend→normal by ±20%. Propagates directly into `steps_bps` and therefore into `signal_quality` / `directional`, the H1-bound confidence-formula inputs.
3. **WS2.5 gate false-close** — if `allowed_entry_regimes` is ever enabled (it isn't today), flicker out of `trend_{up,down}` would gate-off entries at the ~27-per-1000-ticks trend↔normal flicker rate.

**Not a blocker for H1.** Confidence formula inputs are regime-multiplier-modulated but not regime-label-gated structurally; the 0.25 cap hypothesis survives classifier flicker. WS4 hysteresis intervention is a **post-H1 candidate**, priority raised if H1.I2 data shows a live-tape flicker rate > 2× the sprint baseline.

---

## 1. Antecedents

- **WS2.5_regime_gate.md §7** explicitly flags regime sensitivity: "Backward-looking classifier… The trend label means 'the last N minutes were trending,' not 'the next M minutes will'" + "Regime distribution is tape-dependent. 2026-04-20 was 26% trend." These are day-level concerns; they did not reach classifier-tick-level flicker.
- **PLAN.md §Q4** frames WS4 as: "classifier stability across 5–60 s horizons is a raised-priority follow-up." This audit operationalizes "stability" as two measurements: per-tick label-transition frequency and run-length dwell distribution.
- **WS3_H1_SCOPE.md §11** permits WS4 audit in parallel to H1 iff no changes touch the confidence formula, auditor schema, or regime-labeller. This audit is read-only, trivially meets the bar.
- **WS5_harvest_path_audit.md §3** established that the calibrator's cube mechanism (keyed on `regime_label × horizon_bucket`) is live-only. Therefore any sample-dilution concern from classifier flicker applies **only to live**; sprint can't fire the cube in the first place.

---

## 2. Classifier structure — stateless by construction

### 2.1 Code trace ([classifier.py:76-157](src/regime/classifier.py:76))

```python
class RegimeClassifier:
    """Single-tick regime labeler. Stateless between calls except for the
    thresholds; the classifier reads everything it needs off MarketState."""

    def classify(self, state: MarketState) -> Regime:
        vol_bps, drift_eff, signed_drift = self._volatility(state)
        liq_usd = state.long_liq_usd(self.liq_window_s) + state.short_liq_usd(self.liq_window_s)
        if liq_usd >= self.cascade_liq_usd or vol_bps >= self.cascade_vol_bps:
            label = CASCADE_UP if signed_drift >= 0 else CASCADE_DOWN
            ...
        if vol_bps <= self.chop_vol_bps or drift_eff <= self.chop_eff:
            return CHOP
        if drift_eff >= self.trend_eff:
            return TREND_UP/DOWN
        return NORMAL
```

- **No hidden state**: no previous label, no dwell counter, no smoothing buffer inside the classifier. Every call is a pure function of `(state, thresholds)`.
- **Threshold defaults** (classifier.py:84-88):
  - `chop_vol_bps = 15`, `cascade_vol_bps = 60` — vol thresholds
  - `chop_eff = 0.30`, `trend_eff = 0.55` — directional-efficiency thresholds
  - `cascade_liq_usd = 5_000_000` — liquidation threshold
- **OR-structured predicates** at 103 and 112 are the primary flicker surfaces: cascade fires on `liq ≥ 5M` **OR** `vol ≥ 60`, chop fires on `vol ≤ 15` **OR** `drift_eff ≤ 0.30`. Either axis can flip the label independently.
- **No dwell gap between bands**: `drift_eff ∈ (0.30, 0.55)` lands in NORMAL. A drift_eff oscillating around 0.30 flickers CHOP↔NORMAL at the first-derivative noise rate; around 0.55 flickers NORMAL↔TREND. There is no Schmitt-trigger hysteresis to suppress this.

### 2.2 De-facto feature smoothing

- Features are computed over the `vol_window_s = 300` second rolling window ([classifier.py:82](src/regime/classifier.py:82), [classifier.py:129-140](src/regime/classifier.py:129)) and `liq_window_s = 60` second. The *features* are therefore heavily smoothed — a 1-tick microstructure event won't move `vol_bps` meaningfully.
- **But** when the feature value sits near a threshold, even slow feature drift is enough to flicker the label: the label transition is discrete (step function at the threshold), while the feature is continuous. Near-threshold dwell = label flicker.
- **Net**: feature smoothing protects against *noise-driven* flicker (tick-scale), not *drift-driven* flicker (minute-scale sitting in the grey band).

---

## 3. Empirical characterization — sprint 2026-04-20 baseline

Source: `data/baseline/sprint_runs/baseline_2026-04-20/variants/phase0/decisions.jsonl`, 16,811 ticks, one per second.

### 3.1 Label share

| Label | Count | Share |
|:------|------:|------:|
| chop        | 9,836 | 58.5% |
| normal      | 2,386 | 14.2% |
| trend_up    | 2,377 | 14.1% |
| trend_down  | 2,047 | 12.2% |
| cascade_up  |   112 |  0.7% |
| cascade_down |   53 |  0.3% |

Matches the WS2.5_regime_gate.md description of 2026-04-20 as a trend-present, cascade-light tape. Chop-dominance is consistent with sprint's weaker pressure signal (MEASUREMENT_INFRA §2) keeping `drift_eff` below `trend_eff = 0.55` most of the time.

### 3.2 Transition frequency

- **Total transitions: 1,268**.
- **Rate: 75.4 transitions per 1000 ticks** ≈ **1 transition every 13 ticks**.
- Top 6 transitions are all bidirectional near-threshold pairs:

| Transition | Count | Count (reverse) | Sum | Symmetry |
|:-----------|------:|----------------:|----:|:---------|
| chop → normal       | 274 | normal → chop: 271       | 545 | 50.3/49.7 |
| normal → trend_down | 137 | trend_down → normal: 136 | 273 | 50.2/49.8 |
| normal → trend_up   | 135 | trend_up → normal: 133   | 268 | 50.4/49.6 |
| trend_up → chop     |  43 | chop → trend_up: 42      |  85 | 50.6/49.4 |
| trend_down → chop   |  40 | chop → trend_down: 39    |  79 | 50.6/49.4 |

- **545 / 1268 = 43.0% of all transitions are chop↔normal**. The near-perfect 274:271 symmetry (within 0.5%) is the signature of a *random walk across a threshold*, not a true regime alternation. A tape that genuinely switched between two regimes would show asymmetric counts (more → into the current regime than ← out of it).
- The trend↔normal pairs show the same signature (135:133, 137:136).

### 3.3 Dwell-length distribution

| Run length (ticks) | Runs | Share of runs |
|:-------------------|-----:|--------------:|
| 1        | 176 | 13.9% |
| 2        | 194 | 15.3% |
| 3–5      | 315 | 24.8% |
| 6–10     | 217 | 17.1% |
| 11–30    | 273 | 21.5% |
| 31–100   |  72 |  5.7% |
| 101–500  |  22 |  1.7% |
| **total**| 1,269 | **100%** |

- **54.0% of runs last ≤ 5 ticks**. **71.1% last ≤ 10 ticks**.
- Dwell **median: 5 ticks; mean: 13.2 ticks; max: 491 ticks** (one long chop run).

### 3.4 Tick-weighted exposure

Two numbers matter differently depending on the damage path:

- **Run-count-weighted**: 71% of runs are ≤ 10 ticks. Relevant for *cell accounting* — every short run creates a discrete sample dilution event in the calibrator cube.
- **Tick-weighted**: only **20.6% of ticks** are inside runs ≤ 10 ticks; **79.4% of ticks** sit inside longer runs. Relevant for *time-integrated damage* — regime_multiplier swings and WS2.5 gate flicker only bite during the 21% short-run window.

The bimodality is the key structural finding: short flicker bursts are *frequent but brief*, interleaved between long structurally-stable runs. Most of the bot's operating time is flicker-free; but every ~13 ticks a burst happens, and during that burst the downstream damage paths are live.

### 3.5 Caveats

1. **Sprint only.** This is one day of sprint tape. Live flicker rate could differ — pressure is ~400× stronger (PLAN §D6), drift_eff distributions shift, threshold crossings are more/less frequent. Re-running this analysis on a live-tape day once the forecast/decision log is enriched with a regime field is a ~1-hour follow-up (see §5.2.1).
2. **Single day.** 2026-04-20 is the trend-heavy reference day; regime mix varies across 04-18/04-19/04-21. The transition *structure* (bidirectional symmetry) should survive the mix shift; absolute rates may not.
3. **Threshold regime is not literal.** Near-threshold dwell depends on the joint distribution of `vol_bps` and `drift_eff` around the threshold — not on the threshold value itself. We haven't measured the grey-band dwell density directly; we're inferring flicker from transition symmetry. Direct measurement (histogram of `drift_eff` conditional on a regime-flip tick) is another §5.2.1 follow-up.

---

## 4. Blast-radius map — where `regime_label` propagates

| # | Consumer | File:line | Damage mechanism under flicker |
|:-:|:---------|:----------|:-------------------------------|
| 1 | `projector.regime_multiplier` stamping | [core/engine.py:248](src/core/engine.py:248), [replay/harness.py:224](src/replay/harness.py:224) | Per-tick mult swings: chop↔normal is ±40%, trend↔normal is ±20%. Folded into `effective_gain = bps_per_unit_pressure * regime_multiplier` at [chess.py:292](src/projector/chess.py:292); changes `steps_bps` magnitudes tick-to-tick, which feeds signal_quality and directional in the confidence formula. |
| 2 | `projector.regime_label` stamping | [core/engine.py:251](src/core/engine.py:251) | String field on projector; read by the calibrator cube as the regime-key. Flicker = sample-dilution across cells. |
| 3 | Calibrator `record_votes(regime_label=…)` | [core/engine.py:292](src/core/engine.py:292), [audit/calibrator.py:205-233](src/audit/calibrator.py:205) | Vote-time regime is frozen into the snapshot. At resolution, the sample is attributed to vote-time regime, not resolution-time regime — **good**. But flicker at vote time still splits the attribution incorrectly. |
| 4 | Calibrator `_skill_by_regime` | [audit/calibrator.py:347-354](src/audit/calibrator.py:347) | Regime-conditional EWMA; `skill_multiplier` falls back to global when per-regime samples < `_regime_min_samples`. Flicker delays threshold-reach. |
| 5 | Calibrator `_skill_cube` (3-D) | [audit/calibrator.py:367-379](src/audit/calibrator.py:367) | The (tribe × regime × horizon) cube read by `contrarian_mask` ([calibrator.py:458-500](src/audit/calibrator.py:458)). Min-samples = 30. Dilution is worst here because the key has three dimensions — samples spread thinner per cell. |
| 6 | WS2.5 entry gate | [decision/engine.py:172-176](src/decision/engine.py:172) | When `allowed_entry_regimes` is non-empty, flicker out of an allowed label blocks entries at the flicker rate. Currently disabled per WS2.5 §8. |
| 7 | Executor regime stamp | [core/engine.py:310-311](src/core/engine.py:310), `executor.set_regime_label` | Used by paper/shadow executors for cost-attribution metadata. Flicker is harmless — stamps are resolution-time metadata, not trade logic. |
| 8 | Decision log regime field | [replay/decision_log.py:75-89](src/replay/decision_log.py:75) | Downstream analytics; flicker is already present in the log and measurable (this audit's primary data source). |

**Observation**: (1) is the only path that perturbs the *live confidence formula*, and it does so through a pure multiplier on `bps_per_unit_pressure`. (2)–(5) are calibrator-pipeline concerns; they reduce *future* signal quality but are decoupled from the current tick's decision. (6) is gated-off today.

---

## 5. Verdict and recommendations

### 5.1 Verdict

- **Flicker is real, but not uniformly harmful.** The heavy-tailed dwell distribution (21% of ticks in short runs, 79% in long runs) means most of operating time is label-stable. When flicker happens, it's in predictable near-threshold bands, and the top-5 transitions are all bidirectional-symmetric pairs.
- **Most acute damage path is calibrator cube dilution.** Live-only, affects the loser-harvest mechanism, delays by a factor proportional to the per-cell short-run sample fraction (~21% of samples attributed to short-run labels in the sprint tape — almost certainly higher for live cube cells that sit near thresholds for a given tribe's preferred regime).
- **Confidence formula is multiplier-modulated, not structurally perturbed.** The ±40% regime_multiplier swing flickers the *magnitude* of signal_quality but not the structural 0.25 cap hypothesis. H1 should not be gated on WS4.
- **WS2.5 gate would be flicker-fragile.** The near-27-per-1000-ticks trend↔normal transition rate means roughly one flicker every ~37 ticks (~37 s in live cadence) in the trend regime; a naive gate would close during each. Pre-enabling the gate in a future WS2.5 iteration will require addressing this.

### 5.2 Recommendations

1. **Live-tape companion measurement (post-H1.I2, when forecast archive carries regime).** Two numbers decide whether WS4 intervention is worth the effort:
    - **Transition rate**: compute on a 2026-04-18/-19/-21 live day (NOT 2026-04-20, which is the H1 measurement tape). Compare to the sprint baseline's 75/1000 ticks. If live rate is < 50/1000, WS4 is lower priority; if > 150/1000, escalate.
    - **Near-threshold dwell density**: histogram of `drift_eff` at ticks that immediately preceded a transition. If the grey-band density is flat, flicker is "everywhere"; if it concentrates tightly at the thresholds, hysteresis cleanly eliminates it.
2. **Proposal space for intervention (not yet actioned):**
    - **A. Schmitt thresholds** — separate enter/exit thresholds per band. E.g., enter TREND at `drift_eff ≥ 0.55`, exit only when `drift_eff ≤ 0.45`. Cheapest, purely local to `classifier.py`, backward-compatible (when enter = exit, identical to today). Natural default.
    - **B. Minimum dwell time** — hold a label for at least `min_dwell_s` seconds after adoption. Stateful, requires a small in-classifier state machine. Harder to reason about interactions with sprint/live determinism.
    - **C. Probabilistic label + soft weighting** — emit `P(regime)` and weight calibrator sample contributions by probability. High-accuracy theoretically, large code change, interacts with every consumer on the blast-radius map.
    - Recommended: **A** first. Zero state, minimal code delta, measurable before/after.
3. **Cascade-up/down are NOT flicker candidates** (0.7% + 0.3% = 1.0% of ticks). Cascade threshold bands are liquidation-driven and discrete; hysteresis there buys nothing. Intervention (if any) should exclude cascade.
4. **Do not gate H1 on WS4.** H1's hypothesis space (options A/B/C) is evaluated on the same tape for all three; a constant-per-tape flicker rate biases all three identically. The 0.25 confidence cap is structural, not regime-dependent; flicker modulates magnitudes, not the cap.
5. **Do not re-enable WS2.5 gate until Schmitt-style hysteresis lands.** The current gate design would close on every trend↔normal flicker. This is not new information (WS2.5 §8 already ships disabled), but WS4 data sharpens the reason.

### 5.3 Priority recommendation

**Post-H1 / pre-H3, Q-slot adjacent to WS5.** If H1 lands a fix (any of A/B/C), the confidence-formula re-audit will re-measure signal_quality distributions — those distributions are regime_multiplier-modulated, so WS4 flicker quantification becomes a useful cross-reference for interpreting the H1 results. If H1 nulls, WS4 moves up: regime_multiplier flicker may be one of the magnitude-dampening mechanisms the H1 hypothesis missed.

### 5.4 Scope discipline note

Per `WS3_H1_SCOPE.md` §11, this audit made zero code changes — it is a pure measurement pass on already-archived sprint data. No auditor schema change, no regime-labeller change, no confidence-formula change. The follow-up live-tape measurement (§5.2.1) will also be read-only; the Schmitt-threshold prototype in §5.2.2-A is the first action item that would perturb the label sequence, and it is **deferred to a post-H1 WS4 implementation chip**, not part of this audit.

---

*Filed in service of: WS4 Q-slot (PLAN §Q4) scoping, WS2.5 gate re-enablement prerequisite documentation, H1 companion measurement for regime_multiplier contamination of signal_quality magnitude.*
