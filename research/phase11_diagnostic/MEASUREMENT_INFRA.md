# Phase 11 — Measurement Infrastructure

**Date:** 2026-04-23
**Scope:** Governs which tapes Phase 11 hypothesis-tests read against, and
records a measurement-infrastructure finding surfaced by WS3 H2 Step 4
(live-feedback attribution).

## 0. TL;DR

The sprint harness — `SprintEngine` + `CachedHistoricalClient` bars +
synthetic trade stream — produces materially weaker microstructure
pressure than the live Binance BTCUSDT tape consumed by the production
`TradingEngine`. **Measurement derived from sprint replays does not
quantitatively transfer to live trading.** Phase 11 adopts the operating
rule that WS3 H-acceptance bars are measured against the live-tape
forecasts archive (`data/forecasts/<day>.jsonl`); the sprint harness is
retained for determinism verification, regression testing, and code-
change attribution only.

This document records the empirical finding, the resulting operating
rule, retroactive caveats applied to prior Phase 11 artifacts, a
decision point on `MAX_GAIN`, and a catalog of follow-up work to
close the sprint↔live gap (post-Phase-11 scope).

## 1. Empirical sprint-vs-live delta

Source: WS3 H2 Step 4 attribution
(`research/phase11_diagnostic/_ws3_h2_snapshots/option1a_live-feedback_20260423T055545Z.md`
compared to the pre-wiring offline snapshot
`option1a_20260422T160057Z.md`). Both snapshots replay identical
calibrator simulation code; they differ only in the tape they consume.

**Offline column** — `data/forecasts/<day>.jsonl`, the resolved-anchor
archive written by the live `TradingEngine` against the Binance
BTCUSDT tape (production calibrator active throughout, persisted
gain 1.57).

**Live-feedback column** — sprint-produced anchors from full-day
`SprintEngine` runs against cached kline bars, synth-60 trade
stream, synth order book. Calibrator wired per feat `686164b`;
starts cold at gain=8.0 each run.

### 1.1 Raw-data distribution (aggregate across panel)

| metric                       | live tape (offline) | sprint tape (live-fb) | ratio |
|:-----------------------------|--------------------:|----------------------:|------:|
| `|expected_move_bps|` median |               0.143 |                 0.009 |   16× smaller in sprint |
| `|expected_move_bps|` p90    |               3.540 |                 0.335 |   10.6× smaller |
| `|realized_move_bps|` median |               1.704 |                 0.392 |   4.4× smaller |
| `|realized_move_bps|` p90    |               8.446 |                 6.478 |   1.3× smaller |
| anchors total (4-day panel)  |              61,290 |               686,263 |   11× more in sprint |
| anchors kept ≥ MIN_PRED_BPS  |      20,367 (33 %)  |         34,888 (5 %)  |   sprint fraction collapses |
| label horizons present       |  15s, 1m, 5m, 10m   |         15s, 1m only  |   sprint projector horizon limit |

Reading the table:

- **Expected-move collapse is multiplicative.** Sprint `|emove|` p50
  is 16× smaller than live; the calibrator compensates by walking gain
  to the `MAX_GAIN=40.0` ceiling on every panel day and then still
  asks for raw 4× corrections at end-of-day. The sprint loop is
  ceiling-bound.
- **Realized-move collapse is milder (4.4×) but still material.** The
  sprint bars *are* the real 1-minute kline open/high/low/close, so
  realized 1m/15s returns should match live one-to-one. The 4.4×
  collapse indicates the realized measurement is being taken at
  smaller intervals or over microstructure the sprint synth book
  doesn't reproduce — worth chasing if the gap becomes a blocker.
- **Anchor count inflates by 11× in sprint** while the kept-fraction
  drops from 33 % to 5 %. Interpretation: the projector emits many
  low-confidence micro-forecasts per tick in sprint (synth noise is
  different), but most have `|emove| < MIN_PRED_BPS=0.5` and get
  filtered out. The 5 % kept fraction is the scale-bearing tail.
- **Label mix narrows.** Live tape produces 15s/1m/5m/10m anchors;
  sprint produces 15s/1m only. The chess projector's per-step
  horizon in sprint doesn't reach the 5m/10m anchor specs. WS1 §7
  per-horizon MAE tables are not reproducible in sprint at 5m/10m.

### 1.2 Pressure back-solve

Under the linear projection model
`emove_bps ≈ gain × net_pressure`, median net_pressure in each tape:

- **Live tape:** `0.143 / 1.57 = 0.091` (gain is production's
  persisted value)
- **Sprint tape (end-state, gain saturated at 40.0):**
  `0.009 / 40.0 = 0.000225`

**Per-tick net_pressure is ~400× smaller in sprint than in live.**
This is the dominant finding. Whatever the tribe-consensus-to-pressure
mapping does on the live Binance tape, it does not do on the sprint
synth book. Downstream consequences:

- Calibrator-ceiling saturation in sprint reflects sprint pressure
  weakness, not a real production calibration issue.
- Any magnitude-channel H-measurement (H2, H3) run on sprint only
  will systematically over-report deflation.
