# Phase 11 Diagnostic — PLAN

**Branch:** `feat/tensor-engine`
**Last updated:** 2026-04-25
**Status:** H3 Phase 0 complete (P6 fix landed, P4 escalated to H5); H5
Phase 0 audit + H5.A saturation clamp landed; H6 Phase 0 audit + H6.A
DNA recursion-input clamp landed; **H6.B Phase 0 audit** (2026-04-25)
identifies a second door (scale × DNA-output) with fix queued per audit
§7 (Option B — `DEPTH_SCALE_MAX = 5`). H1 Phase 1 measurement remains
data-accumulation-gated; D5 clock is currently not running (see §5.1.3).
See §5 Master checklist for the forward map — the §1 workstream table
below is an archival index as of 2026-04-22.

## 0. North star

Decide whether the PAM council — as currently specified on free-tier
public data — can produce frictionless directional edge at
5–60 s horizons under merciless execution. If yes, find and fix the
binding constraint. If no, escalate cleanly to WS7 (premium data).

Phase 11 is a **diagnostic phase**, not a feature phase. Each
workstream is a measurement first, intervention second, and ships only
when the measurement justifies the intervention.

## 1. Workstream status

| WS   | Name                              | Status      | Artifact                                              |
|:-----|:----------------------------------|:------------|:------------------------------------------------------|
| WS1  | Exit-reason attribution           | ✅ Done      | `WS1_exit_attribution.md` (commit `652daf2`)          |
| §8   | Instrumentation gaps from WS1     | ✅ Closed    | commits `8df1666`, `2fab101`                          |
| WS2  | (Reserved — see §4)               | ⏸  Deferred | post-WS3                                              |
| WS2.5| Regime-entry gate                 | ✅ Shipped   | `WS2.5_regime_gate.md` (commit `08fc581`, `5b2faac`)  |
|      |   default                         |             | `allowed_entry_regimes: []` (gate disabled)           |
|      |   multi-day holdout               | ⏸  Pending  | folded into WS3 harness                               |
| WS3  | Signal-quality diagnostic         | ✏️ Scoped   | `WS3_SCOPE.md` (this draft) — pending review          |
| WS4+ | (Tribe orthogonality / new feeds) | ⏸  Deferred | contingent on WS3 outcome                             |
| WS6  | Exit-layer retuning               | ❌ Killed    | WS1 diagnosis B — exits are not the binding constraint|
| WS7  | Premium / orthogonal data feeds   | ⏸  Deferred | contingent on WS3 null (H4 outcome)                   |

## 2. What landed

### 2.1 WS1 — Exit-reason attribution (done)

Full-day sprint on 2026-04-20 under Merciless produced 236 closed
positions, 100 % exiting via `edge_exhausted` at exactly 20 s. Key
numbers (see `WS1_exit_attribution.md` §0–§6):

- Mark→mark directional hit at 20 s: **44.5 %** (below random).
- Mean frictionless mark-move in predicted direction @ 20 s:
  **−0.21 bps**; worsens monotonically at 60 s (−2.97), 120 s
  (−4.97), 250 s (−3.76).
- Max council confidence across 86 399 ticks: **0.2483**.
- Max `|expected_move_bps|`: **< 5 bps** on every trade.
- `trend_up` regime (29 % of trades) is the *only* regime with
  persistently positive frictionless edge (+3.50 @ 20 s → +7.48 @ 60 s).

**Verdict:** Diagnosis (B) — signal is weak; exits are not the
binding constraint.

### 2.2 §8 — Instrumentation gaps (closed)

Four gaps flagged in WS1 §8, resolved in two chore commits that each
pass `verify_determinism.py --day 2026-04-20 --max-ticks 500`:

| Gap | Description                                          | Commit    |
|:----|:-----------------------------------------------------|:----------|
| 1   | `regime_label` not on ledger close/open events       | `8df1666` |
| 2   | `peak_favorable_s` / `steps_bps` not recoverable     | `8df1666` |
| 3   | SprintEngine doesn't wire ForecastAuditor            | `2fab101` |
| 4   | Projector label/horizon mismatch at 1 Hz tick        | `2fab101` |

Ledger now carries everything needed to re-analyze a sprint without a
post-hoc cross-join. SprintEngine builds a per-run auditor; anchor
spec list is filtered at `__init__` with a one-time log line if any
label is unreachable given `tick_seconds` and `projector_steps`.

### 2.3 WS2.5 — Regime-entry gate (shipped-disabled)

Defensive gate that suppresses entries outside a configured regime
allow-list. Shipped as feature + empty default, so production behaviour
is unchanged until a flag flip.

On the An-0 day (seed 42):

