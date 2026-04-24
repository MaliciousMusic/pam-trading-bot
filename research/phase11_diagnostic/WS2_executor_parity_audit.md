# WS2 — Executor Parity Audit

**Audit type:** Forward code trace + asymmetry inventory.
**Status:** Complete (read-only; no empirical run required).
**Date:** 2026-04-24.
**Branch / base commit:** `feat/tensor-engine` @ `9bd1d86` (post-WS4).
**Companion docs:** `PLAN.md` §Q3, §4 (WS2 deferred framing), `WS5_harvest_path_audit.md` §3 (sprint-vs-live calibrator wiring), `WS3_R2_step_A_attribution.md` §1 (paper/shadow mirror discipline).

---

## 0. Headline + reframe

**PLAN §Q3 frames WS2 as "shadow-vs-main executor fill/slippage/queue behaviour diff." This framing is partially a misnomer.** There is **no shadow *executor***. There is `PaperExecutor` ([paper.py](src/execution/paper.py), 1113 LOC, the council-portfolio executor) and there is `TribeShadowLedger` ([shadow.py](src/execution/shadow.py), 672 LOC, **per-tribe isolation ledgers**). The shadow ledgers are not a mirror of the council executor — they are 24 independent paper accounts, one per tribe, each driven by that tribe's solo vote rather than by the council forecast. They share *exit-layer semantics* with PaperExecutor (R2 commit A explicitly enforces this — paper and shadow are mirrored when fix touches both), but their *fill-price model* and *risk-rail layer* are deliberately, structurally simpler.

**The actual parity question — sharper than the PLAN framing**: do the per-tribe shadow ledgers' realized PnL numbers fairly represent production trading economics, given that those numbers feed `TribeTradingScorer` ([tribe_scorer.py:103](src/audit/tribe_scorer.py:103)) → `top_quartile_tribes()` → the harvest-mass boost at [chess.py:721-723](src/projector/chess.py:721) AND `trading_multiplier(tid, side)` → council vote-weighting via the calibrator?

**Answer: no.** Shadow's fill model is structurally friendlier than `PaperExecutor` whenever the production executor runs with: (a) book-depth-shaded fills (any L2 feed present); (b) Phase 10-Harden overlay enabled (sqrt-slippage, adversarial-vol fill, fat-tail latency queue); (c) book-depth size cap (`book_depth_frac_cap`); (d) slippage cap; (e) autonomous black-swan detector; (f) funding accrual. **Shadow is missing all six.** Aggregate bias direction is positive — shadow PnL is upwardly biased vs. production. Magnitude is unknown without instrumentation, but propagates into both consumers.

**Per WS5 finding**: the harvest-mass amplification path doesn't fire in sprint (calibrator None), so this entire asymmetry is **live-only**. Sprint runs *do* use the shadow model for tribe attribution but the resulting `top_quartile_tribes` set is unused (no calibrator to read it). The bias is consequential exactly when the path is consequential — in production.

---

## 1. Antecedents

- **PLAN §4 (deferred framing)**: "Original WS2 framing was 'decision engine confidence / entry calibration.' WS3 subsumes most of that work (H1 is literally a decision-engine confidence question). After WS3 ships, revisit whether a separate WS2 remains, or whether the remaining scope is already absorbed into WS3 Option A/B/C." This audit confirms WS3 absorbed *zero* of the executor-parity scope; H1/H2/H3 are projector-and-confidence concerns and never touch `paper.py` / `shadow.py`. The relabeling at PLAN §Q3 ("executor parity audit") is the residue WS3 didn't absorb.
- **WS3_R2_step_A_attribution.md §1**: explicitly notes the paper/shadow mirror discipline — "src/execution/paper.py (Layer 2b TP, stop dividing by boost), src/execution/shadow.py (mirror)". Confirms exit-layer parity is a **maintenance contract**, not an emergent property; future commits touching one must touch the other.
- **WS5_harvest_path_audit.md §3**: established sprint cannot fire the harvest-mass path because TribeCalibrator is None in sprint. By extension, the WS2 bias documented here is sprint-irrelevant and live-only.
- **WS3_H1_SCOPE.md §11 parallel-workstream policy**: this audit is read-only, no schema changes, parallel-safe with the in-flight H1 full-day determinism gate.

---

## 2. Asymmetry inventory — six classes

### 2.1 Fill-price model: depth-blind vs. book-shaded

