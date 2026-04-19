# PAM Bot — State of the Project & Roadmap (Phases 10–14)

_Date: 2026-04-20 · Branch: `feat/multi-layer-exit-engine`_

---

## 1. Where we are today

### Architecture (shipped)

- **17-tribe council** — each archetype votes with `(side, conviction, weight, horizon)`; `weight` is the tribe's flow-share ω_i.
- **Chess projector** — 50-step forward simulation, per-tribe isolated walks producing wiggly paths.
- **Adaptive Temporal Weighting** (PR #18):
  - Persistent per-tribe shock buffer — True Markov Slide, terminal slot redrawn each tick.
  - Prediction ledger — circular buffer of the last N frames, used each tick to score per-tribe per-horizon accuracy.
  - Per-(tribe, horizon) weight matrix `W[i,h]` ∈ [0.1, 1.5].
  - Peer-median competitive normalizer — you beat your own council's median error or you lose weight.
  - Zero-sum column renormalization — `Σ W[:,h] ≡ n_tribes`.
- **Regime-dependent pool weights** (this session, not yet committed):
  `ω_eff = ω_base · f(regime, category)` — 6 regimes × 6 categories. MM throttled to 0.35× in cascades, arb boosted to 1.60×, etc.
- **Global Projection formula**:
  `GP_h = Σ(Path_{i,h} · W_{i,h} · ω_{i,eff}) / Σ(W_{i,h} · ω_{i,eff})`
  — flow-share × regime overlay gates amplitude; accuracy heatmap gates who gets trusted at each horizon; consensus is a true probability-weighted average.
- **Decision engine** — portfolio-based, TP/SL/horizon exits, shape-aware peak-favorable exit.
- **Executor** — paper fills, no slippage model, no book-impact, no capacity check.
- **Regime classifier** — chop / normal / trend_up / trend_down / cascade_up / cascade_down.
- **Calibrator** — per-regime skill-multiplier EWMA.
- **Auditor / backscorer** — per-horizon residual σ feeds the projector's uncertainty fan.
- **UI** — council wheel (hue-sorted), wiggly forecast, 17×50 accuracy heatmap (live, hue-sorted), portfolio + trade history, equity sparkline, regime banner, tick-inspect modal. Responsive to ≥1060px.
- **Replay engine** — `WatchEngine` + broadcaster, multi-variant concurrent sparkline panes, click-a-bar tick inspect, .zip report with HTML/SVG/Markdown.
- **Phase 9 variant week experiment** — 6 hand-picked config variants run concurrently with per-variant equity carry-over.

### Known gaps (from council-redundancy memo, 2026-04-18)

- 3 TA tribes (US / EU / Asia) produce near-identical signals.
- No microstructure / L2 / HFT-proxy tribe — all 17 archetypes are OSINT-visible.
- No orthogonal on-chain input (whale flow, miner-to-exchange, stablecoin mint).

### Risk posture

- **No drawdown circuit breaker.**
- **No hard black-swan price stop.**
- **Fills assume infinite liquidity** — Phase 9 Sharpe numbers are optimistic.
- **No capacity check** — an order larger than the book would pretend to fill at mid.

---

## 2. Phases 10 → 14

### Guiding principles

- **Free data only** — no paid APIs until budgets are discussed.
- **Vision-first, no MVP framing** — we're building toward a specific system, not a generic template.
- **Don't force standard patterns** — phases should integrate with the PAM architecture, not replace it with generic GA/RL boilerplate.
- **No premature HFT hardening** — toxic-flow masking, tempo randomization, iceberg-order splitting are *live-execution* concerns. We're paper-only. Defer indefinitely.

---

### Phase 10 — Risk Guardrails *(~1 day)*

**Goal:** nothing ships toward live capital until the bot can stop bleeding on its own.

**Scope**

1. **Max-drawdown circuit breaker**
   - Track session peak equity; compute live DD.
   - New setting `risk.max_session_dd_pct` (default 8%).
   - When crossed: `PaperExecutor` refuses new opens, closes existing positions at mark, raises a `HALTED` banner on the UI. Manual unlock only.

2. **Black-swan hard stop**
   - Rolling 30-second price-move monitor on `MarketState`.
   - New setting `risk.black_swan_sigma` (default 5.0) — multiple of recent 5-min σ.
   - When `|Δprice_30s| > N·σ_5m`: close-all + 10-minute cooldown, council votes are ignored during cooldown.

3. **Per-trade hard SL floor**
   - Cap any TP/SL bracket so the worst-case per-position loss is ≤ `risk.max_position_loss_pct` of current equity (default 1%).
   - Overrides projector's peak-favorable shape exit if it would exceed the floor.

**Files touched**

- `src/execution/paper_executor.py` — breaker state, order-refusal path.
- `src/core/engine.py` — black-swan monitor hook.
- `src/core/settings.py` — new `risk` block.
- `src/ui/static/index.html` — HALTED banner.

**Done when:** replay test with a synthetic −10% 1-min candle shows: hard stop fires within 1 tick, all positions flat, cooldown observed, UI banner lit, no new opens in the window.

---

### Phase 11 — Realistic Execution *(~2 days)*

**Goal:** Phase 9 Sharpe numbers you can actually trust.

**Scope**

1. **Book-depth-shaded fills**
   - Fill price = mid ± `f(size_usd, side_depth_5bps_usd)`.
   - Linear-within-5bps-band model: `slippage_bps = 5 · size_usd / side_depth_5bps_usd`, capped at `slippage_cap_bps` (default 30).

2. **Capacity cap**
   - Reject or clip orders when `size_usd > capacity_frac · side_depth_5bps_usd` (default 0.25).
   - Emits a `CAPACITY_CLIPPED` event on the bus; visible in the UI trade history as an attribute.

3. **Spread + fee model**
   - Half-spread charge on entry and exit (default 0.5 bps each side).
   - Taker fee (default 2 bps) — realistic for Binance perps retail tier.

4. **Replay report augmentation**
   - Per-trade: intended_bps vs realized_bps, slippage cost, fee cost.
   - Aggregate panel: total slippage drag, total fee drag, "frictionless-equivalent" equity curve alongside the real one.

**Files touched**

- `src/execution/paper_executor.py` — fill model.
- `src/core/settings.py` — `execution.slippage` block.
- `src/replay/report.py` — new breakdown table + second equity curve.
- `src/ui/static/index.html` — trade row tooltip shows slippage/fee split.

**Done when:** a Phase 9 variant re-run produces a materially different Sharpe (we expect 10–30% lower), and the frictionless curve matches the old numbers within 1%.

---

### Phase 12 — Silent Tribe *(~2–3 days)*

**Goal:** close the orthogonality gap from the 2026-04-18 redundancy memo. Today's 17 archetypes are all OSINT-visible; microstructure is dark matter.

**Scope**

1. **`microstructure_prober` tribe** — reads L2 book + aggTrade stream. Inputs:
   - **Order-book imbalance** over top-5bps — a leading indicator of short-horizon drift.
   - **Queue-position decay** — how fast top-of-book depth is being refreshed (HFT presence indicator).
   - **Iceberg detection** — repeated fills at the same price with no displayed size change (classic iceberg signature from aggTrade).
   - **Trade-size clustering** — normalized entropy of recent trade sizes; low entropy = algo-dominant tape.

2. **Tribe category** — add `microstructure` to `_TRIBE_CATEGORY` / `_REGIME_CATEGORY_MULT`. Multiplier design:
   - chop: 1.40 (owns this regime)
   - normal: 1.15
   - trend: 0.95
   - cascade: 1.25 (icebergs eat cascades)

3. **Redundancy check** — run correlation matrix of per-tribe `vote_bps` over 1 replay week. Target: `|ρ(microstructure, *)| < 0.4` against all existing tribes.

4. **UI** — add the tribe to the council wheel and heatmap; it inherits all existing wiring automatically.

**Files touched**

- `config/tribes/microstructure_prober.yaml` — new.
- `src/tribes/strategies.py` — new strategy family.
- `src/tribes/market_state.py` — expose L2 queue-decay + aggTrade entropy features.
- `src/projector/chess.py` — category mapping entry.

**Done when:** tribe loads, shows up in UI wheel and heatmap, passes the `ρ < 0.4` orthogonality check against all 17 existing tribes on a 1-week replay.

---

### Phase 13 — Evolutionary Variant Search *(~3–4 days)*

**Goal:** promote Phase 9 (hand-picked variants) into a real mutation + selection loop. Seed-bank guards against genetic drift.

**Scope**

1. **Mutation engine**
   - Input: top-K variants from last generation's Sharpe leaderboard (K=2).
   - Output: N child configs (default N=6) = each parent cloned with ±5% Gaussian jitter on numeric params (conviction thresholds, hold_seconds, TP/SL multiples, pool weights).
   - Categorical params (strategy family, symbol) never mutate.

2. **Selection cut**
   - End-of-week: rank all variants by `Sharpe · sign(equity_growth) · (1 − MDD)`.
   - Kill bottom 50%; top 50% become generation-(g+1) parents.

3. **Seed bank (immutable variant_0)**
   - `config/tribes/*.yaml` = generation-0 baseline, *never* mutated, *always* runs as one variant.
   - If the evolved cohort underperforms variant_0 for 2 consecutive weeks: flag a `GENETIC_DRIFT` warning in the report.

4. **Within-tribe mutation only**
   - No cross-tribe "mating" — schema mismatch would produce garbage. Evolution lives inside each tribe's param space.

5. **Run ledger**
   - `config/tribes_variants/<week>/variant_<id>/` — extend existing layout with `lineage.yaml`: `parent_id`, `mutated_fields`, `generation`.

**Files touched**

- `src/experiments/mutation.py` — new.
- `src/experiments/selection.py` — new.
- `src/experiments/week_runner.py` — existing Phase 9 runner, wire in mutation/selection.
- `src/replay/report.py` — lineage column + genetic-drift alert.

**Done when:** a 3-generation replay shows (a) children distinct from parents in param space, (b) bottom-50% kill working, (c) seed-bank baseline always present, (d) drift alert fires in a rigged replay where parents are seeded worse than the baseline.

---

### Phase 14 — Adversarial Replay *(~2 days)*

**Goal:** training the council's immune system against spoof attacks.

**Scope**

1. **Spoof-injection fixtures**
   - `tests/fixtures/spoofs/`:
     - `fake_bullish_sentiment_then_dump.json` — FGI/news spike followed by −3% move.
     - `liquidation_bait.json` — fake cascade signal, price reverses.
     - `whale_wall_pull.json` — large bid appears then disappears in <2 ticks.
   - Loaded by the replay engine as patches over a real replay window.

2. **Skepticism score**
   - Per-tribe: fraction of spoof fixtures where the tribe voted *against* the bait direction (correctly).
   - Feeds into the calibrator as a prior on `skill_multiplier` — chronically-fooled tribes get a starting malus in future sessions.

3. **Report panel**
   - New section in the .zip replay report: "Adversarial Fitness" — per-tribe spoof-recovery scores + council-level baseline.

**Files touched**

- `tests/fixtures/spoofs/*.json` — new.
- `src/replay/watch_engine.py` — spoof-patch loader.
- `src/decision/calibrator.py` — skepticism prior.
- `src/replay/report.py` — adversarial panel.

**Done when:** the 3 fixtures run end-to-end, per-tribe skepticism scores are logged, calibrator prior moves measurably for fooled tribes.

---

## 3. What we explicitly skip

Taken from Gemini's memo, deferred or rejected:

| Item | Verdict | Why |
|---|---|---|
| Toxic-flow masking, noise trades, tempo randomization | **Skip** | Paper bot at $14k isn't on any MM's radar. Live-execution hygiene, premature. |
| Cross-tribe "mating" | **Skip** | Heterogeneous param schemas — produces nonsense. Within-tribe mutation is the honest version. |
| Iceberg-order splitting for our own orders | **Skip** | Our capacity cap (Phase 11) keeps orders within book depth. Splitting only matters above the Goldilocks ceiling. |
| $100k withdrawal policy | **Skip** | Personal finance, not engineering. |
| ESMA "transparency gene" | **Skip** | We already log `reason` + `sample_paths_tribe_ids`. Covered. |
| "2% daily" goalpost | **Skip** | Correct warning from Gemini but not a build item. |
| Adverse-selection modeling | **Defer** | Only material at live latencies. Revisit when paper → live switch is on the table. |

---

## 4. Ordering & dependencies

```
Phase 10 (risk)  ──►  Phase 11 (execution)  ──►  Phase 13 (evolution)
                                             ├─►  Phase 12 (silent tribe)
                                             └─►  Phase 14 (adversarial)
```

- **10 before 11** — slippage model without a DD breaker is strictly more dangerous than today.
- **11 before 13** — evolutionary selection on unrealistic Sharpe numbers breeds overfitting.
- **12 and 14 parallelizable** after 11 — different parts of the tree.
- **13 depends on 11 only** — mutation needs honest fitness numbers.

Total expected effort: **~10–12 working days** if sequential; ~7–8 if 12 + 14 parallelize after 11.

---

## 5. Open questions to resolve before Phase 10 kickoff

1. **`risk.max_session_dd_pct` default** — 8% proposed. Too tight? Too loose for a 6-hour session?
2. **Manual unlock or auto-reset** at start of next session? Proposal: manual — forces post-mortem.
3. **Black-swan cooldown duration** — 10 min proposed.
4. **Slippage cap in Phase 11** — 30 bps proposed. Too aggressive for a 500k+ book?
5. **Generation cadence in Phase 13** — week proposed (matches Phase 9). Consider per-day for faster iteration, but statistical noise gets worse.

Answer these at Phase 10 kickoff. Defaults above are my best guesses; all of them are single-line config changes.
