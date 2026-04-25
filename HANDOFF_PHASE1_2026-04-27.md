# HANDOFF — Phase 1 re-entry (saturation chain SHUT, D5 clock anchored 2026-04-25)

**Last updated**: 2026-04-25 17:00 CET (post H6.C landed + live PASS + push H6.A+B+C; saturation chain closed)
**Target consumption**: **2026-04-28 evening** (D5 anchor + 3 days)
**Scope**: everything a fresh Claude session needs to monitor the new D5 detached bot and consume Phase 1 results when the clock matures.

---

## 0. One-paragraph status (SUPERSEDES §0 v4 — post H6.C live PASS + push)

**Saturation chain SHUT. Push complete. New D5 anchor live.** H6.C fix landed `src/projector/chess.py` per `WS3_H6C_phase0_audit.md` §5 Option A (`DNA_DELTA_CLAMP_BPS = 100.0` cap on `dna_delta = dna_fn(ctx)` post-evaluation, +6 unit tests/26 parametrized in `tests/projector/test_dna_delta_clamp.py`, +`scripts/h6c_live_diagnostic.py`, +`research/phase11_diagnostic/WS3_H6C_phase0_audit.md`); committed local as `675bc80` and pushed to `origin/feat/tensor-engine`. **Phase 0 verdict differed from H6.B audit hypothesis**: live instrumentation (PID 5780, 234 projections, 4 min Bybit chop) showed `event_bps = 0.0` always (#3a refuted in chop) and `max_dna` per projection drift monotonically from 65 bps (warmup) to 2,744 bps (steady, max=3,364 bps). Driver = `intent_drift = c × base_strength × signed_intent` in DNA functions (whale c=0.55, mev 0.35, ta 0.25, ...) where `base_strength = conviction × calibrator_skill × effective_gain` is unbounded via `calibrator.skill_multiplier` feedback. H6.A clamped recursion *inputs* only; H6.B capped scale only; neither bounded the DNA *output*. Phase 1 verification: 26/26 H6.C tests + 73/73 projector tests PASS, det gate byte-identical (sha256 `9ff73647bf154fbe...` on 04-20), chop regression 0/2000 saturation hits (`--max-ticks=1000` × 2). **Live 5-min on Bybit (PID 20944, 22:50:37–22:55:37 local, 276 ticks)**: saturation_hit = **0/276** (steady-state 0% << 5% gate); H6.C clamp engages massively (211/276 projections = 76%); H6.A 680,666 + H6.B 1,656,000 cap-fires confirm full chain working. Push trio H6.A+B+C complete on origin, commits `cf97165 → 6332848 → e666c05 → e02f3a6 → 675bc80` all on `feat/tensor-engine`. **D5 anchor**: new detached bot launched 2026-04-25 22:59:34 CEST (PID 22484); Phase 1 consumption ETA 2026-04-28 evening.

---

## 1. Exact state at 2026-04-25 17:00 CET (post H6.C live PASS + push)

| Item | Value |
|---|---|
| **D5 anchor time** | **2026-04-25 22:59:34 CEST** (PID 22484, post-H6.A+B+C live) |
| **Bot PID** | **22484** alive |
| Previous (now-killed) anchors | PID 16096 pre-H5.A (22:47) → PID 12104 post-H5.A (71 min) → PID 2296 post-H6.A (5 min, 100% sat) → PID 7560 post-H6.B (~4 min, 100% sat) → PID 5780 H6.C P0 instrumentation (~4 min) → PID 20944 post-H6.C live gate (5 min, **0% sat PASS**) |
| Branch | `feat/tensor-engine` — **clean, all H6 commits pushed** |
| Tip commit (now) | `675bc80` (H6.C fix) — origin synchronized |
| Push trio (H6.A+B+C) | `cf97165` H6 audit · `6332848` H6.A · `e666c05` H6.B audit · `e02f3a6` H6.B · `675bc80` H6.C — **ALL PUSHED** |
| Config path | `config/settings.yaml` |
| Venue | Bybit V5 perps, BTCUSDT |
| Live D5 anchor stderr log | `logs/bot_ui_2026-04-25_D5_anchor.err` (anchored 22:59:34 CEST) |
| Live H6.C gate log (PASS reference) | `logs/bot_ui_2026-04-25_post_H6C.err` (276 ticks, 0/276 sat hits) |
| H6.C P0 instrumentation log | `logs/bot_ui_2026-04-25_post_H6C_p0.err` (234 projections, working-tree diag, NOT committed) |
| Earlier-night logs | `bot_ui_2026-04-25_post_H6B.err` (~4 min FAIL) · `bot_ui_2026-04-25_post_H6A.err` (5 min FAIL) · `bot_ui_2026-04-24_post_H5A.err` (71 min FAIL) |
| Pathological baseline | `data/baseline/pathological_2026-04-24_pre-H5A/` — **committed** (`f948e36`), 12,011 rows, 4 SHA256s |
| Lively-chop fixture (04-21) | `data/replays/h6b_baseline_pre_2026-04-21/` + `data/replays/h6b_post_2026-04-21/` — scratch artefacts, **NOT committed** |

**Earliest legitimate Phase 1 read: 2026-04-28 evening** (D5 anchor + 3 days). Target consumption tape: `data/forecasts/2026-04-2{5,6,7,8}.jsonl` — accumulating from 22:59:34 CEST anchor.

---

## 2. Commits — pushed vs. local

**Pushed to origin (tip = `ff995a3`):**
```
ff995a3  docs(research): WS3 H6 scope — DNA-recursion positive-feedback audit
f948e36  data(baseline): pathological_2026-04-24_pre-H5A — 12011 rows, 4 sha256s, H5 P4 attractor reproduced
f0076bd  feat(projector): H5.A saturation clamp on step_bps_list (closes §6.3)
f101800  docs(research): WS3 H5 Phase 0 audit — bug-found, 1/99 attractor fingerprinted
```

**ALL PUSHED to origin (saturation chain quintet, H6.A+B+C trio of fixes):**
```
675bc80  feat(projector): H6.C — DNA-output clamp (closes door #3; saturation chain shut)
e02f3a6  feat(projector): H6.B — depth-scale cap (closes door #2; door #3 promoted)
e666c05  docs(research): H6.B Phase 0 — scale × DNA-output audit (bug-found door #2)
6332848  feat(projector): H6.A — DNA recursion-input clamp (closes door #1)
cf97165  docs(research): H6 Phase 0 — bug-found, DNA coefficients drive divergence
```

Brief on the quintet:
- `cf97165` — H6 Phase 0 audit doc (`research/phase11_diagnostic/WS3_H6_phase0_audit.md`, 382 lines). Identified ta_dna + retail panic as the 2 DNA categories producing chain coefficient ≥ 1 under routine scale. Options A/B/C; recommended C.
- `6332848` — H6.A fix. `src/projector/chess.py` (+32/-2, adds `DNA_CTX_CLAMP_BPS = 100.0`, 2 clamp lines at 601-603, diagnostic counter + logger.warning aggregation), `tests/projector/test_dna_input_clamp.py` (new, 22 tests). 22/22 new + 101/101 existing tests PASS; determinism `--max-ticks=1000` byte-identical PASS. **Live test INSUFFICIENT per §3 live-readout — closes door #1 only.**
- `e666c05` — H6.B Phase 0 audit doc (`research/phase11_diagnostic/WS3_H6B_phase0_audit.md`, 495 lines). Identified door #2 (scale amplification). Options A/B/C/D; recommended Option B (scale cap). Worst-case quartile: LOW–MEDIUM.
- `e02f3a6` — H6.B fix. `src/projector/chess.py` (+24/-1, adds `DEPTH_SCALE_MAX = 5.0`, `min(raw_scale, DEPTH_SCALE_MAX)` cap, `_depth_scale_cap_hits_last` diagnostic counter), `tests/projector/test_depth_scale_cap.py` (new, 6 contracts), `scripts/h6b_signal_preservation.py` (new, regression tool). 6/6 new tests PASS; det gate byte-identical on 04-20; chop regression saturation 0/1000 ticks; signal-preservation 04-21 lively-chop top-decile = ratio 1.0 / 0 windows annihilated. Live test on Bybit (PID 7560) showed cap fires 1.34M× but steady-state saturation 100% — door #2 closed correctly, door #3 displaced. Full validation came post-H6.C below.
- `675bc80` — H6.C fix. `src/projector/chess.py` (+24/-1, adds `DNA_DELTA_CLAMP_BPS = 100.0`, `max(-100, min(100, dna_fn(ctx)))` cap on output post-evaluation, `_dna_delta_clamp_hits_last` diagnostic counter), `tests/projector/test_dna_delta_clamp.py` (new, 6 contracts/26 parametrized), `research/phase11_diagnostic/WS3_H6C_phase0_audit.md` (new, 10-section audit), `scripts/h6c_live_diagnostic.py` (new, live-gate parser). 26/26 new + 73/73 projector tests PASS; det gate byte-identical on 04-20 (sha256 `9ff73647bf154fbe...`, 3,081,630 bytes); chop regression 0/2000 saturation hits. **Live test PASS on Bybit (PID 20944, 22:50:37–22:55:37 CEST, 276 ticks)**: saturation_hit = 0/276 (steady-state 0% << 5% gate), H6.C clamp engages 211/276 projections (76%), chain confirmed shut.

---

## 3. 🚩 PHASE 1 READOUT — saturation chain SHUT, H6.A+B+C live PASS

**Evidence trail (measurements, no interpretation):**

| Run | Duration | Ticks | Saturation ratio (overall) | Saturation ratio (steady-state, ticks ≥ 100) | Trades | Diagnostic |
|---|---|---|---|---|---|---|
| PID 12104 post-H5.A | 71 min | 2,846 | **100.0%** | 100.0% | 0 | — |
| PID 2296 post-H6.A | 5 min | 303 | 84.7% | **100.0%** (ticks 100-299 all 50/50) | 0 | DNA clamp ~3,700 fires/projection |
| PID 7560 post-H6.B | ~4 min | 224 | 70.5% (158/224) | **100.0%** (125/125) | 0 | DNA clamp 600K + depth-scale cap 1.34M |
| **PID 20944 post-H6.C** | **5 min** | **276** | **0.0%** | **0.0%** (0/176) | 0¹ | DNA-out clamp **211/276 projections (76%)** + DNA ctx 680K + depth-scale 1.66M |
| PID 22484 D5 anchor (running) | live | accumulating | TBD | TBD | TBD | tape `data/forecasts/2026-04-25.jsonl` |

¹ 0 trades on H6.C live ≠ bug. Audit verified: `final_conf` median = 0.000 because consensus is genuinely directionless in chop, NOT because saturation forces conf=0. With saturation closed, conf reflects real signal alignment; in 5 min of chop the alignment was insufficient. This is correct behavior — the bot is NOT trading nonsense.

**Reading.** H5.A (`f0076bd`) = output clamp on `step_bps_list` (kill-switch). H6.A (`6332848`) = recursion-input clamp on `prev_state_bps` / `prev_delta_bps` (door #1). H6.B (`e02f3a6`) = scale cap (door #2). H6.C (`675bc80`) = DNA-output cap on `dna_delta` post-`dna_fn(ctx)` (door #3). The full chain `(bounded_dna_input → bounded_dna_output → bounded_event → bounded_scale → bounded_step_bps)` is now strict. The saturation pathology that defined Phase 11 is fixed.

**For the analyst consuming `logs/bot_ui_2026-04-25_post_H6C.err` (live PASS reference):**
- DNA-out clamp fires = 10,301 (per logger.warning aggregation across 211 projections). H6.C engaging massively but not pathologically — only ~50 dna_delta values clamped per projection on average, vs ~3,700 input clamps for H6.A on the same run.
- DNA ctx clamp fires = 680,666 (H6.A diagnostic, recursion input). Depth-scale cap fires = 1,656,000 (H6.B diagnostic). Both H6.A + H6.B continue to engage on every projection.
- saturation_hit warnings = 0 across 276 ticks. Steady-state window (ticks ≥ 100) = 0 hits / 176 ticks = 0%. Saturation chain closed end-to-end.
- Last 100 ticks `conf` median = 0.000 BECAUSE consensus directionless in chop, NOT because of saturation forcing. With saturation closed, the bot is correctly choosing not to trade in low-signal chop. This is the desired behavior.
- Regime = chop. event_bps = 0.0 always (P0 instrumentation confirmed: #3a from H6.B audit hypothesis was REFUTED on chop tape — `pending_events` empty without news classifier hits).
- **Note (Bybit symbol log encoding)**: file has 0xd7 byte in symbol field; read with `errors='replace'` if parsing in Python. Same caveat applies to D5 anchor log.

**Phase 1 analyzer work pending.** The D5 anchor tape (`logs/bot_ui_2026-04-25_D5_anchor.err` + `data/forecasts/2026-04-2{5,6,7,8}.jsonl`) is the Phase 1 consumption tape. Earliest legitimate read: 2026-04-28 evening. Analyzers to build/run: `ws3_h5_attractor_analyze.py` (saturation_hit / abs_activity_bps / step_bps_signflips by hour & regime), Q5/Q6 calibrator readers (I3.a/b — should now populate, since saturation is no longer suppressing tribe stamps).

**Cascade-regime regression deferred (handoff caveat, still open).** No cascade-regime tape (≥50 bps/60s 1-min window) exists in the repo. Door #3 fix (H6.C) was validated on chop only. **Prerequisite for cascade validation: acquire cascade-regime tape.** This is NOT a blocker for D5 / Phase 1 read — chop is the dominant regime — but IS a prerequisite for H6.D (door #4 candidates) if those surface post-Phase 1.

---

## 4. Daily-check commands — **STALE, no bot to check**

> The §4.1–4.6 procedure below targeted PID 12104, which was killed at
> 2026-04-25 00:42 CET. It is preserved as a template for the *next*
> detached bot (anchored after H6.B fix lands). Until then, these
> commands will return `BOT_DEAD` + no fresh tape rows — that is the
> expected state, not a bug. **Do not restart a bot without landing
> H6.B first** or the saturation ratio will stay at 100% steady-state
> per §3.



### 4.1. Is bot alive + uptime
```powershell
powershell.exe -Command "Get-Process -Id 12104 -ErrorAction SilentlyContinue | Select-Object Id,StartTime,@{N='UptimeHours';E={[math]::Round(((Get-Date)-$_.StartTime).TotalHours,2)}},@{N='CPU_s';E={[math]::Round($_.CPU,1)}},@{N='WS_MB';E={[math]::Round($_.WorkingSet/1MB,1)}} | Format-List"
```

### 4.2. Saturation rate (new, post-H5.A specific)
```bash
grep -c "saturation hit" logs/bot_ui_2026-04-24_post_H5A.err
# Divide by post-H5.A tick count from logs (tail a recent "[tick N]" line and subtract from 21 which is the first tick at launch).
```
Trending down over 24 h = calibrator healing; flat ≥ 80 % = H6 self-sustaining.

### 4.3. Tape growth
```bash
wc -l data/forecasts/2026-04-24.jsonl data/forecasts/2026-04-25.jsonl data/forecasts/2026-04-26.jsonl data/forecasts/2026-04-27.jsonl 2>/dev/null
```

### 4.4. Err log crash scan
```bash
grep -c -iE "traceback|exception|fatal|critical|segfault" logs/bot_ui_2026-04-24_post_H5A.err
```
Expected: **0** (saturation warnings don't match any of those keywords).

### 4.5. Liveness (last jsonl row within 2 min)
```bash
python -c "import json; from pathlib import Path; import time; rows=[json.loads(ln) for ln in Path('data/forecasts/2026-04-27.jsonl').read_text(encoding='utf-8').splitlines() if ln.strip()]; last=rows[-1]['tick_ts']; print(f'last_tick={last:.0f}, age_s={time.time()-last:.0f}')"
```

### 4.6. One-liner combined
```bash
(powershell.exe -Command "if (Get-Process -Id 12104 -ErrorAction SilentlyContinue) {'BOT_ALIVE'} else {'BOT_DEAD'}") && wc -l data/forecasts/2026-04-2*.jsonl 2>/dev/null && grep -c "saturation hit" logs/bot_ui_2026-04-24_post_H5A.err && grep -c -iE "traceback|exception|fatal" logs/bot_ui_2026-04-24_post_H5A.err
```

---

## 5. H6 status (as of 2026-04-25 17:00 CET — chain SHUT, H6.A+B+C all landed and pushed)

### 5.1 Timeline (full H6 thread)

| Time (CET) | Event | Artefact |
|---|---|---|
| 2026-04-24 23:48 | H6 Phase 0 audit box opened | |
| 2026-04-25 00:03 | H6 Phase 0 verdict: BUG-FOUND (DNA recursion, ta_dna + retail panic) | `cf97165` |
| 2026-04-25 00:28 | H6.A implemented + tests + det gate | `6332848` |
| 2026-04-25 00:42 | PID 12104 killed + PID 2296 launched post-H6.A | — |
| 2026-04-25 00:47 | H6.A live 5-min check: saturation steady-state 100% → STOP | — |
| 2026-04-25 00:48 | PID 2296 killed, check-in with user | — |
| 2026-04-25 00:50 | H6.B Phase 0 audit box opened (70 min strict) | |
| 2026-04-25 01:20 | H6.B Phase 0 verdict: BUG-FOUND (scale × DNA-output, door #2) | `e666c05` |
| 2026-04-25 12:47 | H6.B fix box opened (2h20 strict, 2h35 hard abort) | — |
| 2026-04-25 ~13:30 | Baseline replay 04-21 lively-chop top-decile (11/95 windows covered, ~3.86h, killed at synth-rate=6 throughput limit) | `data/replays/h6b_baseline_pre_2026-04-21/` |
| 2026-04-25 ~14:00 | H6.B impl + 6 tests + det gate byte-identical PASS + chop regression (0/1000 sat) + signal-preservation (cap dormant on synth) | `e02f3a6` |
| 2026-04-25 14:21 | PID 7560 launched post-H6.B for live 5-min | — |
| 2026-04-25 14:25 | H6.B live 5-min: saturation steady-state 100% → STOP | `logs/bot_ui_2026-04-25_post_H6B.err` |
| 2026-04-25 ~16:25 | Check-in: H6.B committed local (`e02f3a6`), NOT pushed; door #3 promoted to H6.C scope | — |
| 2026-04-25 16:35 | H6.C box opened (2h30 strict, 2h50 hard abort) | — |
| 2026-04-25 ~16:38 | P0 static trace: event_bps source + dna_delta math + base_strength source identified | — |
| 2026-04-25 ~16:38–16:42 | P0 live instrumentation (PID 5780, working-tree only): 234 projections, max_dna 65→2,744 bps, event_bps=0.0 always — #3a refuted, driver = intent_drift via base_strength × calibrator_skill | `logs/bot_ui_2026-04-25_post_H6C_p0.err` |
| 2026-04-25 ~16:43 | P0 audit doc closed: BUG-FOUND, Option A = cap dna_delta at ±100 bps post-evaluation | `WS3_H6C_phase0_audit.md` |
| 2026-04-25 ~16:44 | CHECKPOINT 1 → user GO | — |
| 2026-04-25 ~16:45–16:48 | P1: revert P0 instrumentation + add cap + 26 unit tests + det gate byte-identical PASS + chop regression 0/2000 | `675bc80` |
| 2026-04-25 22:50:37 | PID 20944 launched post-H6.C live gate | — |
| 2026-04-25 22:55:37 | H6.C live 5-min: saturation_hit = 0/276, steady-state 0% << 5% gate → **PASS** | `logs/bot_ui_2026-04-25_post_H6C.err` |
| 2026-04-25 ~16:58 | Commit `675bc80` + push trio to origin/feat/tensor-engine | — |
| **2026-04-25 22:59:34** | **D5 anchor: PID 22484 launched** for 3-day Phase 1 tape accumulation | `logs/bot_ui_2026-04-25_D5_anchor.err` |

### 5.2 H6.A decision (commits local, not reverted, not pushed)

- **Keeps local.** User decision at 00:55: don't revert H6.A. It closes a real door (DNA recursion), tests green, byte-deterministic, technically valid — just partial, not wrong.
- **NOT pushed** until H6.B fix is scoped/landed. The commit message "closes Phase 0 bug-found" is partial and will be amended or superseded at push time to reflect "closes door #1 only".
- Revert is NOT the plan; the plan is layering H6.B on top.

### 5.3 H6.B fix decision cadre — EXECUTED 2026-04-25 12:47–14:25 CET (FAIL on live)

**Verdict applied**: Option B from H6.B audit §7 — **cap `scale` at `DEPTH_SCALE_MAX = 5`** in `src/projector/chess.py:644-650`. Code landed in `e02f3a6`.

**Pre-live checkpoints — all PASS**:
- 6/6 unit tests `tests/projector/test_depth_scale_cap.py` PASS.
- Determinism gate `--max-ticks=1000` on 2026-04-20: byte-identical PASS.
- Chop regression on 2026-04-20: saturation 0/1000 ticks (0%, well below 5% target).
- Signal-preservation regression on 2026-04-21 lively-chop top-decile (11/95 windows): ratio 1.0 (post = pre), 0 windows annihilated, cap fired 0× — cap dormant on cold-start synthetic replay (forecast emove p99 ≈ 1.92 bps, no scale > 5 reachable).

**Live gate — FAIL**: PID 7560 ran ~4 min on Bybit live tape (224 ticks). Cap fires 1.34M× (working), but steady-state saturation (ticks ≥ 100) = 100% (125/125), 0 trades, equity flat. Pathology displaced to door #3.

**Per protocol** (`< 5% steady-state = push H6.A + H6.B`, `≥ 5% = check-in, no push`): user decision at check-in (2026-04-25 ~16:25 CET) = **(1) no push** + **(2) commit H6.B local** + **(4) update HANDOFF** + **new H6.C box with proper budget**.

### 5.4 Door #3 — CLOSED by H6.C (`675bc80`)

Live instrumentation refined the H6.B audit's hypothesis on which door #3 candidate was active:
- **#3a `event_bps`** — REFUTED on chop tape. Live measurement: `max_event = 0.0 bps` on all 234 P0 projections. `pending_events` is empty without news-classifier hits. *Latent in news/cascade regimes per the original H6.B note, but not active in the chop pathology that defined the live FAIL.*
- **#3b `base_strength` × intent_drift** — CONFIRMED active driver. `intent_drift = c × base_strength × signed_intent` with c up to 0.55 (whale_dna), and `base_strength = conviction × calibrator.skill_multiplier(tid) × effective_gain`. The `skill` term is unbounded in code — feedback loop calibrator → prediction → calibrator. P0 live instrumentation observed `max_dna` per projection drift from 65 (warmup) to 2,744 (steady state, max=3,364) over 234 projections.
- **#3c consensus aggregation weight concentration** — not assessed; not needed since #3b explained the entire ramp.

**Fix**: Option A from H6.C audit §5 — cap `dna_delta = dna_fn(ctx)` at ±`DNA_DELTA_CLAMP_BPS = 100.0` post-evaluation, single-knob, ≤8 LOC. Bounds the entire DNA output regardless of which internal term explodes. Mirror of H5.A/H6.A/H6.B clamp pattern.

**Live verification**: PID 20944, 276 ticks, **0/276 saturation_hits** (0% steady-state, far below 5% gate). Cap engaged on 211/276 projections (76%). Saturation chain confirmed shut.

### 5.5 I6.I1 (per-tribe curve persistence)

Still **out of scope**. Was not needed to land H6.A/B/C — static analysis + projection-level instrumentation sufficed for all three Phase 0 audits. If door #4 surfaces post-Phase-1 (e.g., on cascade-regime tape), I6.I1 may become a prerequisite.

### 5.6 H6.C box — EXECUTED 2026-04-25 16:35–17:00 CEST (live PASS, push complete)

| Phase | Allocated | Used | Result |
|---|---|---|---|
| P0 audit (static + live instrumentation) | 75 min | ~10 min | BUG-FOUND, #3a refuted, #3b confirmed |
| P1 fix + tests + det gate + chop regression | 60 min | ~5 min | All gates PASS |
| P2 live 5-min gate | 15 min | 5 min | PASS (0% steady-state) |
| P3 close (commit + push + handoff + D5 anchor) | 15 min | ~10 min | Complete |
| **Total H6.C box** | **2h30 strict** | **~30 min actual** | **CLEAN PASS** |

The box closed dramatically under-budget because P0 live instrumentation (rather than a longer static analysis) cut directly to the answer in 4 min of measurement. Phase 1 trades stayed at 0 not because of saturation but because the consensus directional score is genuinely small in chop — confirmed correct behavior.

### 5.7 Door #4 watchlist (only if Phase 1 reads abnormal saturation post-D5)

Phase 1 (2026-04-28 evening) consumes 3 days of D5-anchored tape. If saturation shows up again in that tape, candidate #4s in priority:
- **`perceived_sum` upstream** — `_world_shocks_for_tribe` magnitudes. If world shocks have unbounded amplitude, `arb_dna.shock_response = 0.55 × perceived_sum` could re-explode dna_delta even with cap. Cap `perceived` at the source. **Cascade-regime tape needed for this audit.**
- **calibrator skill update rule** — if skill compounds positively on bot-self-prediction-matches-bot-self-move, the calibrator is broken semantically. Separate audit (H6.D scope).
- **`consensus aggregation weight concentration`** (#3c, deferred from H6.C) — if a single tribe dominates the weighted mean of `step_bps_list`, consensus could spike independent of per-tribe step_bps. Triage by reading per-tribe weight distribution.

These are NOT promoted unless Phase 1 surfaces them. Default expectation: chain is shut.

---

## 6. Phase 1 entry checklist — to run on 2026-04-27 evening (NOT before)

1. Run the combined check (§4.6). Confirm BOT_ALIVE + ≈72h uptime + 0 tracebacks.
2. **Capture the saturation-rate evolution** — this is the H5/H6 Phase 1 primary readout:
   ```bash
   grep "saturation hit" logs/bot_ui_2026-04-24_post_H5A.err | awk '{print $1}' | cut -c1-13 | sort | uniq -c > /tmp/sat_per_hour.txt
   head /tmp/sat_per_hour.txt
   ```
3. Stop the bot cleanly:
   ```powershell
   powershell.exe -Command "Stop-Process -Id 12104 -Force"
   ```
4. Capture final tape byte size + sha256 for audit:
   ```bash
   for f in data/forecasts/2026-04-2{4,5,6,7}.jsonl; do echo "$f"; sha256sum "$f"; stat --format='size=%s' "$f"; done
   ```
5. Restart bot if continuing (decide at that point whether to archive pathological calibrator state first — see §3 calibrator-self-healing clause).
6. Run Phase 1 analyzers on the D5 tape:
   - `ws3_h5_attractor_analyze.py` (not yet written — to be built 2026-04-28). Reads `saturation_hit`, `abs_activity_bps`, `step_bps_signflips`, stratifies by hour/regime.
   - Q5/Q6 analyzers (I3.a/b readers) — **empty on this tape**, I3 still dormant. Will populate next restart.

---

## 7. Recovery procedures if bot died mid-run

**If PID 12104 no longer exists** on 2026-04-25 or 2026-04-26:

1. `stat --format='%y' data/forecasts/2026-04-2*.jsonl` to estimate death time.
2. `tail -80 logs/bot_ui_2026-04-24_post_H5A.err` for cause.
3. Decision tree:
   - Crash < 1h ago, no stack trace → Windows hiccup, restart with fresh log suffix (`_post_H5A_recovery_N`).
   - Stack trace present → capture, restart, flag for separate investigation.
4. Restart (adapt log suffix to recovery attempt):
   ```powershell
   powershell.exe -Command "Start-Process -FilePath python -ArgumentList '-m','scripts.run_bot_ui' -WorkingDirectory 'C:\Users\agent\Desktop\TRADING BOT' -WindowStyle Hidden -RedirectStandardOutput 'logs\bot_ui_2026-04-2X_recovery.log' -RedirectStandardError 'logs\bot_ui_2026-04-2X_recovery.err'"
   ```
5. **Do NOT reset the D5 clock**. Anchor stays 2026-04-24 23:30:21 CET.

---

## 8. Reference — files touched in the H5.I1 + I3 + H5.A chain

| Commit | Chip | Files |
|---|---|---|
| `b1c1f97` | H5.I1 | `src/projector/chess.py`, `src/core/types.py`, `src/audit/backscore.py`, `src/core/engine.py`, `src/replay/harness.py`, `tests/test_ws3_h5_instrumentation.py` |
| `fabc5f4` | I3.a | `src/audit/backscore.py`, `src/core/engine.py`, `src/replay/harness.py` |
| `0586a73` | I3.b | `src/audit/backscore.py`, `src/core/engine.py`, `src/replay/harness.py` |
| `2ffe035` | I3.c | `src/execution/paper.py`, `src/execution/shadow.py`, `scripts/run_bot_ui.py` |
| `f101800` | H5 Phase 0 audit | `research/phase11_diagnostic/WS3_H5_phase0_audit.md`, `research/phase11_diagnostic/_ws3_h1_I1_gate/` |
| `f0076bd` | **H5.A** | `src/core/types.py`, `src/projector/chess.py`, `tests/projector/test_chess_saturation.py` (new) |
| `f948e36` | Baseline | `data/baseline/pathological_2026-04-24_pre-H5A/` (MANIFEST.sha256, README.md, forecasts + calibrator state + err log) |
| `ff995a3` | H6 scope | `research/phase11_diagnostic/WS3_H6_SCOPE.md` |

Determinism gate: PASS byte-identical on H5.A (`--max-ticks=1000` on 2026-04-20 sprint).

---

## 9. Scope documents

- `research/phase11_diagnostic/WS3_H5_SCOPE.md`
- `research/phase11_diagnostic/WS3_H5_phase0_audit.md` (closed verdict)
- `research/phase11_diagnostic/WS3_H6_SCOPE.md` (fresh tonight)
- `research/phase11_diagnostic/WS3_I3_SCOPE.md`
- `research/phase11_diagnostic/WS3_H1_SCOPE.md`
- `research/phase11_diagnostic/PLAN.md`

---

## 10. One-line summary for a hurried future-you

Saturation chain SHUT (H5.A + H6.A + H6.B + H6.C); H6.C live PASS at 0% steady-state on 276-tick Bybit gate (PID 20944); all 5 commits pushed to `origin/feat/tensor-engine` tip = `675bc80`; D5 anchor live at 2026-04-25 22:59:34 CEST (PID 22484); Phase 1 read scheduled 2026-04-28 evening — DO NOT consume tape before then; door #3 closed via `dna_delta = max(-100, min(100, dna_fn(ctx)))` post-evaluation cap (the H6.B audit's `event_bps` hypothesis was REFUTED on chop; actual driver was `intent_drift × calibrator_skill_multiplier` feedback unbounded).
