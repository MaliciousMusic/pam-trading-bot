# WS1 — Exit-Reason Attribution Under Merciless

**Phase 11 Diagnostic · Load-bearing master measurement**

| Item          | Value                                                           |
|:--------------|:----------------------------------------------------------------|
| An-0 day      | 2026-04-20                                                      |
| Seed          | 42                                                              |
| Execution     | Merciless (adversarial slip + fat-tail latency + sqrt-slippage) |
| Cadence       | 1 Hz heartbeat (Phase 2.5 normalized horizon)                   |
| Max ticks     | None (full day, 86,399 ticks simulated)                         |
| Sample        | 236 closed positions (236 opens, 236 closes — flat at end)      |
| Run dir       | `data/replays/ws1_merciless_seed42/variants/full_day/`          |
| Analysis tool | `scripts/ws1_exit_attribution.py`                               |

## 0. Headline

**Diagnosis: (B) signal is weak — exits are not the binding constraint.**

The aggregate directional hit rate over a 20-second hold is **44.5% (N=236)** — *below* the
50% random baseline. Mean mark-to-mark move in the predicted direction is **−0.21 bps**.
Holding longer only makes things worse: at 60 s the mean drops to −2.97 bps, at 120 s
to −4.97 bps. At no hold horizon is the signal’s frictionless expectation above zero.

A single regime pocket (`trend_up`, 29% of trades) does have real persistent edge
(+3.50 bps @ 20 s rising to +7.48 bps @ 60 s). But its contribution is
swamped by negative-edge trades in every other regime.

**Therefore WS6 (exit retuning) is a trap.** The exit layer is correctly terminating
positions whose forecast has decayed below friction; the problem is that the forecast
was not carrying real edge to begin with, outside of `trend_up`. WS3 (signal improvement)
or an earlier gate (don’t open outside `trend_up` + a handful of adjacent regimes) is
the right lever.

## 1. Exit-reason distribution

100% of closes fire under a single exit rule:

| reason               |   N |    % | realized_bps mean | median |  p25 |  p75 | hold_s | wins | win_rate |
|:---------------------|----:|-----:|------------------:|-------:|-----:|-----:|-------:|-----:|---------:|
| `edge_exhausted`     | 236 |100.0 |            −10.04 | −10.33 |−16.2 | −5.2 |   20.0 |   30 |   12.7 % |

Every position exits at **exactly held_seconds = 20.0** with reason `edge exhausted
remaining=+X.Xbps < fees×1.00=8.0bps`. This is a hard artefact of the current exit
config at `config/settings.yaml:177-184`:

```
edge_exhausted_factor: 1.0
edge_exhausted_min_age_s: 20
```

Layer 3b checks *every tick ≥ 20 s old* whether the projector’s remaining forecasted
move is below `round_trip_fees_bps × 1.0` (≈ 8 bps under Merciless). The first check
fires at t = 20 s; in 100 % of positions the remaining-edge estimate is already
below that threshold, so the position is closed immediately.

No trade ever hit hard-SL, strong-flip, weak-flip, ratchet-retrace, forecast-target-TP
or past-peak shape-aware exits under these conditions.

### 1a. Edge-exhausted “remaining edge” distribution at the 20 s checkpoint

| stat              | value                     |
|:------------------|:--------------------------|
| mean              | **+0.90 bps**             |
| median            | +0.30 bps                 |
| p10 / p90         | −0.10 / +2.50 bps         |
| fraction ≤ 0      | **26.7 %** (forecast has already flipped to reverse by 20 s) |

Context: the friction threshold is ≈ 8 bps. The projector is reporting that after
20 s of holding, the median position has ≤ 0.3 bps of remaining directional edge,
and in over a quarter of cases the remaining forecast sign has inverted.

## 2. Exit-reason × regime label

Regime label is attached by cross-joining each close with the nearest-timestamp
decision record (ledger close events do not carry `regime_label` — this is an
instrumentation gap, see §8).