- `v2_trend_only` halves daily equity bleed (−4.70 % → −2.19 %) and
  raises per-trade edge (−10.04 → −7.96 bps).
- Cross-seed σ = 0.17 bps across {42, 123, 777, 2024} — structural,
  not RNG.
- `v3_trend_cascade` is strictly worse; cascade regimes add trades with
  no edge improvement.
- `high_conf_override=0.4` is a no-op (max conf on tape = 0.248).

**Council still loses money in every variant.** Gate is a bleed
reducer, not an alpha generator. Default remains
`allowed_entry_regimes: []`; multi-day holdout validation (04-18,
04-19, 04-21) is required before flipping on, and has been folded
into the WS3 harness so we don't double-run.

## 3. WS3 — scope agreed, H2 Phase 0 is next

Scope in `WS3_SCOPE.md`. Four hypotheses for the signal collapse
diagnosed in WS1:

1. **H2** (1st) — `KernelCalibrator.bps_per_unit_pressure` pinned at
   `MIN_GAIN=1.0` floor (on-disk: gain=1.57, last_correction=0.25 —
   calibrator wants a 4× shrink it can't apply). Kernel gain is causally
   upstream of the conf formula, so H2 runs before H1.
2. **H1** (2nd) — Confidence formula's `signal_quality × directional ×
   diversity_mult` structure compounds three sub-1 factors to a cap
   near 0.25 regardless of underlying signal.
3. **H3** (3rd) — 24 tribes are heavily correlated → `N_eff` ≪ 24,
   inflating diversity_score without adding independent evidence.
4. **H4** (conditional) — Public tape is signal-less at 5–60 s →
   escalate to WS7. Fires only if H2 + all H1 options (A/B/C) + H3
   each fail to clear the 52 % bar.

**H2 opens with a mandatory path audit (Phase 0).** Before any floor
change is proposed, trace one forecast end-to-end through the
prediction → realized comparison path to rule out structural bugs
(double-applied regime multiplier, conviction_boost leak, off-by-one
anchor horizon, unit errors). Report findings to
`WS3_H2_path_audit.md`. If the audit surfaces a bug, the fix
supersedes the floor-change intervention.

**Execution rule:** measure → intervene → re-measure, one hypothesis at
a time, one commit per mechanism change, determinism-gate pass
required between each. No stacking.

**Acceptance for "WS3 delivered":** either a single intervention raises
**aggregate directional hit at 20 s across the 3-day tape panel
(04-18, -19, -20, -21) to ≥ 52 %**, or a written null for H2 + H1 +
H3 that elevates H4 and requests WS7 scope. Explicitly not a PnL bar
— signal diagnosis is decoupled from execution economics, which is a
separate post-WS3 workstream.

## 4. WS2 — deferred

Original WS2 framing was "decision engine confidence / entry
calibration." WS3 subsumes most of that work (H1 is literally a
decision-engine confidence question). After WS3 ships, revisit whether
a separate WS2 remains, or whether the remaining scope is already
absorbed into WS3 Option A/B/C.

## 5. Master checklist — forward map

Phase 11 is the current phase. §1's 7-workstream index is archival
(as-of 2026-04-22). This section is the **forward map**: what's in
flight, what's queued after R2 lands, and what's deferred. The
historical D1-D5 scope decisions are preserved as §5.4.

### 5.1 Active chips (in flight)

| Chip | State | Note |
|:-----|:------|:-----|
| **H6.B fix — depth-scale cap** | ▶ Active — queued for next session | Single-line fix in [`src/projector/chess.py`](../../src/projector/chess.py) per [`WS3_H6B_phase0_audit.md`](WS3_H6B_phase0_audit.md) §7 Option B: `scale = min(scale, DEPTH_SCALE_MAX=5)` on line 628. Blocks D5 clock restart — steady-state `saturation_hit` was 100 % after H6.A alone (door #1 only); door #2 (scale × DNA-output) is the current dominant mechanism. Fix box estimated 1 h 50 (see audit §7 + §10). No code touched tonight per cadre. |
| H1 Phase 1 — decomposition measurement | ⏸  Blocked behind H6.B fix + new D5 anchor | Analyzer script + tests landed 2026-04-24 ([`scripts/ws3_h1_decomp_analyze.py`](../../scripts/ws3_h1_decomp_analyze.py), [`tests/test_ws3_h1_decomp_analyze.py`](../../tests/test_ws3_h1_decomp_analyze.py)). D5 clock is not currently running (see §5.1.3); earliest Phase 1 run requires H6.B landed + 3 consecutive full days of post-fix Bybit tape. Pre-I1 + pre-H5.A archives do not count. |
| Venue swap — Bybit primary ingest | ▶ Shipped | Binance futures WS silently geo-blocked from this host 2026-04-22 → present (TLS OK, zero frames). Switched primary venue to Bybit BTCUSDT linear perps (`wss://stream.bybit.com/v5/public/linear`); commit `a9a220c`. See §5.1.2 for full amendment and panel-invalidation consequences. |
| 2026-04-21 panel inclusion | ⏸  Quarantined 08:44→14:00 UTC | **Superseded by §5.1.2 venue amendment** — full 04-18 → 04-21 panel now venue-obsolete regardless of Fix A state. Quarantine retained for reference until Fix A scope is rewritten against the Bybit panel. |

### 5.1.0 Recently closed (2026-04-23 → 2026-04-25)

| Chip | Closed | Commit(s) | Note |
|:-----|:------:|:----------|:-----|
| MAX_GAIN ceiling decision | 2026-04-23 | `26cb61e` | Option (ii) accepted; WS3 H-measurement reads live tape. `MEASUREMENT_INFRA.md` §4.1.1 revisit triggers in place. |
| 2026-04-21 projector divergence audit | 2026-04-23 | [`proj_divergence_20260421_audit.md`](proj_divergence_20260421_audit.md) | Conjunction root cause identified; Fix ladder A→C→B queued in §5.1.1. |
| R2 commit A — `conviction_boost` drop from projector | 2026-04-23 | `fd400e8` | Measurement-hygiene refactor; 0.00 pp directional delta (algebraic). |
| R2 commit B — `conviction_boost` as sizing multiplier | 2026-04-24 | `20b53f6`, docs `31b0646` | Full-day determinism PASS; sprint dormant by construction ([`WS3_R2_step_B_attribution.md`](WS3_R2_step_B_attribution.md)). |
| WS3 H1 Phase 0 — structural audit | 2026-04-24 | `827040e` | CLEAN verdict ([`WS3_H1_path_audit.md`](WS3_H1_path_audit.md)); I1 unblocked. |
| WS3 H1.I1 — instrumentation | 2026-04-24 | `3e2c3b6`, docs `42c79a0` | Five-field decomposition plumbed through auditor; full-day determinism PASS (sha256 `1fc982a69f3a1df5…`). |
| Parallel-audit cohort (WS5/WS4/WS2/WS3b) | 2026-04-24 | `c46670a` `9bd1d86` `cb512fc` `05e3b05` | Ran read-only while the H1.I1 full-day gate was in flight, per `WS3_H1_SCOPE.md` §11. Docs-only; zero code change; gate result authoritative. See cohort roll-up below. |
| WS3 H3 Phase 0 — council-redundancy path audit | 2026-04-24 | `484d9d5` | P6 bug-found (`_TRIBE_CATEGORY` missed 20/24 tribes — only 4 mapped); P4 attractor escalated to H5; P8 resolved. See [`WS3_H3_path_audit.md`](WS3_H3_path_audit.md). |
| WS3 P6 fix — tribe-category mapping | 2026-04-24 | `84f50ed` | `_TRIBE_CATEGORY` now covers all 24 loaded tribes; one-line dict fix + tests. |
| WS3 I3 — live-tape additive schema | 2026-04-24 | `c0afa04` scope, `fabc5f4` I3.a, `0586a73` I3.b, `2ffe035` I3.c | `conviction_boost` / `harvest_mass_ratio` / `path_integrity_ok` / `regime_label_at_tick` / `realized_bps_paper_model` mirrored onto PendingAnchor / Resolved / ShadowTrade (unblocks WS5/WS4/WS2 live-tape measurement). |
| WS3 H5 scope | 2026-04-24 | `671dd4e` | Cumulative-path oscillation diagnostic scoped as P4 escalation target. |
| WS3 H5.I1 — instrumentation | 2026-04-24 | `b1c1f97` | Persist `abs_activity_bps` + `step_bps_signflips` per tick through auditor. |
| WS3 H5 Phase 0 — audit | 2026-04-24 | `f101800` | **Bug-found.** 1/99 attractor fingerprinted; runaway `step_bps` identified as dominant mechanism. Fix = saturation clamp. See [`WS3_H5_phase0_audit.md`](WS3_H5_phase0_audit.md). |
| WS3 H5.A — saturation clamp | 2026-04-24 | `f0076bd` | `MAX_STEP_BPS = 500.0` output clamp in [`src/projector/chess.py`](../../src/projector/chess.py) `_apply_saturation_clamp`; forces `saturation_hit = True` + `conf = 0` on breach. Full-day determinism gate PASS. |
| Pathological baseline — pre-H5.A | 2026-04-24 | `f948e36` | `data/baseline/pathological_2026-04-24_pre-H5A` — 12 011 rows, 4 sha256 anchors, H5 P4 attractor reproduced. Regression reference for any future DNA-path tuning. |
| WS3 H6 scope | 2026-04-24 | `ff995a3` | DNA-recursion positive-feedback audit scoped as H5.A partial-fix follow-up. |
| WS3 H6 Phase 0 — audit | 2026-04-24 | `cf97165` | **Bug-found.** Jury/Schur on 2nd-order recurrence: `b = β · scale` exceeds unity on `ta_dna` for any `scale > 1.54`. Two candidate fixes: DNA input clamp (Option C) vs mechanism rewrite. See [`WS3_H6_phase0_audit.md`](WS3_H6_phase0_audit.md). |
| WS3 H6.A — DNA recursion-input clamp | 2026-04-24 | `6332848` (local; not yet pushed) | `DNA_CTX_CLAMP_BPS = 100.0` applied to `prev_state_bps` / `prev_delta_bps` entering DNAContext ([`src/projector/chess.py:601-603`](../../src/projector/chess.py)); 22 tests in [`tests/projector/test_dna_input_clamp.py`](../../tests/projector/test_dna_input_clamp.py); determinism gate PASS byte-identical. **Closes door #1 only** — 2026-04-25 live 5-min validation showed 100 % steady-state `saturation_hit`, revealing a second door. Commit title ("closes Phase 0 bug-found") is partial; reconciled in PR body. |
| WS3 H6.B Phase 0 — audit | 2026-04-25 | `e666c05` (local; not yet pushed) | **Bug-found — door #2.** `step_bps = (dna_delta + event_bps) × scale` where `scale ∈ [1, 20]`; post-H6.A-clamp `ta_dna` output ≈ 65 bps → breaches `MAX_STEP_BPS = 500` at `scale > 7.1`. Simple linear inequality, not Jury/Schur. Fix recommendation: Option B — cap `scale` at `DEPTH_SCALE_MAX = 5` ([`chess.py:628`](../../src/projector/chess.py)); worst-case quartile LOW–MEDIUM. §8 scans 3 candidate doors #3 (event_bps / base_strength / consensus) with promotion criteria. See [`WS3_H6B_phase0_audit.md`](WS3_H6B_phase0_audit.md). |

**Parallel-audit cohort roll-up (2026-04-24):**

- **WS5 harvest-path audit** ([`WS5_harvest_path_audit.md`](WS5_harvest_path_audit.md)) — sprint cannot fire the harvest-mass conviction_boost path; `ChessProjector.calibrator is None` in sprint. §2 "400× weaker pressure" framing reduced to categorical, not quantitative. Recommend live-tape measurement once I3 lands (`conviction_boost` + `harvest_mass_ratio` + `path_integrity_ok` plumbed to auditor).
- **WS4 regime-hysteresis audit** ([`WS4_regime_hysteresis_audit.md`](WS4_regime_hysteresis_audit.md)) — 1,268 regime transitions / 16,811 sprint ticks (75.4/1000); 54% of runs ≤5 ticks; `chop↔normal` 43% of all transitions. Not an H1 blocker; Schmitt-threshold intervention scoped for post-H1.
- **WS2 executor parity audit** ([`WS2_executor_parity_audit.md`](WS2_executor_parity_audit.md)) — reframes PLAN Q3: there is no "shadow executor", only PaperExecutor + per-tribe TribeShadowLedger. Six asymmetry classes documented (depth-blind fill, no Harden overlay, no book-depth cap, no black-swan detector, no funding accrual, fee defaults 2.0 vs 5.0 bps). Dormant in sprint (no calibrator consumers); live-only impact. Recommend I3 adds `realized_bps_paper_model` alongside `shadow.realized_bps`.
- **WS3b warmup/persistence audit** ([`WS3b_warmup_persistence_audit.md`](WS3b_warmup_persistence_audit.md)) — "Weakness #4" of PLAN:183 reinterpreted from PLAN's parenthetical (the original weakness list is absent from the committed scope doc). Persistence-weighted anchor horizons: capability gap, not broken impl — anchor specs fixed `(label, step_idx)`, no regime label on `PendingAnchor`/`Resolved`, no dwell weighting. Warmup: KernelCalibrator `min_samples=50` + immediate hi-clamp saturation on sprint (confirmed live by PID 1462 gate log). Four recommendations, priority-ordered; no protected surfaces touched.

### 5.1.2 Scope amendment — Bybit venue swap (2026-04-24 evening)

**Trigger.** Attempt to launch `scripts.run_bot` on 2026-04-24 ~18:24 UTC found Binance USD-M futures WS (`wss://fstream.binance.com/stream`) silently unresponsive: TLS handshake completes, WebSocket upgrade succeeds, subscription is accepted, zero application frames arrive over 3+ minutes on aggTrade / markPrice / depth. Minimal isolated probe reproduces (see commit log / session transcript). Binance **SPOT** WS from the same host delivers frames immediately, confirming a futures-specific block rather than a local network failure. No VPN available.

**Amendment.** Primary live ingest venue changes from Binance BTCUSDT USD-M perpetuals to **Bybit BTCUSDT linear perpetuals** (`wss://stream.bybit.com/v5/public/linear`). Probe confirms tickers + orderbook frames flow within seconds. Bybit public WS remains free-tier (`project_budget_constraint.md` respected).

**Consequences.**

1. **Panel invalidation.** The 2026-04-18 → 2026-04-21 Binance perps live-tape archive (`data/forecasts/2026-04-{18,19,20,21}.jsonl`) is no longer the measurement reference for H1 / H3 / any downstream hypothesis. Different venue ⇒ different microstructure (tick size, maker/taker density, funding rate schedule, liquidation cadence). The four archives are retained on disk for code-change regression reference but are out-of-scope for any new acceptance measurement.
2. **D5 clock reset to zero.** The "≥ 3 consecutive days" multi-day panel rule (D5) now counts from the first full post-swap Bybit day. Earliest Phase 1 run shifts from 2026-04-27 to **≥ 2026-04-28** (04-24 evening partial + 04-25 + 04-26 full + 04-27 partial; strict ≥ 3 full days wants 04-25/26/27 close → run 04-28 morning).
3. **D6 rule still holds.** Live-tape-only for H-measurement; sprint remains reserved for determinism gates. The "400× weaker pressure" claim in `MEASUREMENT_INFRA.md` §2 was calibrated against Binance perps; the ratio versus Bybit is unmeasured but plausibly similar (both are the same asset class at the same cadence). Revisit when first Bybit day is in.
4. **Determinism gate unaffected.** `verify_determinism --day YYYY-MM-DD` reads cached Binance klines for synth-tick generation and writes to a temp dir; it never hits live WS. Existing gate artifacts (full-day PASS at `3e2c3b6`) remain authoritative for the instrumentation surface.
5. **Intervention options A/B/C (scope §4) stay valid.** They are formula-level changes to `signal_quality / directional_score / diversity_mult` arithmetic, venue-agnostic. Post-intervention determinism gate + re-measurement ride the Bybit panel instead of the Binance panel.
6. **Fix queue (§5.1.1) scope-rewrite required.** Fix A ("per-step `step_bps` clamp") was sized (±400 bps) against observed Binance 04-18/19/20 maxima. The clamp threshold is re-derivable from any venue's first full day; no mechanism change — just a one-line threshold re-fit once 04-25 Bybit data lands. Fix C is venue-agnostic as written.
7. **Parallel audits (§5.1.0 roll-up) unaffected.** Q3/Q5/Q6 I3-plumbing recommendations and Q4 Schmitt-threshold scope all operate on the live-tape regardless of venue. Schema-side additions in I3 carry forward.

**Risks acknowledged.**

- Bybit BTCUSDT has different funding cadence (8h vs 8h — aligned) but different fee structure and rebate ladder. Paper-executor fee defaults (2.0 bps in current config per WS2 audit) may need revisit when real execution is considered; measurement-only pipeline is unaffected.
- Liquidation feed on Bybit (`liquidation.<symbol>` channel) carries different message shape than Binance `forceOrder`; the primary ingress will normalize, but the `liquidation_hunter` tribe's rate calibration was done on Binance volume. Re-check once first Bybit day lands.
- Bybit tick price-size is 0.10 (same as Binance on BTCUSDT USD-M), but orderbook depth resolution is 50 levels (`orderbook.50.BTCUSDT`) versus Binance's 20. Depth-aware projector kernel normalization (in `_normalize_book_depth`) is top-of-book + 5-bps window → resolution difference is absorbed before it reaches the kernel.

**No scope docs edited.** This amendment lives in PLAN only; `WS3_SCOPE.md` D5/D6 and `WS3_H1_SCOPE.md` §3 remain as written. The live-tape rule is unchanged; only the venue is. Consider a scope-doc revision only if a second venue swap is considered, or if measurement finds Bybit microstructure materially changes intervention ordering.

### 5.1.3 D5 clock status (2026-04-25)

**Clock not currently running.** Two successive invalidations since the
Bybit venue swap mean no accumulation window has been laid down yet:

1. **2026-04-24 evening — venue swap (§5.1.2)** reset the D5 count to
   zero on the Binance panel.
2. **2026-04-24/25 — H5.A + H6.A landings** change projector output
   shape (runaway clamp + DNA-input clamp). Any Bybit tape recorded
   pre-H5.A is pre-fix and does not count toward a D5 window that
   measures the post-fix world.
3. **2026-04-25 — H6.B Phase 0 audit** identifies a third fix (door
   #2, scale × DNA-output) still pending. Starting a D5 window before
   H6.B lands would measure a projector that still saturates by
   construction.

**Restart protocol.** D5 clock begins at the first full Bybit day after
H6.B fix lands and passes a full-day determinism gate + 5-min live
validation showing `saturation_hit` steady-state ratio < 10 %. Earliest
Phase 1 run = fix-day + 3 full Bybit days (04-26 if H6.B lands
2026-04-26 morning → clock 04-26/27/28, run 04-29).

**Invariants preserved:**

- D5 rule (multi-day panel, ≥ 3 consecutive days) — unchanged.
- D6 rule (live-tape only for H-measurement) — unchanged.
- Determinism gate authority — every projector change rides the
  `verify_determinism.py` gate; pathological baseline
  (`f948e36`) is the regression reference for any future DNA-path
  tuning.

### 5.1.1 Post-R2 fix queue (from 2026-04-21 divergence audit)

R2 closed 2026-04-24 (both commits landed); H1 Phase 0/I1 opened
immediately as the next chip (per §5.2 Q1). Fix ladder below is
therefore **deferred post-H1 Phase 1**, not post-R2 — rationale is
that the 04-21 contamination is already mitigated by panel
quarantine, and the 4-day panel is reconstituted once Fix A lands.
H1 Phase 1 runs forward against post-I1 days only (no dependency on
the pre-I1 panel), so the ladder does not block H1 progress.

| Fix | Scope | State |
|:----|:------|:------|
| **A — per-step `step_bps` clamp + counter** | `chess.py:~580`; cap `step_bps` to ±400 bps (5× the largest legitimate single-step observed on 04-18/19/20). Loud counter + pre-clamp value log — clamp must not be silent. Protects against any DNA runaway, not just retail. | ⏸ Deferred post-H1 Phase 1 |
| **C — anchor-enqueue sanity check** | `engine.py::_compute_predictions`; skip anchor if \|emb\| > 2000 bps or non-finite; counter `forecaster.anchor_dropped`. Defense-in-depth behind Fix A. | ⏸ Deferred post-Fix-A |
| **B — retail quadratic → tanh saturator** | `behavioral_dna.py::retail_dna`; replace `0.0004 · state²` overshoot with `40 · tanh(state/400)` saturator; cap `chase` at `min(4·prev_delta, 200)`. Mechanism change — requires full WS3 H2 re-evaluation. | ⏸ Conditional — lands only if Fix A's counter fires on 04-18/19/20 clean days |
| **D — projector state reset on gap** | Flag-gated ForecastAuditor window truncation + conviction-history warm-start reset + cold-start calibrator for one projection after gap > 300 s. | ⏸ Deferred — re-scope after A+B+C attribute cleanly |
| **E — strengthen `mean_reversion_rate`** | YAML bump `0.005 → 0.015` or horizon-aware schedule. One-line change but interacts with WS3 H2 consensus fit (the 0.02 → 0.005 drop was motivated). | ⏸ Deferred — WS3 R2 candidate with own attribution sprint |

Attribution discipline (per audit §4): Fix A → re-replay 04-21 → Fix C → re-replay → Fix B (if counter-promoted) → re-replay. One commit per mechanism, determinism gate between each. No bundling.

### 5.2 Forward map — remaining workstreams

R2 closed 2026-04-24; H1 Phase 0/I1 closed 2026-04-24; Q3-Q6
audits delivered as a docs cohort 2026-04-24 (see §5.1.0 roll-up).
The **measurement** commits for Q3/Q5 remain conditional on I3
plumbing; Q4/Q6 recommendations are scheduled post-H1 per each
audit's §5. Q2/Q7/Q8 unchanged.

| # | Workstream | Scope sketch | Gate / State |
|:--|:-----------|:-------------|:-------------|
| Q1 | **H1 — confidence-formula compounding** | Measure whether `signal_quality × directional × diversity_mult` structurally caps confidence < 0.25 regardless of the underlying signal. Phase 0 ✅, I1 ✅, Phase 1 ▶ blocked on ≥3 post-I1 days. Options A/B/C per [`WS3_H1_SCOPE.md`](WS3_H1_SCOPE.md) §4. | ▶ Phase 1 active (data-gated); earliest run 2026-04-27. |
| Q2 | **H3 — tribe correlation / N_eff** | Measure effective rank of 24-tribe vote matrix; diversity_mult inflation quantified. | ⏸ Opens after H1 converges or nulls. |
| Q3 | **WS2 — executor parity audit** | Shadow-vs-main executor fill/slippage/queue behaviour diff. Deferred since WS1. | ✅ Audit delivered `cb512fc` ([`WS2_executor_parity_audit.md`](WS2_executor_parity_audit.md)). Live-tape measurement of bias pending I3 plumbing (`realized_bps_paper_model`). |
| Q4 | **WS4 — regime classifier hysteresis** | WS2.5 exposed regime sensitivity; classifier stability across 5-60 s horizons is a raised-priority follow-up. | ✅ Audit delivered `9bd1d86` ([`WS4_regime_hysteresis_audit.md`](WS4_regime_hysteresis_audit.md)). Schmitt-threshold intervention scoped post-H1. |
| Q5 | **WS5 — harvest boost ablation** | Remove the harvest-tribe amplification path; measure delta. | ✅ Audit delivered `c46670a` ([`WS5_harvest_path_audit.md`](WS5_harvest_path_audit.md)). Sprint cannot fire harvest path by construction; live-tape ablation pending I3 plumbing. |
| Q6 | **WS3b — original WS3 warmup / persistence** | Persistence-weighted anchor horizons, warmup behaviour (PLAN:183 "Weakness #4" rephrased). | ✅ Audit delivered `05e3b05` ([`WS3b_warmup_persistence_audit.md`](WS3b_warmup_persistence_audit.md)). Additive anchor-regime-label schema extension queued post-H1. |
| Q7 | **WS7 — L2 microstructure data feed** | H4 escalation: only if H1 A/B/C + H3 each null against the 52 % bar. Paid-data conversation per `project_budget_constraint.md`. | ⏸ Conditional — H1/H3 nulls. |
| Q8 | **Phase 11 Retrospective** | Consolidate findings → phase close doc; recommend next phase. | ⏸ Terminal. |

### 5.3 Deferred chips

None currently. Scope-discipline rule: "worth looking at later"
observations land here as one-liners with a rationale; they do not
interrupt the active sequence.

### 5.4 Scope decisions (2026-04-22, historical)

Open questions from the WS3 scope draft, answered in review on
2026-04-22. Preserved for traceability; full rationale in
`WS3_SCOPE.md` §0.

| # | Decision |
|:--|:---------|
| D1 | **H2 → H1 → H3 → H4.** Kernel gain is causally upstream of the confidence formula; H1 measurements on top of uncalibrated H2 are contaminated. |
| D2 | **H2 opens with a prediction→realized path audit** before any floor change. Trace one forecast end-to-end; report findings before proposing intervention. |
| D3 | **Acceptance = aggregate directional hit ≥ 52 % @ 20 s** across 3-day tape panel. Not PnL. Do not raise the bar beyond 52 % (N=236 doesn't support tighter claims). |
| D4 | **H4 escalation is condition-based:** H2 validated AND H1 A/B/C each fail AND H3 fails. Any single H lifting to 50-52 % pauses on that H for option-swap iteration. |
| D5 | **Multi-day harness from day one.** Every H-measurement runs across (04-18, -19, -20, -21). WS2.5 v1-vs-v2 re-runs ride the same panel as a standalone section of whichever H ships first. |
| D6 (2026-04-23) | **WS3 H-measurement reads live-tape archive, not sprint.** Sprint microstructure produces ~400× weaker net_pressure than live BTCUSDT; acceptance numbers must come from `data/forecasts/<day>.jsonl`. Sprint reserved for determinism / regression / code-change attribution. See `MEASUREMENT_INFRA.md` §2. |

## 6. What's deliberately not happening

- **No exit-layer changes.** WS6 killed by WS1; reopen requires fresh
  evidence.
- **No tribe swaps.** H3 scopes correlation diagnosis; swaps are
  WS4+ contingent on that result.
- **No paid data feeds.** H4 escalates to WS7 only if H1/H2/H3 null;
  paid data requires a separate user conversation per
  `project_budget_constraint.md`.
- **No model replacement** (transformer, RL, anything else). Out of
  Phase 11 entirely.
- **No live-trading promotion** until a WS3 win survives cross-seed +
  multi-day holdout.

## 7. Reproduction — what's wired right now

```bash
# WS1 sprint + attribution (already shipped; for re-running)
PYTHONPATH=. python -m scripts.ws1_sprint --day 2026-04-20 --seed 42
PYTHONPATH=. python -m scripts.ws1_exit_attribution \
    --run-dir data/replays/ws1_merciless_seed42/variants/full_day/

# WS2.5 gate variants
PYTHONPATH=. python -m scripts.ws2_5_run_variants --day 2026-04-20 --seed 42
PYTHONPATH=. python -m scripts.ws2_5_analyze --seed 42

# Determinism gate (must pass before any mechanism change merges)
PYTHONPATH=. python -m scripts.verify_determinism \
    --day 2026-04-20 --max-ticks 500
```

WS3 reproduction commands are listed in `WS3_SCOPE.md` Appendix; those
scripts do not yet exist.

## 8. H2 audit outcome

Verdict: **suspected-bug, two candidates** (see
`WS3_H2_path_audit.md`). Floor-change intervention (§4.4 Options
A/B/C) is shelved pending fixes.

1. **Calibrator fit target is a signed ratio, not a magnitude.**
   `kernel_calibrator.py:130` takes median of signed `r/p`, which
   conflates directional accuracy with magnitude bias. With WS1's
   44.5 % directional hit, the raw median sits near zero and the
   `max(0.25, …)` clamp floors it to 0.25 — the on-disk
   `last_correction=0.25` is a clamp artifact, not a measurement.
2. **`conviction_boost` is a unidirectional inflator.**
   `chess.py:769-770` multiplies consensus `steps_bps` by a
   `[1.0, 1.5]` boost on the prediction side; realized-side market
   move is raw. Unidirectional inflation up to 1.5× that the
   calibrator cannot disentangle.

Reviewer resolutions (2026-04-22):

- **R1 — Fit target = Option 1a** (drop wrong-direction samples);
  log correct-direction ratio distribution + dropped-sample fraction.
- **R2 — Move `conviction_boost` to `DecisionEngine` as a sizing
  amplifier**; verify Path-Integrity Cap interaction intentionally.
- **R3 — Separate commits with attribution discipline:** commit A →
  replay → snapshot → commit B → replay → snapshot. No bundling.
- **R4 — Defer H1 reordering** until post-H2 snapshot; note folded
  into `WS3_H2_path_audit.md §7`.

## 9. Standing rule — audit before tune

Every remaining hypothesis (H1, H3) opens with a structural path
audit before any intervention. Artifact: `WS3_H{i}_path_audit.md`.
Verdict `{bug-found, suspected, clean}`. Pause for review before
mechanism changes. H2 caught two bugs where we expected one — make
this the default.

## 10. Immediate next action

**H6.B fix is the active chip — blocks D5 clock restart.**

Per [`WS3_H6B_phase0_audit.md`](WS3_H6B_phase0_audit.md) §7, apply
Option B: cap depth-scale at `DEPTH_SCALE_MAX = 5` on
[`chess.py:628`](../../src/projector/chess.py). One-line mechanism
change; worst-case-quartile LOW–MEDIUM. Box estimate 1 h 50
(audit §7 + §10): ~30 min implementation + tests, ~40 min determinism
gate + pathological replay, ~30 min live 5-min validation,
~10 min handoff updates.

**Acceptance (all four):**

1. `tests/projector/test_dna_input_clamp.py` + new scale-cap tests green.
2. `verify_determinism --day 2026-04-20` byte-identical across two runs.
3. Pathological baseline replay (`data/baseline/pathological_2026-04-24_pre-H5A`) — `saturation_hit` ratio drops by ≥ 50 % vs. pre-fix baseline.
4. Live 5-min bot restart on Bybit — steady-state `saturation_hit` ratio < 10 % (vs. 100 % after H6.A alone).

**Escalations if acceptance fails:**

- If steady-state sat ratio stays > 30 %, STOP and promote one of the
  §8 candidate doors #3 (event_bps / base_strength / consensus
  aggregation) per that audit's promotion criteria.
- If pathological replay fails to converge, re-open H6 audit with
  full numerical derivation (not just Jury/Schur qualitative bound).

**After H6.B lands:**

1. **D5 clock restart** — first full Bybit day post-H6.B is day 1
   (§5.1.3 restart protocol). Earliest H1 Phase 1 run = fix-day + 3.
2. **H1 Phase 1 measurement** —
   `python -m scripts.ws3_h1_decomp_analyze --days <D+1> <D+2> <D+3>`.
   Produces per-field distributions, horizon-stratified tables, top-Q
   subset. JSON artifact lands in
   `research/phase11_diagnostic/_ws3_h1_decomp/`.
3. **Phase 1 snapshot** → `WS3_H1_phase1_snapshot.md` identifies
   binding factor; selects intervention A/B/C per scope §4.
4. **Intervention commit** — one mechanism, full gate, full re-measure.
5. **H3 / Fix queue** (§5.2, §5.1.1) — re-sequenced post-H1 snapshot.

**Attribution discipline** throughout: one commit per mechanism
change, determinism gate between each (`scripts.verify_determinism
--day 2026-04-20`), no bundling. See §5 for the full forward map.

---

*PLAN last revised 2026-04-25 post-H6.B Phase 0 audit. Next revision
after H6.B fix lands on the Bybit panel + D5 clock restarts.*
