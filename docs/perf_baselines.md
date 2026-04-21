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

**Phase 2.5 cadence change.** Since the 1 Hz heartbeat commit, the
engine ticks every second instead of every 5 s. Comparisons below
split into two regimes:
- **Pre-2.5 (5 Hz, 50 steps → 250 s horizon)**: the original invariant
  contract applies. Snapshot: `pre_phase2.json`.
- **Post-2.5 (1 Hz, 250 steps → same 250 s horizon)**: new canonical
  baseline. Same forward horizon, 5× finer decision cadence, different
  trade density. Snapshot: `post_phase25_heartbeat.json`.

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
| `feat(perf): RollingSeries NumPy backend` | 28.96 s | 25.53 s | **1.13×** | HELD: 0 vote drift / 14 tribes, end_equity exact, SHA regenerated |
| `feat(perf): realized_vol_bps NumPy` | 25.53 s | 19.62 s | **1.30×** (cumul **1.48×**) | HELD: 0 vote drift / 14 tribes, end_equity exact, SHA regenerated |
| `feat(perf): Phase 2.5 — 1 Hz heartbeat tick` | 19.62 s (5 Hz) | **43.15 s (1 Hz)** | N/A — cadence change, see §Phase 2.5 | Invariant contract DOES NOT APPLY across a cadence change; new baseline captured |

### Post-commit 1 profile

Hot-spot #1 (`market_state.py:61 <genexpr>`, 21M calls, 10.23 s) is gone from
the top-15. New leader inside `market_state.py` is `realized_vol_bps` at
6.10 s tottime — the intended target for commit 2. Incidental residue:
`_trim_to_epoch` ticked up +0.7 s (adding the `_dirty` flag maintenance),
and the window consumers still pay a `zip(ts.tolist(), vs.tolist())`
materialization tax. That tax collapses once `realized_vol_bps` calls
`_slice_arrays` directly in commit 2.

Snapshot: `docs/perf_baselines/post_rollingseries_numpy.json`.

### Post-commit 2 profile

`realized_vol_bps` tottime collapses from 6.10 s → 0.52 s (11.7× on the
function itself; 11,627 calls in a 500-tick sprint). `np.diff(np.log(px))`
plus `.std(ddof=1)` runs in C with one ufunc reduction. Float ordering
drifts at the 1e-15 level and never flips a downstream vote — all 14
tribes match BUY/SELL counts exactly; end_equity holds to the cent.

New leaderboard inside `market_state.py` is the structural trio:
`_trim_to_epoch` (3.29 s), `_advance_clock_epoch` (2.89 s), `snapshot`
(2.53 s). These are per-ingest and don't have a single easy win —
they're out of scope for Phase 2 (deferred target 3 in the original
plan, re-evaluated here). External consumer `indicators.py:54 _prices`
(2.59 s) is the next reasonable target when we want another round.

Snapshot: `docs/perf_baselines/post_realized_vol_numpy.json`.

---

## Phase 2.5 — 1 Hz heartbeat tick

**Commit**: `feat(perf): Phase 2.5 — 1 Hz heartbeat tick (normalized horizon)`
**Date**: 2026-04-21
**Snapshot**: `docs/perf_baselines/post_phase25_heartbeat.json`

This is a **behavioral** change, not a pure perf refactor. The invariant-
diff contract from Phase 2 (exact tick count, exact trade count, exact
per-tribe vote count) does not apply across a cadence change — the new
cadence changes everything downstream. The pre-Phase-2 baseline
(`pre_phase2.json`, 5 Hz) is now **historical reference only**; all
future Stability Gate comparisons use the 1 Hz baseline below.

### What changed

| Knob | Pre-2.5 | Post-2.5 |
|---|---|---|
| `clock.tick_seconds` | 5 | **1** |
| `clock.projector_steps` | 50 | **250** |
| Forward horizon | 50 × 5 s = 250 s | 250 × 1 s = **250 s** (preserved) |
| Per-tick budget | 5000 ms | 1000 ms |
| Calibrator EWMA halflife | 500 **anchors** (count) | 2500 **seconds** (time) |
| Calibrator fast EWMA halflife | 50 anchors | 250 seconds |
| Kernel calibrator halflife | 300 anchors | 1500 seconds |

### Metrics — standard sprint

| Metric | Pre-2.5 (post-`realized_vol_bps`) | Post-2.5 |
|---|---|---|
| Wall time (no profiler) | 19.62 s | **43.15 s** |
| Per-tick wall (no profiler) | 39.2 ms | **86.3 ms** |
| Tick budget (live) | — | 1000 ms — **11.5×** headroom |
| Ticks | 500 | 500 |
| Simulated wall time | 2500 s | **500 s** (5× shorter per 500-tick cap) |
| Trades opened | 5 | **102** |
| End equity | $49,990.95 | $49,393.12 |
| `decisions.jsonl` SHA256 | `2e65f7fe…` | `cb3aaf47…` |

### Why 102 trades vs 5

The cadence change makes the engine evaluate entries 5× more often per
simulated second (1 Hz vs 0.2 Hz), and council votes now re-weight every
second instead of every 5 s. That finer resolution opens positions in
places the 5 Hz cadence would have blurred past, so trade density
roughly 20×. This is **expected behavior** — the strategy question
("should we gate entries more aggressively at 1 Hz?") belongs to Phase
10-Harden, not Phase 2.5.

### Profile under 1 Hz + 250 steps

| Rank | Function | tottime | Note |
|---|---|---|---|
| 1 | `chess.py:265 project` | 27.50 s | 55 ms/tick at 250 steps — 3.4× from 50-step scaling (sublinear win from vectorized RollingSeries) |
| 2 | `market_state.py:743 _advance_clock_epoch` | 11.50 s | 3.0 M calls (5× pre-2.5) — structural, per-ingest |
| 3 | `market_state.py:70 _trim_to_epoch` | 6.12 s | 33 M calls (5× pre-2.5), still O(1) per event — trim itself is cheap, overhead is in the call count |
| 4 | `builtins.sum` | 2.10 s | residual list-comprehension consumers of RollingSeries |
| 5 | `voting.py:39 aggregate_votes` | 1.10 s | 5× more invocations per sprint |

The projector dominates more than ever (71.7 s cum out of 75 s profiled
— 95 %). Per-tribe batched vote evaluation (the Phase 2 Target 3 that
was deferred) becomes the natural next perf move if 1 Hz is too tight
in live — but the 86 ms/tick already fits comfortably inside the 1 s
budget, so this is a "save for later" lever.

### Time-based calibrator EWMA

`TribeCalibrator` and `KernelCalibrator` now accept `halflife_resolved_s`
(wallclock seconds). When set, each EWMA cell tracks its own `last_ts`
and alpha is computed from the gap: `alpha = 1 − 0.5 ** (dt_s /
halflife_s)`. The count-based halflives remain as fallback for anything
that still reads the old config. Cold-start uses a 1 s gap equivalent
so the first observation per cell moves the EWMA by roughly the same
tiny amount the legacy fixed-alpha did at 1 Hz.

This refactor means any future cadence change (1 Hz → 10 Hz, 1 Hz →
0.5 Hz, heterogeneous anchor cadences) leaves the calibrator's time
constants unchanged — no re-tuning required.
