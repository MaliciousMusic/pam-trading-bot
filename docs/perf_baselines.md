# PAM Bot — Performance Baselines

_Records per-commit perf numbers and the semantic-invariant contract._

All measurements use the **standard profile sprint**:
- Day: `2026-04-20`
- Max ticks: `500`
- Synth rate: `60` events/bar
- Seed: `42`
- Symbol: `BTCUSDT`

Numbers include the 24-tribe post-Yeux-Ouverts council
(`microstructure_prober`, `put_call_fader`, `options_dealer_gamma` all
active).

Snapshots live under `docs/perf_baselines/*.json`. Raw cProfile dumps
live under `data/perf/*.prof` (gitignored — per-machine, regenerable).

## The semantic-invariant contract

For Phase 2 NumPy commits, we accept a new `decisions.jsonl` SHA256 when
and only when:

| Invariant | Tolerance | Rationale |
|---|---|---|
| tick count | exact | Timing logic unchanged |
| trade count | exact | Decision path unchanged |
| per-tribe BUY count | exact (or documented ordering drift ≤ 0.1%) | Same votes, same math |
| per-tribe SELL count | exact (or same) | Same votes, same math |
| `end_equity_usd` | ≤ 1 bps drift | Fee + slippage same; float reorder ≤ 1e-4 |

Violations require an explicit waiver in the commit message explaining
why the semantics moved.

Tool: `python -m scripts.bench_semantic_invariants --day 2026-04-20 --baseline <path>`

---

## Phase 2 baseline — pre-NumPy

**Commit**: `a59073e` (roadmap lock; no code change since `93c8014`).
**Date**: 2026-04-21
**Snapshot**: `docs/perf_baselines/pre_phase2.json`

| Metric | Value |
|---|---|
| Wall time (no profiler) | **28.96 s** |
| Wall time (under cProfile) | 56.75 s |
| Per-tick (no profiler) | **~58 ms** |
| Per-tick (under profiler) | ~113 ms |
| Ticks | 500 |
| Trades completed | 5 |
| Events consumed | 30,000 |
| Start equity | $50,000.00 |
| End equity | $49,990.95 |
| Equity delta | −$9.05 |
| `decisions.jsonl` SHA256 | `2e65f7fe7f61b436…` |

### Hot-spots (from cProfile)

By **internal time** (where CPU actually burns):

| Rank | Function | Calls | tottime | Note |
|---|---|---|---|---|
| 1 | `market_state.py:61 <genexpr>` | 21.0M | 10.23 s | `_window` filter — every tribe read rides on this |
| 2 | `chess.py:265 project` | 500 | 7.33 s | Per-tick projector body (cum 39.9 s incl. callees) |
| 3 | `market_state.py:235 realized_vol_bps` | 11.6K | 5.51 s | Manual log-returns + stddev loop |
| 4 | `builtins.sum` | 755K | 3.53 s | Python sum over list comprehensions |
| 5 | `_trim_to_epoch` | 6.6M | 2.72 s | Ring-buffer trim on projector step |

By **cumulative time** (subsystem ownership):

| Rank | Function | cum |
|---|---|---|
| 1 | `chess.project` | 39.88 s (70% of per-tick) |
| 2 | `tribe._fresh_signal` | 30.35 s |
| 3 | `tribe._blend_with_indicators` | 20.99 s |
| 4 | `indicators.evaluate` | 17.39 s |
| 5 | `market_state.realized_vol_bps` | 14.02 s |

### Phase 2 target plan

| Commit | Target | Expected |
|---|---|---|
| `feat(perf): RollingSeries NumPy backend` | Lazy-materialized ndarray + searchsorted for `_window`/`sum`/`first_last`/`min_max` | 30–50% per-tick reduction; `_window` cost collapses |
| `feat(perf): realized_vol_bps NumPy` | `np.diff(np.log(...)).std(ddof=1)` replaces manual loop | ~1 s saved per sprint; ≤ 1e-15 float drift |
| (deferred) batched tribe-vote loop | Re-evaluate after the above two land | TBD |

---

_Table below appended with each Phase 2 commit._

| Commit | Pre wall | Post wall | Speedup | Invariants |
|---|---|---|---|---|
| (pre-Phase-2 baseline) | — | 28.96 s | — | — |
