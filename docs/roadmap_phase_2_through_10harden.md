# PAM Bot — Locked Roadmap: Phase 2 → 10-Harden

_Date locked: 2026-04-21 · Branch: `feat/tensor-engine` · Head: `93c8014`_

This doc supersedes `roadmap_phases_10_14.md` (2026-04-20) for the active
work arc. The older doc remains as historical context for what was planned
before the Yeux Ouverts session; most of its "Phase 10 risk" items and
the "Phase 12 silent tribe" are already shipped ahead of schedule.

---

## 0. Context — what just happened

Four commits on 2026-04-21 locked the "Yeux Ouverts" socle:

| SHA | What |
|---|---|
| `0b6e7ad` | Fix `@depth20` futures routing; relax options thresholds; add G3 proxy book |
| `3be5004` | Research analyzers (orthogonality + cluster stability) |
| `9177a36` | UI adaptive Y-axis scaling |
| `93c8014` | Archive pre-fix depth-blind ledgers; start An 0 |

Council state after the socle:

- **8+ independent voices** (up from 5). `microstructure_prober`,
  `put_call_fader`, `options_dealer_gamma` moved from 0% activity to
  active contributors (100% sample rate on the 5-min post-fix pulse).
- **Determinism preserved** — verify_determinism still passes at
  `SHA 2e65f7fe7f61b436` with default flags.
- **An 0** — `data/ledger/` and `data/audit/` empty; background
  `run_bot_ui` accumulating fresh ledger data from 2026-04-21 onward.
- **Replay fidelity closed** — `synth_book=true` exercises
  microstructure-dependent tribes in backtest (opt-in flag,
  default-off preserves baseline SHA).

---

## 1. Locked sequence

```
  Phase 2 (Tensors chirurgicaux)
    │
    ▼
  Phase 2.5 (1s heartbeat tick)
    │
    ▼
  Phase 10-Harden (impitoyable paper)
    │
    ▼
  Stability Gate — 7 An-0 days, Sharpe > 2.0, zero guard alerts
    │
    ▼
  Phase 10-Bridge (LiveExecutor → testnet → tiny mainnet)     [deferred]
    │
    ▼
  Phase G4 (anti-clone: news_fader + retail_app_newbie)       [deferred]
```

**Principle**: no mainnet exposure, no roster expansion, and no further
feature additions until the paper bot proves stability on the frozen
8-voice Yeux Ouverts roster under adversarial conditions.

---

## 2. Phase 2 — Tensors chirurgicaux

**Goal**: cut per-tick latency from ~37 ms baseline by vectorizing the
top-3 hot-spots identified via cProfile.

### Approach

1. **Profile** — run cProfile on a capped sprint (500 ticks, synth_rate=60
   default). Rank functions by cumulative time. Target: the three
   functions that together consume > 60% of tick time.
2. **Vectorize** — rewrite the three in NumPy broadcast form. No numba,
   no JAX — free-threaded 3.14t compat is too thin for those today.
3. **Verify** — re-run verify_determinism after each rewrite. SHA
   regeneration is accepted when:
   - Distribution-level invariants hold (mean pred error, vote-histogram
     shape, final equity on the standard sprint are within published
     tolerance, TBD per commit).
   - Bit-identity across different machines is NOT a requirement for
     Phase 2 commits (numpy reduces are order-sensitive; tolerated).
4. **Document** — each Phase 2 commit updates `docs/perf_baselines.md`
   with the old SHA, new SHA, perf delta, and the semantic-invariant
   check output.

### Candidate hot-spots (pre-profile guesses)

- Forecasting Monte Carlo in `DecisionEngine` (per-path sim loop)
- Per-tribe vote loop in `ChessProjector` (24 tribes × 50 steps)
- `MarketState` rolling micro-stats (top-of-book queue decay, aggTrade
  entropy, recent-trades ring buffers)
- Chess projector fan generation

**Actual targets frozen after cProfile, not before.**

### Exit criteria

- Per-tick latency cut to ≤ 15 ms median on the standard sprint.
- No semantic regression vs 8-voice post-fix baseline.
- Each rewrite commit stands alone (atomic revert possible if issues
  surface later).

---

## 3. Phase 2.5 — 1 Hz heartbeat tick

**Goal**: decouple the tick clock from event arrival so horizon math,
calibrator EWMA windows, and UI cadence stay synchronized with wall time
regardless of market activity.

### Semantics: heartbeat + backpressure cap

- **Heartbeat**: if no events fired in the last 1 s, emit a synthetic
  empty tick at the 1 s boundary. The engine processes it — votes
  degenerate to "no new info", horizons advance, forecasts stay fresh.
- **Backpressure cap**: during event bursts (trade floods in volatile
  bars), never fire more than 1 tick per 1 s. Events in a 1 s window
  batch into one tick emission at the boundary, with the aggregated
  MarketState.
- **Rationale**: fat-tailed bursts shouldn't cost us CPU; quiet
  windows shouldn't let horizon math drift.

### Consequences

- **Replay determinism**: baseline SHA regenerates once at Phase 2.5
  commit (timing restructured). Post-2.5 determinism contract rebuilds
  on the new cadence.
- **Tribe horizons**: tribes with sub-second sensitivity
  (`microstructure_prober`) need to aggregate within the 1 s window.
  Check `_TRIBE_HORIZON_HINT` for any sub-1s declarations — if found,
  raise them to 1s or document why they remain sub-second (will need
  special plumbing).
- **UI polling**: already > 1 Hz. Backpressure cap on the engine side
  means UI fetches may coalesce multiple engine ticks into one frame.
  Acceptable — the UI isn't a trading path.