| regime        |   N | realized_bps μ | note                                      |
|:--------------|----:|---------------:|:------------------------------------------|
| `trend_up`    |  68 |          −6.3 | least-damaging regime                     |
| `trend_down`  |  55 |         −11.3 |                                           |
| `cascade_up`  |  46 |          −9.8 | counter-trend loss regime                 |
| `normal`      |  32 |         −12.2 |                                           |
| `chop`        |  27 |         −13.0 |                                           |
| `cascade_down`|   8 |         −16.1 |                                           |
| **all**       | 236 |         −10.0 |                                           |

Only one regime (`trend_up`) avoids the −10 bps cluster.

## 3. Exit-reason × hold-bucket (peak_favorable_s proxy)

All 236 closes fall in the `short ≤ 60 s` bucket at exactly 20 s. Medium and long
buckets are empty. The cross-tab is therefore degenerate: no variation in hold
length to correlate with forecast peak.

**Instrumentation gap (flagged for Phase 11):** `decisions.jsonl` persists
`expected_move_bps` and `confidence` but not `forecast.steps_bps`, so
`peak_favorable_s` cannot be recovered post-hoc. `_entry_meta` in
`src/execution/paper.py:962-999` also does not stamp it onto the open event. Close
this gap before rerunning WS1 under any alternate exit config, otherwise a meaningful
hold-bucket cross-tab remains impossible.

## 4. Counterfactual — what if the exit didn’t fire at 20 s?

For every closed position, we look up the price from `decisions.jsonl` at t_open +
{20, 60, 120, 180, 250} s and recompute the mark-to-mark move in the position’s
predicted direction. Friction is assumed constant at the ≈ 8 bps round-trip cost.

| hold    |   N | direction-hit | mean mark-move | median | p25    | p75    | net vs 8 bps fees |
|:--------|----:|--------------:|---------------:|-------:|-------:|-------:|------------------:|
|  20 s   | 236 |       44.5 %  |   **−0.21 bps**| −0.51  | −6.33  | +4.83  |           −8.21   |
|  60 s   | 236 |       45.8 %  |    −2.97 bps   | −2.23  | −14.65 | +10.40 |          −10.97   |
| 120 s   | 236 |       31.4 %  |    −4.97 bps   | −4.99  | −13.44 | +2.22  |          −12.97   |
| 180 s   | 236 |       39.0 %  |    −4.68 bps   | −6.60  | −15.73 | +9.62  |          −12.68   |
| 250 s   | 236 |       40.7 %  |    −3.76 bps   | −4.40  | −13.80 | +5.42  |          −11.76   |

Mean frictionless edge is **negative at every horizon**. Direction-hit rate is
**below 50 % at every horizon**. The 120 s bucket is the worst — 31.4 % — consistent
with mean-reversion dominating beyond the council’s nominal projection horizon.

Rule: if `mean mark-move` grew monotonically with hold, the exit would be truncating
real edge (diagnosis A). Here it *shrinks* monotonically (−0.21 → −2.97 → −4.97),
which is the signature of (B).

## 5. Regime-conditional counterfactual (the one place (A) still applies)

| regime        |   N | μ @ 20 s |  @ 60 s |  @ 250 s | skill persists? |
|:--------------|----:|---------:|--------:|---------:|:----------------|
| `trend_up`    |  68 |  **+3.50** | **+7.48** |  **+5.01** | **YES — rises then fades slowly** |
| `cascade_down`|   8 |   −6.24  |  −2.05  |   +7.52  | Rises but N=8       |
| `normal`      |  32 |   −2.43  |  −1.83  |   −5.70  | Partial             |
| `trend_down`  |  55 |   −1.49  |  −2.33  |   −7.66  | NO — reverts        |
| `cascade_up`  |  46 |   +0.20  | −18.97  |   −9.28  | NO — catastrophic reversion |
| `chop`        |  27 |   −3.18  |  −4.96  |   −9.50  | NO — reverts        |

`trend_up` (29 % of trades) is the single regime where **both** the 20 s exit is
truncating a real signal and holding longer would recover more alpha. In that
regime @ 60 s, frictionless mark-move mean is **+7.48 bps** — within sight of the
8 bps friction floor, implying a marginally profitable 60 s hold.