| | `PaperExecutor._fill_price` ([paper.py:651-738](src/execution/paper.py:651)) | `TribeShadowLedger._fill_price` ([shadow.py:359-365](src/execution/shadow.py:359)) |
|---|---|---|
| Half-spread charge | `self.half_spread_bps` | `self.half_spread_bps` (parity) |
| Depth model | `5 · size_usd / side_depth_5bps_usd` when L2 present, else size-scaled fallback (`slippage_bps · (1 + slippage_size_k · size/ref)`) | Constant `self.slippage_bps`, **size-blind**, **L2-blind** |
| Sqrt overlay (Harden Layer 3) | `1 + sqrt_k · sqrt(size/ref)` when enabled | **Absent** |
| Adversarial-vol (Harden Layer 1) | `adv_mult · realized_vol_bps + adv_floor_bps`, capped | **Absent** |
| Slippage cap | `slippage_cap_bps` clamps total | **Absent** |

**Claim**: for the same `size_usd`, `mark`, `side`, and market state, `shadow_total_bps ≤ paper_total_bps` whenever any of the following is true: L2 depth is finite (book-depth term ≥ 0); harden enabled (overlay + adv_floor add ≥ 0); slippage exceeds the cap (paper clamps, shadow ignores). In production with L2 + harden enabled, the shadow underprices fills systematically by tens of bps on medium-to-large orders.

### 2.2 Latency queue (Harden Layer 2)

| | PaperExecutor | TribeShadowLedger |
|---|---|---|
| Latency draw | Mixture of exponential + Pareto, per intent ([harden.py:185-215](src/execution/harden.py:185)) | **None** — fills land at decision-tick mark |
| Sub-tick absorption | < `tick_seconds` collapses to immediate fill | n/a |
| Supra-tick deferral | Intent queued in `_pending_fills`, fills at later tick's mark with fresh `_fill_price` draw | n/a |

**Claim**: when harden is enabled in production, ~2% of council intents (`p_spike=0.02` Pareto path; mean exponential `0.08 s` rarely crosses 1 s tick) land at a mark that has moved against them. Shadow tribes never experience this — every shadow open fills at the council-tick mark exactly. **Direction of bias**: positive for shadow (no decision-to-fill drift cost).

### 2.3 Book-depth size cap

| | PaperExecutor | TribeShadowLedger |
|---|---|---|
| Cap predicate | `_book_capacity_usd` returns `book_depth_frac_cap × side_depth_5bps_usd` ([paper.py:743-752](src/execution/paper.py:743)); intents above are throttled | `max_total_notional_pct=200%` of equity — equity-based, not book-based |
| L2 awareness | Yes — checks `bid/ask_depth_5bps_usd` per side | No — never reads L2 |

**Claim**: in production with default `book_depth_frac_cap=0.25`, council orders that would consume > 25% of the visible 5 bps band on the taker side get downsized. Shadow tribes can place `2% × $50k = $1k` orders in the worst case (small) but the *fraction* of book they'd consume is never checked. Practically: shadow rarely runs into this because shadow notional is tiny. But the per-tribe size scaling at [shadow.py:331-339](src/execution/shadow.py:331) doesn't know about the book at all, so any future config bump to `size_base_pct` would silently bypass the production cap.

### 2.4 Autonomous black-swan detection

| | PaperExecutor | TribeShadowLedger |
|---|---|---|
| Detector | Internal — `black_swan_sigma` rolling-Δ vs. ref-σ ([paper.py:130-146](src/execution/paper.py:130)); fires hard-close + cooldown | **External flag only** — `on_tick(..., black_swan_active=False)` parameter ([shadow.py:267](src/execution/shadow.py:267)); shadow doesn't compute the detector itself |
| Behavior on fire | Closes all positions at mark, suspends new opens for `cooldown_s` | Same close behavior, but only when caller sets the flag |

**Claim**: in production, the engine wires the council executor's autonomous detector and *propagates* `black_swan_active` to shadows ([core/engine.py](src/core/engine.py) wires this — confirmed in earlier WS5 trace). So the asymmetry is mechanically resolved by the wiring. **But** if the wiring breaks, shadows stop respecting black-swan halts independently. Latent fragility, not current bug.

### 2.5 Funding accrual

| | PaperExecutor | TribeShadowLedger |
|---|---|---|
| `apply_funding(rate, dt)` | Yes — accrues per-tick funding on every open position ([paper.py:514](src/execution/paper.py:514)) | **Absent** — grep confirms zero `funding` references in `shadow.py` |

**Claim**: shadow PnL excludes funding cost. For positions held across funding epochs, shadow systematically over-credits longs (when funding > 0, longs pay) and undercharges shorts during reverse-funding. Aggregate direction depends on funding-rate distribution + tribe-side mix; not unidirectional but is a known omission.