- **Live vs replay parity**: in live, the 1 s boundary is wall-clock
  UTC. In replay, it's simulated bar-relative time. They match
  semantically but not literally.

### Exit criteria

- Quiet-market sprint (low synth_rate) shows 1 tick/s even when no
  trades fire.
- Volatile-market sprint (high synth_rate) shows exactly 1 tick/s with
  events batched.
- All 24 tribes produce valid votes on the new cadence.
- Calibrator EWMA windows unchanged in effective half-life.

---

## 4. Phase 10-Harden — impitoyable paper

**Goal**: make paper trading as adversarial as a live market would be,
so a "stable" paper bot is not lying to us about live viability.

### Additions to PaperExecutor

1. **Adversarial fill model**
   - **Quote fade**: with probability `p_fade(size, regime)`, the top
     of book steps 1 tier away before your order lands. You fill at
     tier 2 prices.
   - **Partial fill cascade**: when `size_usd > 25%` of 5bp-band depth,
     you sweep the book. Fill price averages top-5 tiers weighted by
     their visible size.
   - **Missed fill on tight limits**: limit orders within 1 tick of
     current price have `p_miss = 0.3` (price snaps back before the
     limit lands in queue).

2. **Realistic latency**
   - Median 50 ms RTT emulation on order submission.
   - Fat tail: `p(rtt > 200 ms) = 5%`, `p(rtt > 500 ms) = 0.5%`
     (matches observed Binance peaks).
   - Price moves during RTT window apply to the fill.

3. **Maker/taker distinction**
   - Orders classified by type at submission.
   - Maker: subject to queue-wait (~ depth at price / flow rate).
   - Taker: immediate fill but pays 2 bps fee vs maker rebate.

4. **Stale-feed guard** (already partially there; formalize)
   - WS heartbeat miss > 5 s → halt new opens.
   - Book spread > 5 bps → halt new opens until it normalizes.
   - Log every guard firing with state snapshot for post-mortem.

### Config surface

New `execution.adversarial` block in `config/settings.yaml`:

```yaml
execution:
  adversarial:
    enabled: true
    quote_fade:
      p_base: 0.15
      p_by_regime:  {cascade_up: 0.45, cascade_down: 0.45, chop: 0.05}
    partial_fill:
      sweep_threshold_frac: 0.25       # of 5bp-band depth
      tiers_to_sweep: 5
    missed_fill:
      tight_limit_ticks: 1
      p_miss: 0.30
    latency:
      median_rtt_ms: 50
      tail_p200: 0.05
      tail_p500: 0.005
    maker_queue_wait_model: "depth_over_flow"
```

Off-switch lets us isolate adversarial effects when debugging.

### Stability Gate — the mainnet barrier

**Bridge work does not begin until all three conditions hold on the
adversarial paper bot:**

| Metric | Threshold |
|---|---|
| **Consecutive green An-0 replay days** | **≥ 7** |
| **Sharpe ratio on that 7-day window** | **> 2.0** |
| **Guard-rail alerts during the window** | **exactly 0** |

Operational notes:
- "An-0 replay day" = replay against a day whose live ledger was
  captured post-`0b6e7ad` (depth-blind ledgers are not eligible).
- "Green" = positive P&L after adversarial fees + slippage.
- "Zero guard alerts" = no peak DD breaker, no daily loss breaker, no
  session DD halt, no per-position floor trigger, no black-swan
  detonation, no stale-feed halt.
- Any violation resets the counter — 7 consecutive is a hard sequence.

Rationale for the barrier height: if the 8-voice council genuinely has
alpha, Sharpe > 2 on adversarial paper is conservative. If it doesn't
hit that bar, we don't want any real-money exposure.

---

## 5. Frozen decisions

| Decision | Locked value | Revisit gate |
|---|---|---|
| Roster size | 8 voices (post-`0b6e7ad`) | Stability Gate passed |
| Anti-clone work (G4) | Deferred | After bridge |
| Tensor lib | NumPy only | Post-Phase-2 benchmark |
| Determinism | Distribution-invariant, not bit-exact | Never (accepted) |
| Tick cadence | 1 Hz heartbeat + backpressure cap | Never (locked) |
| Execution target | Paper only | Stability Gate passed |
| Mainnet scope | None until Gate | Gate pass + post-mortem |

---

## 6. Out of scope for this arc

The following are explicitly NOT part of Phase 2 → 10-Harden:

- **LiveExecutor / mainnet / testnet** — deferred to post-Gate Phase 10-Bridge.
- **Additional tribes** — roster frozen at 8.
- **Phase 13 evolutionary search** — needs honest Sharpe numbers first,
  which 10-Harden provides; it follows Bridge.
- **Phase 14 adversarial replay (spoof fixtures)** — parallel to Bridge.
- **Phase 12 "silent tribe"** — already landed (microstructure_prober).
- **Paid APIs** — free data only, always.

---

## 7. Current status (2026-04-21)

- [x] Yeux Ouverts socle (`0b6e7ad`..`93c8014`)
- [x] An 0 accumulation started — `run_bot_ui` running in background,
      writing to `data/ledger/`, `data/audit/` populated fresh
- [ ] Phase 2 cProfile — NEXT
- [ ] Phase 2 NumPy rewrites — top 3 hot-spots
- [ ] Phase 2.5 heartbeat tick
- [ ] Phase 10-Harden adversarial fill + latency
- [ ] Stability Gate 7-day An-0 replay run
- [ ] (Then, not before) Phase 10-Bridge

---

_Last modified: 2026-04-21 · Owner: see commits on `feat/tensor-engine`._
