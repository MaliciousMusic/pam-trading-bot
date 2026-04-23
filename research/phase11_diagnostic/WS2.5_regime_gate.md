# WS2.5 — Regime-Entry Gate

**Date:** 2026-04-22
**Branch:** `feat/tensor-engine`
**Gate implementation commit:** `5b2faac feat(decision): WS2.5 regime-entry gate`
**Tape:** `data/events/2026-04-20.jsonl` (synth_rate=60, tick_seconds=1, 86 399 ticks)

> **Measurement caveat (added 2026-04-23).** Findings in this artifact
> derived from the `SprintEngine` replay harness, not the live-tape
> forecasts archive. Per `MEASUREMENT_INFRA.md`, the sprint
> microstructure produces ~400× weaker per-tick net_pressure than the
> live Binance BTCUSDT tape, and magnitude-channel transfer to
> production is not quantitatively validated. The cross-seed control
> (§6) bounds RNG variance within the sprint tape; it does not
> establish sprint→live transfer. Directional findings (trend-only
> regime's +2.08 bps improvement sign, cascade's harm) and relative
> A/B variant deltas remain informative; absolute bps-magnitude
> numbers should be re-measured against `data/forecasts/<day>.jsonl`
> before they set acceptance gates.

## 1. Summary

On day 2026-04-20 the trend-only gate (v2) roughly halves daily equity
bleed (−4.70 % → −2.19 %) and improves per-trade edge (−10.04 bps → −7.96
bps, +2.08 bps) at the cost of 42 % of the trade flow. Cross-seed validation
on {42, 123, 777, 2024} shows the per-trade improvement is **structural**,
not a lucky-seed artifact: mean realized bps under v2 is −8.18 ± 0.17 across
seeds, and baseline's −10.04 sits ~4σ outside that distribution.

Caveats:

- **The council still loses money in every variant.** The gate is a
  bleed-reduction tool, not an alpha-generator. A council with ≤-friction
  edge everywhere stays unprofitable even after filtering to its least-bad
  regime.
- **`high_conf_override=0.4` is a no-op on this council.** Max observed
  confidence = 0.2483 over 86 399 ticks; the override variant (v4) is
  byte-identical to vanilla trend-only (v2).
- **v3 (trend + cascade) is strictly worse than v2.** Cascade regimes
  re-admit trades with the same edge as baseline, so including them
  undoes most of the gate's benefit.
- **Single-day evidence.** Cross-seed controls for RNG, not for tape.
  See §7.

Recommended default: **leave the gate disabled
(`allowed_entry_regimes: []`) until a multi-day validation confirms the
trend-pocket edge isn't an artifact of 2026-04-20's regime mix.** Ship the
plumbing so it can be flipped on cheaply once that evidence exists.

## 2. Variants

| id | `allowed_entry_regimes` | `high_conf_override` | intent |
|----|---|---:|---|
| v1_baseline | `[]` (no gate) | 1.0 | control |
| v2_trend_only | `["trend_up", "trend_down"]` | 1.0 | WS1-aligned pocket |
| v3_trend_cascade | trend + `["cascade_up", "cascade_down"]` | 1.0 | wider pocket |
| v4_trend_high_conf | trend only | 0.4 | escape hatch for high-conf outliers |

All four share seed=42, identical tape, identical tribe config, identical
executor settings. Only the `decision.*` block is patched per variant, via
an in-process dict mutation (see `scripts/ws2_5_run_variants.py`); the
yaml on disk is never edited.

## 3. Seed 42 results

| variant                | trades | win%  | mean bps| med bps | sharpe | maxDD  | d_equity USD| d_eq %  |
|------------------------|-------:|------:|--------:|--------:|-------:|-------:|------------:|--------:|
| v1_baseline            |   236  | 12.7% |  -10.04 |  -10.33 | -15.76 |  4.70% | $ -2350.10 | -4.700% |
| v2_trend_only          |   138  | 19.6% |   -7.96 |   -9.68 |  -9.39 |  2.19% | $ -1093.63 | -2.187% |
| v3_trend_cascade       |   184  | 16.3% |  -10.18 |  -10.94 | -12.89 |  3.65% | $ -1825.55 | -3.651% |
| v4_trend_high_conf     |   138  | 19.6% |   -7.96 |   -9.68 |  -9.39 |  2.19% | $ -1093.63 | -2.187% |

(Start equity = $50 000 in all runs. Sharpe here is per-trade:
`mean/stdev × sqrt(N)` on realized_bps; with ~10 bps of per-trade noise and
~150–240 trades, Sharpe magnitudes are large because the loss signal is
*consistent*, not because the equity curve is jagged.)

## 4. Interpretation

### 4.1 v2 ≡ v4 — the override knob never fires

v2 and v4 are byte-identical (same trades, same per-trade realized bps, same
equity delta). Reason: council confidence never crosses 0.4 on this tape.
Distribution of `forecast.confidence` over 86 399 baseline ticks:

