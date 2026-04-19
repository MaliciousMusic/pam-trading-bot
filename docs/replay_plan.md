# PAM Replay Harness — Build Plan

**Project:** PAM Council trading bot (BTC perps, Binance)
**Branch:** `feat/multi-layer-exit-engine` → PRs merged to `main`
**Last updated:** 2026-04-19

---

## Standing constraints (never relax without discussion)

| Constraint | Detail |
|---|---|
| Free data only | No paid APIs until budget discussed. Binance kline + funding endpoints are free. |
| UI shape-purity | `onTick(m)` and `onEvent(m)` handlers must never be modified for replay. Only `onReplayState(m)` may touch replay state. Tick messages from `WatchEngine` are byte-for-byte shape-compatible with the live engine. |
| Determinism | Same `(day, seed, synth_rate)` → byte-identical `decisions.jsonl`. Tribe seeds are derived via `sha256(tribe_id + master_seed)` — cross-process stable. |
| No GIL workaround | CPU-bound tribe voting is parallelised via real OS subprocesses, not `asyncio` tasks. |

---

## Phase status

### ✅ Phase 1 — Kline cache + event stream
- `src/replay/kline_client.py` — `CachedHistoricalClient` (Binance 1m klines + funding, disk cache)
- `src/replay/bar_to_events.py` — 1m bar → N synthetic tick events (configurable `synth_rate`)
- `src/replay/feed.py` — `build_event_stream()` merges kline + funding into ordered `Event` iterator
- `scripts/test_replay_phase1.py` — green

### ✅ Phase 2 — Sprint harness (headless, deterministic)
- `src/replay/harness.py` — `SprintEngine` + `run_sprint()`
- `src/replay/decision_log.py` — `DecisionLogWriter` (per-tick JSONL)
- `scripts/run_replay.py` — CLI entry point
- `scripts/test_replay_phase2.py` + `scripts/test_replay_smoke.py` — green
- Exit criterion proved: two runs of same `(day, seed, synth_rate)` → sha256-identical `decisions.jsonl`

### ✅ Phase 3 — Multi-variant subprocess orchestrator
- `src/replay/orchestrator.py` — `run_multi_variant()` fans out N variants as real subprocesses
- `VariantSpec`, `VariantOutcome`, `MultiVariantResult` dataclasses
- Parent pre-fetches Binance cache; workers read from disk (no race)
- `max_concurrent` defaults to `cpu_count - 1`
- `scripts/run_multi_variant.py` — CLI (rate × seed cartesian product)
- `scripts/test_replay_phase3.py` — green; wall = 0.51 × sum-of-workers proven

### ✅ Phase 4 — Downloadable report zip
- `src/replay/report.py` — `RunMetrics` / `VariantMetrics` extracted from `decisions.jsonl` (N_PLOT=600 downsample)
- `src/replay/report_html.py` — single-file HTML with server-side SVG charts (no CDN, offline-safe)
- `src/replay/report_md.py` — Markdown summary with comparison table
- `src/replay/report_zip.py` — `build_zip()` packages run dir + both reports
- `scripts/build_report.py` — CLI
- `scripts/test_replay_phase4.py` — 5 tests, all green
- Orchestrator auto-calls `build_zip()` after all variants succeed (`emit_report=True`)

### ✅ Phase 5 — Watch-mode paced engine + amber banner
- `src/replay/watch_engine.py` — `WatchEngine` (async, sleeps `tick_seconds / speed` per tick, broadcasts to `UIBroadcaster`) + `WatchController` (lifecycle: start / pause / speed / stop)
- `src/replay/harness.py` — `run_watch()` added (builds same stack as `run_sprint`, uses `WatchEngine`)
- `src/ui/server.py` — replay routes added: `GET /replay`, `POST /api/replay/start|speed|pause|stop`; `create_app()` accepts `watch_controller=`
- `src/ui/static/index.html`:
  - CSS: `.replay-banner` amber styles + `.replay-start` modal styles
  - HTML: `#replay-banner` (sticky, hidden until `.active`) + `#replay-start` modal (shown at `/replay` path)
  - JS: `onReplayState(m)` — **only** function that touches the banner; `replayStart/Pause/Stop/Speed()` — thin fetch wrappers; `maybeShowReplayForm()` — shows modal at `/replay`
  - `ws.onmessage` router: `tick → onTick | event → onEvent | replay_state → onReplayState` — no other handler changed

**Architectural invariant:** `WatchEngine` tick messages are shape-identical to `TradingEngine._on_tick()`. The live dashboard renders replay data with zero knowledge of the source.

---

## ✅ Phase 6 — `/replay` route end-to-end

**Goal:** A user opens `/replay` in the browser, picks a date + speed, clicks START, and watches the full dashboard animate exactly as it does live — with the amber banner counting progress.