- Any acceptance bar calibrated to sprint will mis-set the live
  threshold in either direction depending on the bar's sign.

## 2. Phase 11 operating rule

**WS3 H-measurement (H2, H1, H3) and acceptance bars read against the
live-tape forecasts archive: `data/forecasts/<day>.jsonl`.** The
archive is written continuously by the production `TradingEngine` with
the live calibrator, auditor, and projector active. Scripts that
consume it:

- `scripts/ws3_h2_option1a_snapshot.py` (default `--forecasts-root =
  data/forecasts`)
- any future `scripts/ws3_h1_*.py`, `scripts/ws3_h3_*.py`

**Sprint harness retained for, and limited to:**

- `scripts/verify_determinism.py` — byte-identity contract across
  runs with the same inputs. Currently the tensor-engine refactor's
  merge gate.
- code-change attribution (e.g. the Step 4 driver itself —
  `scripts/ws3_h2_step4_live_attribution.py`). Running sprints with a
  new mechanism lets us observe whether the mechanism *behaves
  differently*, which is a regression-test signal. It does **not**
  license "the mechanism produces X%/Ybps more/less" claims as
  production-transferable.
- regression testing of new engine code against fixed fixtures.

**What this rule forbids:**

- Quoting a sprint-measured directional-hit percentage as the
  acceptance number for D3 (52 % @ 20s). The D3 measurement runs
  on the live-tape forecasts archive.
- Declaring H1/H2/H3 "validated" from sprint-only evidence.
- Introducing new H-gates that read sprint output.

**What it permits:**