- max confidence:          **0.2483**
- ticks with conf ≥ 0.10:  236   (these are the exact entry triggers)
- ticks with conf ≥ 0.20:  15
- ticks with conf ≥ 0.40:  **0**

So `high_conf_override=0.4` is unreachable. Any override threshold ≥ 0.25 is
operationally equivalent to "strict gate" (`override=1.0`). The override
variant is therefore indistinguishable from v2 on this tape, and we do not
have evidence to recommend a 0.4 cutoff — that would require a council that
can actually emit high-confidence signals, which this one can't (WS1
diagnosis B: weak signal).

### 4.2 Where the gate kicks in (hold-reason distribution, v2)

- `council` (normal flat / below-min-conf / council-flat): 86 163 ticks
- `regime gated`: **98 ticks** — this is where the 236→138 trade gap comes from
- On entry side: 138 trades fired, 98 would-be entries suppressed = 41.5 %
  reduction, which matches the headline trade-count delta (236−138 = 98).

### 4.3 Cascade is actively harmful

v3 adds cascade_{up,down} to the allow-list. Result: trade count rises from
138 → 184 (46 re-admitted trades), but mean bps drops from −7.96 → −10.18 and
equity delta worsens from −2.19 % → −3.65 %. The re-admitted cascade trades
have worse edge than the trend-only subset they're added to. Reading:
cascade regimes in WS1's attribution were already the worst pocket; the gate
variant bears this out on live entries, not just counterfactual exit
windows.

### 4.4 Regime time-shares on 2026-04-20

| label        | ticks  | share |
|--------------|-------:|------:|
| chop         | 51 000 |  59.0% |
| normal       | 12 126 |  14.0% |
| trend_up     | 12 120 |  14.0% |
| trend_down   | 10 332 |  12.0% |
| cascade_up   |    558 |   0.6% |
| cascade_down |    263 |   0.3% |

Trend covers ~26 % of the day. The gate is rejecting entries during ~73 %
of calendar time but only ~42 % of *would-be* entries, which implies the
council's entry impulses are already somewhat concentrated in trend
windows (makes sense — directional agreement is harder to produce when
price is range-bound).

## 5. Trade-count vs edge-per-trade trade-off

Total realized-bps bleed per unit equity (rough back-of-envelope):

| variant | trades × mean-bps | total loss (bps) |
|---|---:|---:|
| v1 | 236 × −10.04 | **−2 369.4** |
| v2 | 138 × −7.96  | **−1 098.5** |
| v3 | 184 × −10.18 | **−1 873.1** |

v2 delivers a **−53.6 %** reduction in total bleed vs v1, decomposing as
−41.5 % from trade-count reduction and −20.7 % from per-trade edge
improvement (these compound). v3 saves only −20.9 %, almost entirely from
trade-count; its per-trade edge is statistically indistinguishable from v1.

The trade-count cost matters for tournament purposes: v2's 138 trades on a
single day are enough for per-trade stats (stdev_bps = 9.96, so the standard
error on mean bps is ~0.85 bps, i.e. the −7.96 vs −10.04 gap is a plausible
≥1-sigma move already at one seed). It's not a data-starvation problem.

## 6. Cross-seed validation (v2_trend_only)

Four seeds on the same tape (day=2026-04-20). Seed changes only the
RNG for tribe vote noise and projector MC paths; the bar stream is
identical.

| variant                  | trades | win%  | mean bps| med bps | sharpe | maxDD  | d_equity USD| d_eq %  |
|--------------------------|-------:|------:|--------:|--------:|-------:|-------:|------------:|--------:|
| v2_trend_only@seed42     |   138  | 19.6% |  -7.96  |  -9.68  |  -9.39 |  2.19% | $ -1093.63 | -2.187% |
| v2_trend_only@seed123    |   113  | 16.8% |  -8.43  | -10.00  |  -9.21 |  1.84% | $  -919.36 | -1.839% |
| v2_trend_only@seed777    |   153  | 15.0% |  -8.24  |  -9.93  | -11.08 |  2.65% | $ -1323.20 | -2.646% |
| v2_trend_only@seed2024   |   139  | 17.3% |  -8.11  |  -9.96  |  -9.69 |  2.43% | $ -1217.31 | -2.435% |

Rollup (mean ± std across seeds):

| metric | mean | std | min | max |
|---|---:|---:|---:|---:|
| trades | 135.75 | 14.41 | 113 | 153 |
| win_rate | 0.172 | 0.016 | 0.150 | 0.196 |
| mean_realized_bps | **−8.18** | **0.17** | −8.43 | −7.96 |
| sharpe_per_trade | −9.84 | 0.73 | −11.08 | −9.21 |
| max_drawdown_pct | 2.28 | 0.30 | 1.84 | 2.65 |
| equity_delta_pct | −2.28 | 0.30 | −2.65 | −1.84 |