**What's already done (Phase 5):**
- Server routes exist (`POST /api/replay/start` etc.)
- Start modal HTML + JS exists (`#replay-start`, `replayStart()`)
- Amber banner HTML + JS exists
- `WatchController` wires into `create_app()`

- `scripts/run_bot_ui.py` — `WatchController` constructed and passed to `create_app(watch_controller=...)`; `cache_root` resolved from `settings().replay.cache_dir`
- `index.html` — `onReplayState`: closes start modal on `active=True`; toggles pause button `⏸ PAUSE` ↔ `▶ RESUME`; `resetChartState()` clears all history arrays + scalars on new replay start
- `scripts/test_replay_phase6.py` — mock broadcaster smoke test: 39 ticks, 39 tick msgs, 2 replay_state msgs, final `active=False` ✓

---

## ✅ Phase 7 — Multi-variant watch panes

**Goal:** `POST /api/replay/start` accepts an optional `variants: list[{synth_rate, seed}]` field. When >1 variant, the dashboard opens N side-by-side mini equity/price panels, one per variant, each receiving its own tick stream. The amber banner shows aggregate progress.

- `WatchEngine`: `variant_id` + `emit_replay_state` params; tick messages tagged with `"variant_id"`
- `_conduct()`: free coroutine sending 1-per-second aggregate `replay_state` with `variant_count` field
- `WatchController.start()`: `variants: list[dict]` param; single path unchanged; multi path builds N stacks + conductor task
- `server.py`: `_ReplayStartBody.variants` field forwarded to controller
- `index.html`: synth-rate checkboxes (multi-select); `onVariantTick(vid,m)` + `_ensureVariantPane()` + `_sparklineSvg()`; `#variant-panes` fixed bottom bar with per-variant price+equity sparklines + action badge; `resetChartState()` clears variant state; `ws.onmessage` routes by `m.variant_id`
- `test_replay_phase7.py`: 2-variant concurrent run (29+29 ticks, 10 conductor msgs, `variant_id=None` backward-compat) ✓

---

## ✅ Phase 8 — Click-a-bar inspect modal

**Goal:** Clicking any bar in the equity or price chart opens a modal showing the tick-level detail for that time window: council votes, regime, forecast, positions, action.

- `WatchEngine`: `decisions_path` param — opens `DecisionLogWriter` in `run()`, writes per tick alongside broadcasting; `WatchController` computes path per variant and passes it in
- `GET /api/replay/tick?run_id=X&tick=N&variant_id=Y` — scans decisions.jsonl, resolves variant subdirs automatically
- `GET /api/replay/runs` — lists recent run directories with day/symbol metadata (cap 50)
- `index.html`: `_replayRunId` tracked from `replay_state`; price+equity canvases get crosshair cursor + click handler → `showTickInspect()`; variant sparklines also clickable; `#tick-inspect` modal renders tick detail + per-tribe conviction bars
- `test_replay_phase8.py`: 4 tests (endpoint tick lookup, variant path, 404s, WatchEngine decisions write) all green

---

## Phase 9 — Week-unit experiment (persistent across day boundary) (NEXT)

**Goal:** Run M consecutive days as a single experiment. Equity carries over between days (end equity of day N = start equity of day N+1). Results aggregated into a weekly report alongside per-day variant breakdowns.

**Design:**
- `run_week()` in `orchestrator.py`: loop over `day in week`, call `run_multi_variant()` sequentially, thread `end_equity_usd` as `starting_equity_usd` into next day's `_build_stack`
- `PaperExecutor` already supports `starting_equity_usd` param — no change needed
- Weekly report: extend `RunMetrics` / `report_html.py` with a day-axis equity curve

---

## Key files quick-reference

```
src/
  replay/
    kline_client.py      — Binance cache (Phase 1)
    feed.py              — event stream builder (Phase 1)
    decision_log.py      — per-tick JSONL writer (Phase 2)
    harness.py           — SprintEngine + run_sprint + run_watch (Phase 2/5)
    orchestrator.py      — multi-variant subprocess fan-out (Phase 3)
    report.py            — RunMetrics / VariantMetrics (Phase 4)
    report_html.py       — server-side SVG HTML report (Phase 4)
    report_md.py         — Markdown summary (Phase 4)
    report_zip.py        — zip packager (Phase 4)
    watch_engine.py      — WatchEngine + WatchController (Phase 5)
  ui/
    server.py            — FastAPI app + replay routes (Phase 5)
    broadcaster.py       — UIBroadcaster (subscribe/publish/buffer)
    static/index.html    — dashboard + amber banner (Phase 5)
scripts/
  run_bot_ui.py          — server entry point (needs Phase 6 wiring)
  run_replay.py          — sprint CLI
  run_multi_variant.py   — orchestrator CLI
  build_report.py        — report zip CLI
  test_replay_phase*.py  — per-phase test suites
```