In `cascade_up` the picture is the inverse: the position looks benign at 20 s
(+0.20 bps) but the price runs 18.97 bps *against* it over the next 40 s. Holding
longer here would be disastrous.

## 6. Council signal strength

| metric                                   | value                   |
|:-----------------------------------------|:------------------------|
| Mark→mark directional hit (frictionless) | **105 / 236 = 44.5 %**  |
| Mean mark-move in predicted direction    | **−0.21 bps**           |
| Median mark-move                         | −0.51 bps               |
| Mean realized_net (post-friction)        | −10.04 bps              |
| Median realized_net                      | −10.33 bps              |

Slicing by council `|expected_move_bps_raw|` at entry:

| bucket             |   N | mark-hit | mark μ bps | net μ bps | conf μ |
|:-------------------|----:|---------:|-----------:|----------:|-------:|
| strong ≥ 10 bps    |   0 |       —  |         —  |        —  |     —  |
| moderate 5-10 bps  |   0 |       —  |         —  |        —  |     —  |
| mild 1-5 bps       | 107 |   44.9 % |    +0.35   |   −9.53   |   0.15 |
| trivial < 1 bps    | 129 |   44.2 % |    −0.67   |  −10.46   |   0.13 |

**The council never produced a forecast with raw magnitude ≥ 5 bps over the entire
24 h day, and never produced confidence > 0.25.** Every single trade opened under
low conviction with raw projected magnitude < 5 bps. This on its own is a quality
signal collapse — see WS3.

## 7. Per-horizon MAE & directional hit (ForecastAuditor)

Pulled from `data/forecasts/2026-04-20.jsonl` (pre-Phase-2.5 live-session data;
`tick_seconds=5` schema, so `label="15s"` is actually a 5 s horizon, `label="1m"`
is 60 s; 5 m / 10 m slots are not populated because step index exceeded the
projector length under the prior cadence).

| label | horizon_s |     N | MAE bps | median abs err | directional hit | \|expected\| μ | \|realized\| μ | conf μ |
|:------|----------:|------:|--------:|---------------:|----------------:|--------------:|--------------:|-------:|
| `15s` |     5     | 10753 |   1.29  |    0.74        |   **43.72 %**   |    0.12       |    1.22       | 0.011  |
| `1m`  |    60     | 10636 |   4.34  |    3.13        |   **49.68 %**   |    0.64       |    4.21       | 0.011  |

Key facts:

* 5 s horizon directional hit **is below 50 %** — the short-horizon projector is
  anti-informative.
* 60 s horizon is a coin flip.
* `|expected|` µ ≪ `|realized|` µ at both horizons. The projector emits forecasts
  whose magnitude is ~10× smaller than the realized move — the model is
  massively under-confident (`conf µ ≈ 0.011`).
* These numbers come from a different execution mode (pre-Phase-2.5 Gentle
  session), so they are corroborating not primary evidence. But they agree with
  the sprint: council has essentially no directional skill at 5-60 s horizons.

## 8. Instrumentation gaps surfaced by WS1 (carry into Phase 11)

1. **`regime_label` is not on close events.** Cross-joining with `decisions.jsonl`
   adds an O(N log N) post-processing step. Stamp it at close time in
   `src/execution/paper.py:1079-1091`.
2. **`peak_favorable_s` is not recoverable from disk.** `_entry_meta` in the
   executor should snapshot `forecast.steps_bps` so later analysis can compute
   argmax / argmin without re-simulating. Alternatively persist the signed curve
   on the open record.
3. **`SprintEngine` does not wire `ForecastAuditor`.** Any sprint-based WS1
   rerun must either accept pre-Phase-2.5 historical anchor data or bolt the
   auditor onto `src/replay/harness.py:134-186`.
4. **Projector label/horizon mismatch at 1 Hz tick.** With `tick_seconds = 1`
   and `projector_steps = 250`, the `("5m", 300/tick_s)` and `("10m", 600/tick_s)`
   anchor specs in `src/core/engine.py:92-97` exceed the projector length and
   are silently dropped.

