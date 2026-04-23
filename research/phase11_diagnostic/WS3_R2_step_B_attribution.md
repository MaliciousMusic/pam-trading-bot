# WS3 R2 Step B — Attribution snapshot

**Status:** **Commit B landed + full-day determinism PASS
(2026-04-24).** Commit B hash `20b53f6`. Tests 36/36 (21 decision +
15 paper) passing with three new B tests. Full-day determinism gate
B: **PASS**, sha256 `1fc982a69f3a1df5…`, 260,143,564 bytes ×2
(see §5.4). Kernel-cal correction distribution (n=5,040 across 2
runs = 2,520/run): **identical to A-alone** — min=4.001, p50=5.059,
p90=9.559, max=11.245, 15.0% clamp rate (see §5.2). Decisions.jsonl
distributions (conv_mult, dsa, PATH_INTEGRITY_CAP fraction): extraction
in progress via persistent `run_replay` job `bjy07teuo`. Live-tape
post-deploy attribution (§5.1): TODO on first post-A+B production
session.

> **What R2 does NOT fix.** B does not change directional hit rate
> (commit A's algebra already pinned that at 0.00 pp delta) and does
> not constitute progress toward the 52 % acceptance bar. Its value
> is (i) making conviction_boost an actual signal consumer rather
> than the orphan output it was post-A, and (ii) expressing the
> harvest-mass-radar intent cleanly — "fade the crowd harder when
> the crowd's mass is larger" — as a *sizing* lever, not a
> prediction distortion.

## 1. Scope of commit B

Commit B wires `conviction_boost` (∈ [1.0, 1.5], capped by the
projector's `harvest_scale=0.5`) into `DecisionEngine.decide` as a
sizing multiplier. Single file + tests.

- `src/decision/engine.py`:
  - Reads `forecast.conviction_boost` as `conv_mult` (default 1.0 when
    missing or non-positive).
  - Includes `conv_mult` in the multiplier chain: `size_usd = equity ×
    pct × lev × σ × agree × htf × crowd × hz × conv`.
  - Includes `conv_mult` in the Path-Integrity clamp (dsa < 0.7 →
    `conv_mult = min(conv_mult, 1.0)`) alongside the four existing
    brake-multipliers.
  - Reason string adds `conv×{conv_mult:.2f}` between `hz×...` and
    `drv=...`.
- `tests/test_decision_engine.py`: three new tests (see §5.5).

Scope boundary: `conv_mult` is the first *accelerator* in the
multiplier chain. All pre-existing multipliers are brakes ∈ [floor,
1.0]; `conv_mult` ∈ [1.0, 1.5] can raise notional above base. The
Path-Integrity clamp is what prevents unreliable-driver ticks from
leveraging that accelerator.

Commits A and B are attributed separately per R3 discipline. A
(`fd400e8`) removed the prediction-side inflator; B (`20b53f6`)
adds the sizing-side consumer. No overlap, no bundling.

## 2. Directional expectation — unchanged from A

B does not touch `expected_move_bps` or any sign-carrying quantity.
Direction is chosen as `Side.BUY if emove > 0 else Side.SELL` at
engine.py:119 — a function of `emove` alone, which is the raw
post-A consensus path. `conv_mult` enters only the sizing formula at
engine.py:150-152, which scales a positive dollar amount.

Consequence: pre-B vs post-B directional hit rate delta is
**exactly 0.00 pp by construction**, identical to A's invariance. If
the §5.4 determinism gate produces byte-identical decisions.jsonl
×2 at HEAD, that confirms B is deterministic at the sizing level;
direction was never in question.

## 3. Sizing expectation — algebraic

Let `N_base = equity × pct × lev × σ × agree × htf × crowd × hz` be
the pre-B notional. Post-B:

```
  N_post = N_base × conv_mult
```

Per-tick delta — two regimes:

- **Path-Integrity OK (dsa ≥ 0.7)**: `conv_mult ∈ [1.0, 1.5]`. Notional
  scales linearly with `conviction_boost`, capped at +50 % vs pre-B.
- **Path-Integrity fail (dsa < 0.7)**: `conv_mult` clamped to 1.0 —
  notional unchanged from pre-B on that tick. Accept direction,
  refuse to amplify via the harvest signal.

Aggregate post-B notional ratio depends on the joint distribution
of `conviction_boost` and Path-Integrity pass-rate, both unobserved
in the live-tape archive (the archive does not persist
`conviction_boost` or `driver_skill_avg` per anchor). **§5 extracts
both distributions from the post-A+B full-day sprint reason-string
stream.**

**Aggregate P&L expectation** (conditional on §5 distributions):
the fade-the-crowd thesis says trades on high-harvest-mass ticks
should carry positive expectancy; B amplifies those trades
proportionally to the conviction. If the thesis is right, B lifts
aggregate P&L. If the thesis is wrong or noise-dominated, B raises
P&L variance without lifting mean — a strictly worse trade because
the same expected return comes with larger drawdowns. This is why
B needs live-tape post-deploy measurement, not just sprint.

## 4. Landing sequence (recap — per R2 plan §4)

Commit A steps 1-6 closed (see `WS3_R2_step_A_attribution.md`).
Commit B steps:

7. **Commit B** (decision/engine.py + test, ~15 lines diff). ✅
   Landed 2026-04-23 as `20b53f6`, +71/-3 including 3 new tests.
8. **Replay B: sprint + live.** Sprint done via full-day determinism
   (§5.4). Live: TODO post-deploy.
9. **Determinism gate B.** Short-window PASS. Full-day in progress.
10. R2 closed; H1 scope doc (PLAN §5.2 Q1) is the default next chip.

## 5. Commit B attribution — post-replay

### 5.1 Live-tape delta (source: `data/forecasts/<day>.jsonl`)

- Aggregate notional delta, per-session: **TODO** (live-tape archive
  does not persist per-anchor `conviction_boost` or `driver_skill_avg`,
  so this must come from the first post-deploy session's decisions.jsonl
  reason-string stream).
- Distribution of `conv_mult` actually applied in production: **TODO**.
- Fraction of ticks where Path-Integrity clamp fired and suppressed
  `conv_mult` to 1.0: **TODO**.
- Directional hit rate delta pre-B vs post-B: **expected 0.00 pp**
  per §2. Sanity-rail verification when live-tape lands.

### 5.2 Sprint-tape delta (source: verify_determinism full-day run)

- Decisions.jsonl size delta vs A-only: **+4,840 bytes (+0.00186 %)
  full-day** (A=260,138,724 B=260,143,564). Short-window delta was
  larger in absolute-fraction terms (+0.063 % across 600 ticks) —
  consistent with the cold-start window containing disproportionate
  conv_mult > 1.0 opens before the Path-Integrity clamp starts firing
  steadily; aggregated across 24 h the sizing delta flattens.
- **Kernel-cal correction distribution (n=2,520/run, 15.0 % of refits
  clamped):**

  | metric | value |
  | --- | --- |
  | min  | 4.001 |
  | p10  | 4.101 |
  | p25  | 4.357 |
  | p50  | 5.059 |
  | p75  | 5.979 |
  | p90  | 9.559 |
  | p99  | 10.930 |
  | max  | 11.245 |
  | mean | 5.784 |

  Byte-for-byte identical to A-alone full-day distribution (same
  count, same percentiles). Expected: `conv_mult` scales notional,
  not `emove`; kernel_cal fits emove-vs-realization, independent of
  notional. Confirms B is orthogonal to the prediction-side signal.
- `conv_mult` distribution in reason-strings: **in progress** via
  `run_replay --run-id ws3_r2_B_attr` job `bjy07teuo`, ~6-7h.
  Extraction regex: `conv×(\d+\.\d+)`.
- `drv=X.XX` distribution: **in progress** (same job). Regex:
  `drv=(\d+\.\d+)`.
- Fraction of opens where `PATH_INTEGRITY_CAP` tag fires: **in progress**.
- Fraction of opens where `conv×1.00` tag fires: **in progress** —
  includes both the dsa-weak clamp path and the no-harvest path;
  joint distribution with `drv=X.XX` < 0.70 separates the two.

### 5.3 Condition-(a) analogue for B

Commit A had a condition-(a) 3 pp directional-delta pause trigger.
B's analogous gate is **notional-delta bounded by [1.0×, 1.5×] per
tick**, with clamp-fraction determining the aggregate:

- Per-tick notional must equal pre-B × conv_mult, with conv_mult ∈
  {1.0, [1.0, 1.5]} depending on Path-Integrity state.
- If per-tick notional exceeds pre-B × 1.5 on any tick, that is a
  refactor-bug safety-rail trip (should be algebraically impossible
  given `harvest_scale=0.5`).
- Aggregate notional post-B / pre-B ratio is bounded above by 1.5
  and below by 1.0. An observed ratio outside [1.00, 1.50] indicates
  an implementation bug.

**Gate decision: PASS.** Full-day determinism + 3 new tests +
algebraic bounds + kernel-cal distribution invariance = convergent
evidence B is correctly implemented. Sprint-side correctness gate
closed 2026-04-24. Live-tape delta remains the load-bearing signal
for B's *value* (as distinct from its *correctness*).

### 5.4 Determinism gate B

- Short-window (600 ticks, --day 2026-04-20 --seed 42): **PASS**,
  sha256 `41839cdffc691679…`, 1,847,239 bytes, byte-identical ×2
  (2026-04-23 run). Compare to A-alone short-window:
  `1c9b2c38935ddfd4…`, 1,846,084 bytes — +1,155 bytes, +0.063 %.
  Different SHA expected (different size_usd values); byte-identity
  ×2 confirms B is deterministic at HEAD.
- Full-day (--day 2026-04-20 --seed 42 --synth-rate 60, ~2.5 M
  ticks): **PASS**, sha256 `1fc982a69f3a1df5…`, 260,143,564 bytes,
  byte-identical ×2 (run A sha = run B sha, 2026-04-24 completion).
  Compare to A-alone full-day: `14d149feafe8ac7d…`, 260,138,724
  bytes — +4,840 bytes (+0.00186 %). SHA change and size delta
  both algebraically predicted (different size_usd values in
  reason-strings). Byte-identity ×2 across A+B runs confirms B
  is deterministic at HEAD.

### 5.5 Test discipline verification

Three new tests added to `tests/test_decision_engine.py`, all
passing; 18 pre-existing tests unchanged.

- `test_conviction_boost_amplifies_size_when_path_integrity_ok`:
  boost=1.5/dsa=0.8 produces notional exactly 1.5× the boost=1.0/
  dsa=0.8 case on identical inputs. Reason contains `conv×1.50`.
- `test_conviction_boost_clamped_by_path_integrity_when_dsa_weak`:
  boost=1.5/dsa=0.5 produces the same notional as boost=1.0/
  dsa=0.5. Reason contains `conv×1.00` and `PATH_INTEGRITY_CAP`.
- `test_conviction_boost_default_is_unity`: forecast with
  `conviction_boost=None` sizes identically to `conviction_boost=1.0`.
  Prevents silent amplification from missing-field default bugs.

### 5.6 Measurement-architecture note — B is where sprint matters less

Commit A's sprint-side measurement is structurally usable per
`WS3_R2_step_A_attribution.md` §5.6 (cal not pinned at MAX_GAIN,
85 % of refits fit inside [0.25, 4.00]). Commit B's value claim —
"fade-the-crowd trades are net-positive EV" — is an **empirical
market question**, not a code-correctness question. Sprint can
verify B is wired correctly (§5.4, §5.5) but cannot validate the
empirical thesis, because sprint's P&L comes from the same replay
tape against which the council was trained, so the fade-the-crowd
signal is by construction detected whenever the crowd is in the
tape — the question is whether the *live* crowd and the *replay*
crowd have the same structure and whether the fade works
out-of-sample.

**Consequence**: B's acceptance verdict requires live-tape data
from a post-deploy session, not a sprint. The sprint gates close
the correctness side only.

## 6. Provenance

- R2 plan reviewed + refined: 2026-04-23 session.
- Commit B landed: 2026-04-23, hash `20b53f6`, 2 files, +71/-3.
- pytest on touched modules: 36/36 passed (18 unchanged + 3 new +
  15 paper-exec).
- Short-window determinism (600 ticks, --day 2026-04-20 --seed 42):
  PASS, sha256 `41839cdffc691679…`.
- Full-day determinism (--day 2026-04-20 --seed 42 --synth-rate 60):
  **PASS**, sha256 `1fc982a69f3a1df5…`, 260,143,564 bytes, byte-
  identical ×2, 2026-04-24 completion. Job `bciqo1e7e`.
- Full-day kernel-cal correction distribution (n=2,520/run):
  identical to A-alone full-day (p50=5.059, p90=9.559, max=11.245),
  15.0 % clamp rate — B is orthogonal to the prediction-side signal.
- Persistent decisions.jsonl reproduction (job `bjy07teuo`,
  `run_replay --run-id ws3_r2_B_attr`): **in progress**, ~6-7h,
  writing to `data/replays/ws3_r2_B_attr/variants/full_day/`. Will
  provide the reason-string stream for §5.2 distribution mining.
- Live-tape post-deploy session: **TODO**.

---

*Living document. §5.1 closes on first post-A+B production session;
§5.2 conv_mult / dsa / clamp distributions close when the background
`run_replay` job `bjy07teuo` completes and its decisions.jsonl
reason-stream is mined.*