**Interpretation.** The edge-per-trade is remarkably stable across RNG
seeds: σ(mean_realized_bps) = 0.17 bps vs. the v1→v2 headline improvement of
+2.08 bps, i.e. the gate's effect is ~12σ above seed noise. Trade count
varies more (113–153 across seeds, σ = 14), driven by tribe-vote jitter
pushing borderline conf scores above/below `min_conf=0.05`.

v1 baseline's seed-42 mean of −10.04 bps sits **4σ outside** the
v2 cross-seed distribution of mean bps — strong evidence that the
per-trade bleed reduction is a structural effect of regime filtering,
not a lucky seed on 2026-04-20. This resolves the "is it RNG?" question;
it does **not** resolve the "is it day-specific?" question (see §7).

## 7. Overfitting / generalization risk

**Flagging this loudly:** the trend pocket identified in WS1 and re-confirmed
here comes from a **single day** (2026-04-20). The mechanisms that could make
this a day-specific artifact, not a robust edge:

- **Regime distribution is tape-dependent.** 2026-04-20 was 26 % trend. Days
  with no sustained trend will have few or zero entries under v2 — the gate
  produces equity-flat, not profit.
- **Backward-looking classifier.** `RegimeClassifier` labels the *current*
  tick based on recent returns/vol. The trend label means "the last N minutes
  were trending," not "the next M minutes will." Any forward-looking
  persistence is empirical, not structural.
- **Selection on the same data the hypothesis came from.** WS1 used
  2026-04-20 to find the trend pocket. WS2.5 uses 2026-04-20 to measure the
  gate's effect. Cross-seed validation here is cross-RNG, not cross-tape.
  This is mild double-dipping; the only real test is holdout days.
- **Council confidence cap (0.248).** Any pocket-finding the council can do
  is bounded by how well-calibrated its low-conf emissions are. WS1 already
  diagnosed this as "weak signal." The gate exploits a pocket *in that weak
  signal*; if the signal decays further (e.g., during a distribution shift),
  the gate has less to work with.

**Minimum bar before productionizing the gate:** apply v1 vs v2 on at least
three additional full days spanning different regime mixes (e.g.,
2026-04-18, 04-19, 04-21 — all available under `data/events/`). The gate
should reduce bleed on *every* day, not just on average. If even one day
shows a materially worse v2, the pocket is day-specific and the gate can't
be trusted in isolation.

## 8. Proposal — default config

Keep the gate present but **disabled** until multi-day validation lands:

```yaml
decision:
  allowed_entry_regimes: []      # empty = no gate (default, current behavior)
  high_conf_override: 1.0        # strict when gate is active
```

Do **not** adopt `high_conf_override: 0.4` as a default — it's a no-op on
today's council and we have no data to recommend any specific value for a
future higher-conf council.

When multi-day validation confirms the pocket on holdout days, flip to:

```yaml
decision:
  allowed_entry_regimes: ["trend_up", "trend_down"]
  high_conf_override: 1.0
```

Do **not** include cascade regimes in the allow-list — seed-42 evidence is
that they strictly worsen the per-trade edge without meaningful trade-count
recovery.

## 9. Open questions → next steps

1. **Multi-day validation.** Run v1 vs v2 on 2026-04-18, -19, -21 (and any
   new days as they're recorded). Decision rule: adopt the gate iff v2
   reduces equity bleed on *every* day in the holdout set.
2. **Does the council have a *positive*-edge pocket at all?** Every variant
   here loses money. WS1 established council edge is ≤ friction; WS2.5
   confirms no subset of labels makes it positive. The gate is defensive;
   WS2+ still needs to find offensive edge (confidence calibration, vote
   weighting, or orthogonal inputs — see the council-redundancy finding
   from 2026-04-18).
3. **Confidence-cap root-cause.** Why does max conf = 0.248? Is this a
   ceiling from vote aggregation (`aggregator/voting.py`) or from the
   projector's `confidence_decay`? If the cap is a bug, all "high-conf
   override" logic is moot. Worth a short dedicated diagnostic.

## Appendix — reproduction

```bash
# 4 variants seed 42 (parallel, ~2.5 h wall clock)
PYTHONPATH=. python -m scripts.ws2_5_run_variants --day 2026-04-20 --seed 42

# metrics
PYTHONPATH=. python -m scripts.ws2_5_analyze --seed 42

# cross-seed for winner
PYTHONPATH=. python -m scripts.ws2_5_cross_seed \
    --day 2026-04-20 --variant v2_trend_only --seeds 123 777 2024
PYTHONPATH=. python -m scripts.ws2_5_cross_seed_analyze --variant v2_trend_only
```

Determinism gate: `scripts/verify_determinism.py --day 2026-04-20 --max-ticks 500`
passes byte-identical with the gate present (empty-list default is a
no-op). Any future change that touches the DecisionEngine must re-pass
this before merge.
