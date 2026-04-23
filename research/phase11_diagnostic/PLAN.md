# Phase 11 Diagnostic — PLAN

**Branch:** `feat/tensor-engine`
**Last updated:** 2026-04-23
**Status:** WS3 H2 Option 1a attribution + live-feedback cycle complete;
measurement infrastructure documented (`MEASUREMENT_INFRA.md`). R2
(`conviction_boost` relocation) is queued behind a `MAX_GAIN` ceiling
decision and the spawned 2026-04-21 divergence audit. See §5 Master
checklist for the forward map — the §1 workstream table below is an
archival index as of 2026-04-22.

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
| R2 — `conviction_boost` relocation to `DecisionEngine` | ▶ Ready — gates cleared | Both blockers resolved (MAX_GAIN confirmed `26cb61e`; audit delivered 2026-04-23). Landing sequence per [`WS3_R2_step_A_attribution.md`](WS3_R2_step_A_attribution.md) §4. Framed as **measurement hygiene, not directional fix**; algebraic expectation **0.00 pp** directional delta (boost is a positive scalar on signed pred — cannot flip sign); condition-(a) 3 pp pause trigger serves as a refactor-bug safety rail, not a mechanism-change gate. |
| MAX_GAIN ceiling decision | ✅ Confirmed 2026-04-23 | Option (ii) — ceiling stays at 40, WS3 H-measurement reads live tape — accepted per [`MEASUREMENT_INFRA.md`](MEASUREMENT_INFRA.md) §4.1 (commit `26cb61e`). §4.1.1 revisit triggers in place (live gain sustained >20 for 72 h, sprint pressure root-caused, or structural projector change invalidating the 1.57 baseline). |
| 2026-04-21 projector divergence audit | ✅ Delivered 2026-04-23 | Verdict in [`proj_divergence_20260421_audit.md`](proj_divergence_20260421_audit.md): **conjunction root cause** — (1) 2h 54m tape gap 08:44→11:39 UTC, (2) first post-gap `forecast_conf` 0.245 (~27× 04-20's equivalent), (3) `retail_dna` panic branch (`chase = 4·Δ` geometric + `overshoot = 0.0004·state²` quadratic), (4) `mean_reversion_rate = 0.005` too weak to damp the quadratic, (5) `ForecastAuditor` 3600 s sigma window perpetuates contamination. Sprint does **not** reproduce (max \|emb\| 27 bps vs live 8.86e+300). Fix ladder: **A → C → B** (audit §4 attribution discipline), defer D/E. No production code changed. |
| 2026-04-21 panel inclusion | ⏸  Quarantined 08:44→14:00 UTC | Diagnostic-only until Fix A lands and 04-21 re-replays clean. Quarantine widened past the audit's 13:00 UTC recommendation to absorb one full `ForecastAuditor` sigma window (3600 s) past the last explicit contamination. Re-admit under `rebaseline_date` marker in [`WS3_H2_option1a_snapshot.md`](WS3_H2_option1a_snapshot.md) §5. |

### 5.1.1 Post-R2 fix queue (from 2026-04-21 divergence audit)

| Fix | Scope | State |
|:----|:------|:------|
| **A — per-step `step_bps` clamp + counter** | `chess.py:~580`; cap `step_bps` to ±400 bps (5× the largest legitimate single-step observed on 04-18/19/20). Loud counter + pre-clamp value log — clamp must not be silent. Protects against any DNA runaway, not just retail. | ⏸ Queued post-R2 |
| **C — anchor-enqueue sanity check** | `engine.py::_compute_predictions`; skip anchor if \|emb\| > 2000 bps or non-finite; counter `forecaster.anchor_dropped`. Defense-in-depth behind Fix A. | ⏸ Queued post-Fix-A |
| **B — retail quadratic → tanh saturator** | `behavioral_dna.py::retail_dna`; replace `0.0004 · state²` overshoot with `40 · tanh(state/400)` saturator; cap `chase` at `min(4·prev_delta, 200)`. Mechanism change — requires full WS3 H2 re-evaluation. | ⏸ Conditional — lands only if Fix A's counter fires on 04-18/19/20 clean days |
| **D — projector state reset on gap** | Flag-gated ForecastAuditor window truncation + conviction-history warm-start reset + cold-start calibrator for one projection after gap > 300 s. | ⏸ Deferred — re-scope after A+B+C attribute cleanly |
| **E — strengthen `mean_reversion_rate`** | YAML bump `0.005 → 0.015` or horizon-aware schedule. One-line change but interacts with WS3 H2 consensus fit (the 0.02 → 0.005 drop was motivated). | ⏸ Deferred — WS3 R2 candidate with own attribution sprint |

Attribution discipline (per audit §4): Fix A → re-replay 04-21 → Fix C → re-replay → Fix B (if counter-promoted) → re-replay. One commit per mechanism, determinism gate between each. No bundling.

### 5.2 Queued — post-R2 (forward map)

Phase 11 reopens the research backlog once R2 lands cleanly. No new
chips until R2 closes. Order is not fixed; H1 opens first by default
(D1 causal ordering).

| # | Workstream | Scope sketch | Gate |
|:--|:-----------|:-------------|:-----|
| Q1 | **H1 — confidence-formula compounding** | Measure whether `signal_quality × directional × diversity_mult` structurally caps confidence < 0.25 regardless of the underlying signal. Scope doc first (`WS3_H1_SCOPE.md`), options A/B/C per §3. | Scope-first; requires explicit go-ahead after scope review. |
| Q2 | **H3 — tribe correlation / N_eff** | Measure effective rank of 24-tribe vote matrix; diversity_mult inflation quantified. | Opens after H1 converges or nulls. |
| Q3 | **WS2 — executor parity audit** | Shadow-vs-main executor fill/slippage/queue behaviour diff. Deferred since WS1; revisit scope vs. what WS3 absorbed. | Post-H1/H3. |
| Q4 | **WS4 — regime classifier hysteresis** | WS2.5 exposed regime sensitivity; classifier stability across 5-60 s horizons is a raised-priority follow-up. | Post-H1/H3. |
| Q5 | **WS5 — harvest boost ablation** | Remove the harvest-tribe amplification path; measure delta. | Post-H1/H3. |
| Q6 | **WS3b — original WS3 warmup / persistence** | Weakness #4 from the original WS3 scope (persistence-weighted anchor horizons, warmup behaviour) reinstated as its own chip. | Post-H1/H3. |
| Q7 | **WS7 — L2 microstructure data feed** | H4 escalation: only if H1 A/B/C + H3 each null against the 52 % bar. Paid-data conversation per `project_budget_constraint.md`. | Conditional — H1/H3 nulls. |
| Q8 | **Phase 11 Retrospective** | Consolidate findings → phase close doc; recommend next phase. | Terminal. |

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

**Unblocked — R2 ready to execute.** Both prior gates have cleared:

1. **MAX_GAIN ceiling** — ✅ Option (ii) confirmed per
   [`MEASUREMENT_INFRA.md`](MEASUREMENT_INFRA.md) §4.1 / commit `26cb61e`.
2. **2026-04-21 projector divergence audit** — ✅ delivered
   ([`proj_divergence_20260421_audit.md`](proj_divergence_20260421_audit.md)).
   04-21 quarantined 08:44→14:00 UTC; Fix A queued post-R2 to unblock
   re-admission.

**Forward sequence:**

1. **R2 commit A** — `conviction_boost` removed from `ChessProjector`
   + four compensating de-boost sites simplified. Attribution per
   [`WS3_R2_step_A_attribution.md`](WS3_R2_step_A_attribution.md) §4.
   Algebraic expectation **0.00 pp** directional delta; the condition-(a)
   3 pp pause trigger is a refactor-bug safety rail, not a
   mechanism-change gate.
2. **R2 commit B** — `conviction_boost` read in `DecisionEngine` as a
   sizing multiplier, Path-Integrity clamp interaction verified.
3. **Fix A** — per-step `step_bps` clamp + loud counter (audit §4.A).
4. **Fix C** — anchor-enqueue sanity check (audit §4.C).
5. **Fix A counter readout** on 04-18/19/20 clean days → decide whether
   **Fix B** is promoted (mechanism change, requires WS3 H2
   re-evaluation) or deferred.
6. **Re-replay 04-21 post-Fix-A**; if clean, re-admit to panel under a
   `rebaseline_date` marker in
   [`WS3_H2_option1a_snapshot.md`](WS3_H2_option1a_snapshot.md) §5.
7. **H1 scope doc (Q1)** — default next chip once R2 + Fix A/C land
   clean and the 4-day panel is reconstituted.

Attribution discipline throughout: one commit per mechanism change,
determinism gate between each (`scripts.verify_determinism --day
2026-04-20`), no bundling. See §5 for the full forward map.

---

*PLAN last revised 2026-04-23 post-MEASUREMENT_INFRA. Next revision
after R2 lands (sprint + live attribution).*