### 2.6 Fee/slippage defaults

| | PaperExecutor default | TribeShadowLedger default |
|---|---:|---:|
| `fee_rate_bps` | **5.0** | **2.0** |
| `slippage_bps` | 2.0 | 2.0 |
| `half_spread_bps` | 0.5 | 0.5 |

**Claim**: shadow defaults to a 2.5× cheaper fee. In practice both are config-driven (the YAML overrides the defaults), so the production executor and shadow ledgers should agree at runtime — but the divergent defaults mean a misconfigured run silently advantages shadow by 3 bps per round-trip (6 bps round-trip on a small position). This is the kind of asymmetry that doesn't bite until someone deletes a config block.

---

## 3. Exit-layer parity — six layers, mirrored

The exit machinery is the **good** part of the parity story.

| Layer | PaperExecutor | TribeShadowLedger | Parity |
|:-----:|:--------------|:------------------|:-------|
| 0 — hard SL floor (% of equity) | `paper.py:425-436` | `shadow.py:454-463` | ✅ identical predicate; shadow's `max_position_loss_pct` derived from `panic ∈ [0,1] → 1.5–0.5%` instead of explicit config |
| 1 — sign flip + confirm streak | `paper.py:438-457` | `shadow.py:465-486` | ✅ identical predicate, `strong_flip_conf` and `weak_flip_confirm_ticks` both wired through |
| 2 — profit ratchet | `paper.py:462-471` | `shadow.py:490-500` | ✅ identical, `_peak_bps` per-position dict on both |
| 2b — forecast-target TP | `paper.py:473-488` | `shadow.py:502-515` | ✅ identical predicate; **R2 commit A** kept the de-boost removal symmetric (per `WS3_R2_step_A_attribution.md` §1) |
| 2c — shape-aware exit | `paper.py:490-499` | `shadow.py:517-526` | ✅ identical; `_peak_favorable_seconds` re-implemented in shadow with the same boost-divide so curve interpretation matches |
| 3 — edge exhausted | `paper.py:501-512` | `shadow.py:528-540` | ✅ identical |

The **maintenance contract** here is that the two implementations are kept aligned commit-by-commit. R2 commit A is the most recent enforcement event; before that, every Phase 10 exit-layer landing also touched both. The risk is **silent drift over time** — there is no test that mechanically asserts predicate equality between the two — but there is no current divergence to flag.

---

## 4. Consequence chain — where the asymmetry bites

```
TribeShadowLedger._close_position
    → _ShadowTrade record (uses shadow's friendlier fill model)
    → drained at engine.py:363 every tick
    → TribeTradingScorer.ingest_closed (tribe_scorer.py:103)
    → updates per-tribe EWMAs (hit_ewma, bps_mean_ewma, realized_bps_ewma, ...)
    → composite net_score (edge × 0.40 + sharpe × 0.25 + drawdown × 0.15 + ...)
    → top_quartile_tribes()  (tribe_scorer.py:207)
        ↓
        chess.py:721-723 — read into harvest-mass boost
        ↓
        if tid in top_quartile: mass *= hq_boost  (chess.py:730)
        ↓
        harvest_mass_ratio drives conviction_boost ∈ [1.0, 1.5]
        ↓
        decision/engine.py:142 conv_mult — sizes the council position up
    → trading_multiplier(tid, side)  (tribe_scorer.py:182)
        ↓
        calibrator.trading_multiplier delegates here  (calibrator.py:451)
        ↓
        folded into council vote-weighting machinery
```

**Two distinct consumers of biased shadow numbers:**

1. **Harvest-mass top_quartile**: tribes whose shadow PnL ranks in the top 25% by composite score get their pool weight inflated (`hq_boost`, configured 1.5–2.0×) when classified contrarian. Friendlier shadow fills boost ranking → wrong tribes get top-quartile status → wrong tribes get the harvest amplification.
2. **Per-tribe trading_multiplier**: the per-tribe EWMA hit-rate maps to a `[max_shrink, max_boost]` multiplier (default `[0.6, 1.4]`). Shadow's friendlier fills preserve marginal trades that production would have stopped out → hit-rate EWMA biased upward → multiplier biased upward.

**Sprint scope (WS5 finding)**: the calibrator is None in sprint, so:
- `trading_multiplier`'s consumer-side at `calibrator.trading_multiplier` returns 1.0 in sprint (calibrator.py:448, `if self._trading_scorer is None`)
- `top_quartile_tribes` is read at chess.py:721 via `getattr(self.calibrator, "_trading_scorer", None)` which returns None in sprint
- Net: shadow ledgers run in sprint and accumulate `_ShadowTrade` records, but the records have no consumers. The bias is dormant in sprint.