- A→B *direction* signals from sprint attribution (e.g. "R2 made
  sprint correct-dir p50 grow" vs "shrink") — as an internal
  sanity-check that the mechanism change is not a no-op.
- Performance profiling and determinism regression.

## 3. Retroactive caveats

Three existing Phase 11 artifacts predate this operating rule and
were measured on sprint output. A caveat paragraph is added to each
linking back to this doc. Affected files:

- `research/phase11_diagnostic/WS1_exit_attribution.md` — 236-position
  attribution replay on sprint fixture, not live tape. Directional
  findings (44.5 % @ 20s) are qualitatively correct in sign but the
  absolute bps magnitudes (−10.04 realized, etc.) are not
  production-transferable.
- `research/phase11_diagnostic/WS2.5_regime_gate.md` — variant A/B on
  sprint fixture. Cross-seed control over RNG is intact; sprint-
  to-live transfer is open.
- `research/phase11_diagnostic/WS3_H2_option1a_snapshot.md` — §§1-7
  read live-tape forecasts (caveat-free); §8 reads sprint tape (the
  caveat is already quoted within §8.2).

Caveat paragraph template applied at the top of each:

> **Measurement caveat (added 2026-04-23).** Findings in this artifact
> derived from the `SprintEngine` replay harness, not the live-tape
> forecasts archive. Per `MEASUREMENT_INFRA.md`, the sprint
> microstructure produces ~400× weaker per-tick net_pressure than
> the live Binance BTCUSDT tape, and magnitude-channel transfer to
> production is not quantitatively validated. Directional findings
> and relative A/B deltas remain informative; absolute bps-magnitude
> numbers should be re-measured against `data/forecasts/<day>.jsonl`
> before they're used to set acceptance gates.

## 4. `MAX_GAIN` ceiling — decision proposal

The live-feedback attribution saturated `MAX_GAIN=40.0` on all 4
panel days. Reviewer surfaced two options before R2 lands:

- **(i) Raise MAX_GAIN (e.g. to 200).** Diagnostic — lets the sprint
  calibrator find its true unclamped equilibrium. Tells us where
  the sprint calibration *wants* to land absent the ceiling.
- **(ii) Leave MAX_GAIN at 40; move WS3 measurement to live tape per
  §2.** Accepts sprint saturation as an artifact of sprint pressure
  weakness. Zero code change.

### 4.1 Proposed decision: **(ii)** — leave ceiling, switch to live tape

Reasoning, in order of weight:

1. **Production gain is 1.57, well below the 40.0 ceiling.** The
   ceiling is not wrong for production — it's 25× higher than the
   current operating gain. There is no evidence live operation is
   brushing against `MAX_GAIN`, so raising it would be a fix without
   a problem.
2. **The sprint saturation is a §1.2 artifact, not a calibration
   issue.** Pressure is 400× weaker in sprint. To close a 400× gap
   the calibrator would need gain ~600 at production-equivalent
   emove — raising to 200 would still be ceiling-bound in sprint.
   The raise-to-diagnose option does not in fact diagnose; it just
   moves the ceiling.
3. **Changing a production-visible setting for a sprint-only
   concern couples the two.** We have just established the sprint
   path is a regression tool, not a measurement tool. Tuning a live
   constant to make sprint behave differently reintroduces the
   coupling §2 tries to break.
4. **The measurement §2 calls for — WS3 H-acceptance on the live
   tape — does not need the sprint ceiling to be different.** On
   the live tape, production gain is 1.57 and corrections sit in
   the 0.69-2.47 range (Option 1a, per §1.1 of the snapshot doc).
   MAX_GAIN=40 gives a 25× headroom against the operating point —
   comfortable; no imminent re-clamp risk.

### 4.1.1 Revisit triggers

This decision is predicated on live gain ≈ 1.57 (25× headroom) and
on the sprint saturation being a downstream artifact of §1.2 pressure
weakness. Any of the following invalidates the predicate and requires
§4.1 to be re-argued from scratch — do not re-apply this reasoning
mechanically if the ground shifts. Re-open if:

1. **Live gain drifts up.** Persisted `data/calibration/projector_gain.json`
   sustained above **20.0** for **72+ continuous hours**, measured from
   the `updated_at` trail. At gain 20 the 25× headroom collapses to 2×
   and the "ceiling is not binding in production" premise is gone.
2. **Sprint pressure weakness is root-caused and closed.** Any of the
   §5 follow-up items (synth trade cadence, depth asymmetry, size
   distribution, label cadence) lands a fix that restores sprint
   `net_pressure` to within ~2× of live. At that point the sprint
   saturation stops being a §1.2 artifact, and option (i)'s
   "diagnose the unclamped equilibrium" motivation becomes real.
3. **Structural projector change invalidates the 1,595-anchor baseline.**
   Any commit that changes the kernel fit target, the ratio definition,
   the anchor spec, or the label set underneath `KernelCalibrator` means
   the 1.57 operating point is not comparable across the change. Re-
   derive the headroom from the post-change production gain before
   treating §4.1 as still valid.

None of these conditions are expected during the R2 attribution cycle.
If a future reviewer finds they hold, the decision reopens rather than
auto-applying.

### 4.2 What (ii) explicitly defers

- Answering "what is the sprint calibrator's true equilibrium?"
  becomes a §5 follow-up item; it is not on the Phase 11 critical
  path.
- If a future sprint-mechanism change needs the calibrator to
  *converge* (not ceiling-bind) as an attribution signal, we either
  raise `MAX_GAIN` for that investigation only (feature-flagged) or
  fix the underlying sprint pressure delta first (§5). We commit
  to neither from this doc.

### 4.3 What (ii) does not change

- `MIN_GAIN=1.0` stays. The production operating point is 1.57 — a
  real number 57 % above the floor. No floor-artifact risk on the
  live tape under Option 1a.
- The correction clamps `[0.25, 4.0]` stay. Telemetry shipped in
  `c81148f` logs when either clamp binds.

**Confirmed by reviewer 2026-04-23.** Option (ii) accepted;
revisit-trigger paragraph §4.1.1 added in the same review to future-
proof the decision. R2 attribution proceeds on this footing; the
MAX_GAIN ceiling stays at 40.0 and WS3 H-measurement reads the live-
tape archive per §2.

## 5. Follow-up work (post-Phase-11 scope)

Root-causing the 400× sprint pressure delta is explicitly out of
Phase 11 scope, but it is on the post-Phase-11 docket. Candidate
mechanisms, ordered by cheap-to-check:

1. **Synth trade volume/frequency.** `data/events/<day>.jsonl` is a
   synth-60 (60 events/bar ≈ 1 Hz) stream; real BTCUSDT trade cadence
   on Binance peaks 20-50 Hz during active volatility. Under-sampled
   trade stream directly reduces the tribe-consensus-to-pressure
   signal.
2. **Synth order book depth distribution.** Synth book sets uniform
   depth ladders; real book has regime-dependent asymmetry and
   pockets. Impacts microstructure-aware tribes (LiquidityHunter,
   SpoofDetector, etc.).
3. **Synth trade-size distribution.** Real trades have heavy tails
   (whales + 5-tick scalpers); synth likely has narrower size
   distribution. Pressure tribes read signed-volume-impact.
4. **Label cadence in the auditor anchor spec.** Sprint produces
   15s/1m only; live produces all four. If H1/H3 need 5m/10m anchors
   and the synth book can't serve them, sprint can't regression-test
   those H-validations either.

**Decision principle for the follow-up:** fix the synth generator
*only if* a future phase needs sprint-based measurement for H-
validation. Otherwise, accept the divergence permanently — cheap
determinism/regression harness, expensive measurement harness.
These are different tools and can stay different.

## 6. Provenance

- Step 4 live-feedback run: `bzkyj3gw6` (local background task), 4
  sprints sequential, 2026-04-23 ~05:55 UTC completion
- Compared against: offline snapshot timestamp `20260422T160057Z`
- Wiring commit: `686164b feat(replay): wire kernel calibrator in
  sprint harness + determinism verification`
- Attribution commit: `2a1bf26 docs(research): WS3 H2 Step 4 live-
  feedback attribution snapshot`
- Amendment commit: `b448f9c docs(research): WS3 H2 snapshot —
  live-feedback delta + refined R2 scoping`

---

*Living document. Update §3 every time a new Phase 11 artifact
lands that reads sprint output. Update §4 if the MAX_GAIN decision
ever changes. Update §5 when a post-Phase-11 phase picks up
synth-book work.*