## 9. Reflection — the four questions

**Q1. Of all closed positions across a full An-0 day under Merciless, what is
the distribution of exit reasons, and how does realized PnL differ by exit
reason?**

Single degenerate distribution: 236 / 236 closes (100 %) fire on
`edge_exhausted` at exactly 20 s. Realized µ = −10.04 bps; median −10.33 bps;
p25 / p75 = −16.2 / −5.2 bps; win-rate 12.7 %. Because only one exit fires, no
cross-reason PnL comparison is possible within the current config.

**Q2. Do exits correlate with regime label?**

Because only one exit fires, the correlation is between `edge_exhausted` and
regime label via realized_bps µ per regime. Best (least negative):
`trend_up` −6.3 bps. Worst: `cascade_down` −16.1 bps, `chop` −13.0 bps,
`normal` −12.2 bps. The 10-bps-wide spread *between regimes* is larger than the
spread within any single regime, which argues for a regime-aware entry gate
before fiddling with exits.

**Q3. Do exits correlate with the forecast-peak horizon (peak_favorable_s
bucket)?**

Not directly measurable — all 236 holds are exactly 20 s and `peak_favorable_s`
is not persisted. The counterfactual in §4 is the best available substitute:
mean mark-move is negative at every hold horizon we can reconstruct from
`decisions.jsonl` prices. So even if `peak_favorable_s` were known,
the empirical post-exit price path says the signal has no positive forward edge
to capture.

**Q4. Based on the data, state which of the two diagnoses holds: (A) signal is
real, exits truncate it OR (B) signal is weak, exits are a red herring. Quote
the specific numbers that force the conclusion.**

**Diagnosis (B) — signal is weak.** The numbers:

* Mark→mark directional hit over 20 s: **44.5 % (105 / 236)**, below the 50 %
  random baseline.
* Mean mark-move in predicted direction @ 20 s: **−0.21 bps**.
* Same mean at extended horizons: **−2.97 bps @ 60 s, −4.97 bps @ 120 s,
  −3.76 bps @ 250 s**. Holding longer does not recover any edge.
* Council never produced raw `|expected_move_bps|` ≥ 5 bps across 236 opens and
  never emitted confidence > 0.25.
* Historical 5 s auditor directional hit: **43.72 % (N = 10 753)**.
* At the 20 s exit, 26.7 % of positions already have a *reverse-signed*
  remaining forecast.

The one partial refinement: in `trend_up` (68 / 236 = 29 % of opens) the signal
*is* real and *does* persist — mark-move rises from +3.50 to +7.48 bps between
20 s and 60 s. But this minority is overwhelmed by negative-edge trades in the
other 71 % of the sample, and even +7.48 bps @ 60 s falls short of the 8 bps
friction floor.

**Implication for the Phase 11 plan:**

* **WS6 (exit retuning) remains a trap as currently framed.** No tweak of
  `edge_exhausted_factor`, `ratchet_arm_bps`, `shape_grace_seconds`, etc. changes
  the aggregate signal quality; they only shuffle when the already-negative-edge
  position is closed.
* **WS3 (signal improvement) is the critical path.** Council confidence is
  capped below 0.25 and expected-move magnitude below 5 bps. Any Phase 11 work
  should start by understanding why the projector can’t produce actionable
  forecast magnitudes.
* **Consider a regime gate before WS3.** `trend_up` is the only regime with
  positive frictionless edge. A gate that suppresses opens outside
  `{trend_up, cascade_down?}` would convert roughly 2/3 of the day’s −10 bps
  trades into non-trades, preserving the 29 % subset where the signal is real.
  This is an adjacent WS (likely a WS2/WS3 hybrid) and should be scoped before
  any exit-layer work.
* **Close the instrumentation gaps in §8** before any WS1 rerun under alternate
  exit configs, otherwise the same analysis costs another post-hoc cross-join.

---

*Generated by `scripts/ws1_exit_attribution.py` on 2026-04-21. Raw tables in
`research/phase11_diagnostic/_ws1_tables.md`.*