**Live scope**: both consumers are wired; both are biased upward by shadow's friendly fill model. Magnitude unknown.

---

## 5. Verdict and recommendations

### 5.1 Verdict

- **Exit-layer parity is OK** by maintenance contract — six layers mirrored, R2 commit A is the most recent alignment event.
- **Fill-economics parity is broken structurally**, not by drift, by deliberate simplification in `TribeShadowLedger`. Shadow trades are systematically cheaper to enter and exit than production council trades whenever (a) L2 is present, (b) harden is enabled, (c) latency would have queued the order, (d) book-depth would have throttled it, (e) slippage would have been capped. All of these are normal production conditions.
- **Bias is unidirectional positive** — shadow PnL > production PnL on the same trade.
- **Bias is dormant in sprint** (calibrator None, no consumers) and **live-only** in impact.
- **Magnitude is unknown.** A reasonable lower bound: the harden snapshot's `avg_slip_bps` minus shadow's `slippage_bps + half_spread_bps` constant. A reasonable upper bound: the same plus harden's `adv_floor_bps + adv_mult × realized_vol`. No empirical numbers from a side-by-side run yet.

### 5.2 Recommendations

1. **Do not block H1 on this.** The bias affects the harvest-mass amplification, not the confidence formula directly. H1's hypothesis space (A/B/C) is structurally upstream; WS2 bias only affects sizing magnitudes (via `conv_mult`), not the structural 0.25 cap.
2. **Instrumentation extension (sequencing flag for post-H1.I3):** when H1.I3 lands the conviction_boost / harvest_mass_ratio plumbing, add a parallel field to `_ShadowTrade`: a "would-be-paper-fill" total_bps computed via the same `_fill_price` machinery PaperExecutor uses, against the same MarketState. Every shadow close logs both `realized_bps` (what shadow charged) and `realized_bps_paper_model` (what paper would have charged). The delta is the WS2 bias, per-trade, directly measurable.
3. **Defer any "promote shadow to use paper's fill model" change until measurement.** The cheap thing structurally is to delete shadow's `_fill_price` and call `paper.PaperExecutor._fill_price` as a static method against a synthetic MarketState. But: shadow's fill model is intentionally simple to keep `_ShadowTrade` cheap (24 tribes × every tick = a lot of work). Decide after seeing the magnitude.
4. **Add a static-analysis or test-level assertion that the six exit-layer predicates are textually paired.** Light-touch — a unit test that imports both modules, walks the layer-by-layer predicates, and fails if they diverge on a fixed input. Cheap insurance against silent drift.
5. **Document the fee-default asymmetry as a config-correctness checklist item.** A YAML that omits the `shadow_ledgers.fee_rate_bps` block produces a 5→2 bps fee gap. Either harmonize the defaults (set shadow default to 5.0) or add a `validate_config` warning on default-mismatch. Cheap and obvious.
6. **Consider making `apply_funding` symmetric.** Funding cost is real; shadow's omission flatters every long-side tribe in positive-funding periods. Light implementation lift; biggest risk is performance (24 ledgers × every funding tick). Defer until measurement shows funding cost is a meaningful fraction of `realized_bps_ewma`.

### 5.3 Priority recommendation

**Post-H1.I3, alongside the harvest-mass live-tape measurement (WS5 §5.2.3).** The two analyses share a measurement surface (per-tribe trading data) and the WS2 bias number is the right denominator for interpreting WS5 numbers. If WS2 bias is small (<5% of realized_bps), WS5's harvest-mass measurement can ignore it. If WS2 bias is large (>20%), WS5's findings need a debiasing correction before any decision about removing the harvest path.

### 5.4 Scope discipline note

Per `WS3_H1_SCOPE.md` §11, this audit made zero code changes. The instrumentation extension proposed in §5.2.2 is deferred to a post-H1.I3 plumbing chip that lands alongside (or shortly after) the WS5 live-tape measurement extension. The static-analysis test in §5.2.4 is lightweight enough to land at any time, but is not on the critical path.

---

*Filed in service of: PLAN §Q3 closure with reframe (no shadow *executor* exists; the parity question is shadow-ledger fidelity), WS5 live-tape measurement prerequisite (knowing WS2 bias bounds the WS5 numbers), TribeTradingScorer + harvest-mass consumer documentation.*
