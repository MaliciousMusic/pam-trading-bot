# WS3 I3 — Live-tape plumbing / additive-schema instrumentation

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item | Value |
|:--|:--|
| Status | Scope-first, opened 2026-04-24 after H5 scope + H3.C ship |
| Predecessors | WS2 executor-parity audit (`cb512fc`), WS3b warmup/persistence audit (`05e3b05`), WS5 harvest-path audit (`c46670a`) — all recommend I3 plumbing as their next step |
| Operating rule | `MEASUREMENT_INFRA.md` §2 — H/Q-acceptance reads `data/forecasts/<day>.jsonl`, not sprint |
| Schema discipline | `WS3_H1_SCOPE.md` §11 — additive-only fields, no contract change to existing readers |
| Determinism impact | Zero (every field mirrors a value already computed upstream) |
| Parallelism | Safe to ship alongside H1 / H3 / H5 Phase 1 tape accrual — no shared edit in-flight |

## 0. Headline

Four parallel audits (WS2, WS3b, WS5 — landed 2026-04-24 as a cohort;
plus the in-flight H5 Phase 0) each end with the same recommendation
shape: **the intervention is deferred but the live-tape measurement
is blocked on a small set of additive schema fields that exist
upstream but are not persisted to `data/forecasts/<day>.jsonl`**.

I3 is the umbrella plumbing chip that consolidates those field
additions in one coherent patch. Nothing mechanical changes; no
decision logic, no projector math, no aggregator formula. I3 is
the *measurement surface* for Q3 / Q5 / Q6 and a secondary
support for H5 Phase 1.

**Why consolidate.** Each audit alone asked for 1-3 new jsonl
fields. Shipping them one chip per audit is four round-trips of
schema-protection review, four determinism-gate runs, four PR
reviews. One coordinated chip lands the whole measurement surface
at once with a single review and a single gate run.

**What I3 does *not* touch.** I3 does not:
- modify the confidence formula (`signal_quality × directional ×
  diversity_mult`),
- change the aggregator (`aggregate_votes`, `net_pressure`,
  `agreement`),
- alter the decision engine (`DecisionEngine.decide`),
- change any existing jsonl field name, type, or semantics,
- remove any existing field,
- perturb the regime classifier state machine.

Every listed consumer (Q3 / Q5 / Q6) has a recommendation section
in its audit that spells out "this measurement is pending I3."
I3 is the scope doc those sections point to.

**One sub-chip per consumer.** Because each audit has an
independent landing cadence (Q3 waits for WS5 alignment, Q6
wants to go early, Q5 depends on calibrator state), I3 is split
into three sub-chips that ship in the order each consumer is
ready. All three are additive and independent — the gate runs
once after the last sub-chip lands.

## 1. Antecedents — what's already known at I3 open

- **Forecast schema (`src/core/types.py:88-`).** Already carries
  `conviction_boost` and `harvest_mass_ratio` (lines 145-146).
  Not mirrored onto `PendingAnchor` / `Resolved` in
  `src/audit/backscore.py`.
- **Projector output (`src/projector/chess.py:740-780, 887-896`).**
  Computes `conviction_boost`, `harvest_mass_ratio`, and the
  path-integrity flag (`driver_skill_avg` → `path_integrity_ok`
  gate). The Forecast is stamped; the auditor captures only
  `expected_move_bps`, `forecast_conf`, `net_pressure`, and the H1.I1
  decomposition block.
- **Regime snapshot (`src/core/engine.py:382-`).** `regime_snapshot`
  exists at tick-dispatch time; `regime_label` is already passed
  to `decision.decide(…, regime_label=regime_label)`. It does
  **not** flow to `auditor.record_tick(…)`. WS3b audit §4
  flagged this as the priority-1 plumbing.
- **Shadow executor (`src/execution/shadow.py:59-76`, `_ShadowTrade`
  dataclass).** Records `realized_bps` (shadow's fill model) but
  not `realized_bps_paper_model` (a parallel computation against
  `PaperExecutor._fill_price`). WS2 audit §5.2.2 spec'd this
  extension; the values needed exist in `PaperExecutor`'s
  `_fill_price` but are not invoked per-shadow-close today.
- **H5.I1 tick-forecast fields (scope landed 2026-04-24).**
  `abs_activity_bps` + `step_bps_signflips` are *projector-output*
  fields, not plumbing fields. They ride H5.I1's own commit. I3
  does not include them — distinct surface, distinct review.
- **WS3_H1_SCOPE.md §11 schema-protection contract.** All new
  fields must be additive (`Optional[...]` with default `None`);
  all pre-I3 readers (UI, replay harness, retrospective analysis
  scripts) must stay functional with the fields absent.

## 2. I3.a — Harvest-mass plumbing (Q5 blocker)

**Motivates:** `WS5_harvest_path_audit.md` §5.2.3 — live-tape
measurement of the harvest-mass conviction-boost path.

**What to add** on `PendingAnchor` and `Resolved`
(`src/audit/backscore.py`):

```python
# WS5 live-tape harvest-path measurement surface. None when the
# projector couldn't populate them (pre-I3 Forecast objects,
# unit-test fixtures, sprint-without-calibrator). All three are
# already present on `core.types.Forecast` and flow through
# unchanged at resolution time.
conviction_boost: float | None = None
harvest_mass_ratio: float | None = None
path_integrity_ok: bool | None = None
```

**Source of each field:**
- `conviction_boost` — `Forecast.conviction_boost`
  (chess.py:887-896, already computed at chess.py:740-747).
- `harvest_mass_ratio` — `Forecast.harvest_mass_ratio`
  (chess.py:887-896, computed at chess.py:733-746).
- `path_integrity_ok` — derived from `driver_skill_avg ≥ 0.7`
  (chess.py:840+). If `driver_skill_avg` is None (harvest path
  not yet fired), `path_integrity_ok` is also None.

**Surface in `record_tick`:** `auditor.record_tick(..., forecast,
...)` already receives the Forecast object; new fields are
populated via attribute-read. No signature change.

**Projected jsonl size delta:** 3 scalars / tick ≈ 0.45 MB / day
at 21 k tick rate.

**Determinism cost:** zero. Every value mirrors extant Forecast
state.

**LOC:** ~15 in backscore.py (struct additions + assign
statements in record_tick).

**Commit message pattern:**
```
feat(audit): mirror conviction_boost / harvest_mass_ratio /
path_integrity_ok onto PendingAnchor/Resolved (WS5 live-tape
measurement surface, I3.a)
```

## 3. I3.b — Regime-label plumbing (Q6 blocker)

**Motivates:** `WS3b_warmup_persistence_audit.md` §4 rec. 1 —
persistence-span and regime-transition-weighted analysis on the
≥3-day H1 panel.

**What to add** on `PendingAnchor` and `Resolved`:

```python
# WS3b regime-transition measurement surface. Regime label at
# tick_ts (anchor dispatch) and at realized_ts (anchor resolve).
# The (open, close) pair lets the offline script count how many
# anchors straddled a regime transition.
regime_label_at_tick: str | None = None
regime_label_at_resolve: str | None = None
```

**Source of each field:**
- `regime_label_at_tick` — `regime_snapshot["label"]` at
  engine.py:382, pass through to `auditor.record_tick`.
- `regime_label_at_resolve` — requires a small addition: the
  auditor's resolution loop (`backscore.py:record_tick`) needs
  access to the current regime label at resolution time. Pass
  `regime_label` through every call that invokes resolution.

**Surface change:** `ForecastAuditor.record_tick` signature
gains `regime_label: str | None = None` (with None-default to
preserve existing callers). Any tests / scripts that call
`record_tick` directly remain green.

**Projected jsonl size delta:** 2 short strings / tick ≈ 0.3
MB / day (labels ≤ 12 chars: `"normal"`, `"cascade"`, `"chop"`,
`"trend"`).

**Determinism cost:** zero — `regime_snapshot["label"]` is a
pre-existing string.

**LOC:** ~10 in backscore.py + ~2 in engine.py.

**Commit message pattern:**
```
feat(audit): stamp regime_label_at_tick / _at_resolve onto
PendingAnchor/Resolved (WS3b transition measurement surface,
I3.b)
```

## 4. I3.c — Shadow paper-model parity plumbing (Q3 blocker)

**Motivates:** `WS2_executor_parity_audit.md` §5.2.2 — per-close
measurement of the shadow-vs-paper fill bias.

**What to add** on `_ShadowTrade` (`src/execution/shadow.py:59-76`):

```python
# WS2 executor-parity measurement surface. The bps a shadow
# close would have realized if it had used PaperExecutor's
# _fill_price machinery against the same MarketState. None
# when the paper executor couldn't be consulted (e.g. shadow
# running in isolation without a PaperExecutor reference).
realized_bps_paper_model: float | None = None
```

**Where to populate:** in the shadow-close path in `shadow.py`,
call `PaperExecutor._fill_price` as a static utility against the
same MarketState / side / size that the shadow used. Compute the
resulting bps (vs. entry) and assign to the new field. The
original `realized_bps` (shadow's own fill) is unchanged.

**Architectural note.** WS2 §5.2.3 flagged a design question:
either (a) delete shadow's `_fill_price` and delegate to paper's,
or (b) keep both and log the delta. I3.c takes path (b) — log
both — because the measurement answer informs which direction to
choose. Do not conflate the measurement plumbing with the design
decision.

**Projected tape size delta:** 1 scalar / shadow close. The
shadow close rate is much lower than the tick rate (typically
< 5 % of ticks produce a close per tribe × 24 tribes). Bounded
at ~0.1 MB / day in practice.

**Determinism cost:** non-zero unless strictly guarded. The
`PaperExecutor._fill_price` call draws adversarial latency
samples via its RNG. To keep shadow's own RNG stream
deterministic, the paper-model call must route through a
**separate, seeded RNG** or the harden layer must be disabled
for the parallel call. Recommendation: introduce a
`PaperExecutor._fill_price_deterministic(...)` variant that
bypasses the harden layer, and call that from shadow. The
harden layer's bias is measurable separately via its own
telemetry.

**LOC:** ~25 in shadow.py + ~10 in paper.py
(`_fill_price_deterministic`).

**Commit message pattern:**
```
feat(shadow): log realized_bps_paper_model per _ShadowTrade close
(WS2 executor-parity measurement surface, I3.c)
```

## 5. Shared-schema consequences

### 5.1 Auditor serialization

`src/audit/auditor.py` (or the jsonl persist path inside
`backscore.py`, depending on how the tree landed) walks
`Resolved` fields at persist time. Adding fields via dataclass
`field(default=None)` means existing serializer code sees them
automatically; no persist-path change required.

### 5.2 Replay harness

`src/replay/harness.py` consumes post-resolution records for
sprint determinism. It reads specific fields; it does not walk
unknown fields. New fields are ignored cleanly.

### 5.3 UI reader

`src/ui/static/*.html` reads a small subset of the jsonl for the
live dashboard. The reader is field-name-keyed; unknown fields
pass through. No UI change required unless we decide to surface
one of the new fields visually (recommended **no**: I3 is a
measurement-surface change, not a UI change).

### 5.4 Existing retrospective scripts

- `scripts/ws3_h1_decomp_analyze.py` reads H1.I1 fields by name.
  New fields invisible.
- `scripts/ws3_h2_step4_live_attribution.py` reads attribution
  fields. New fields invisible.
- Any `scripts/ws3_<consumer>_*.py` script written *after* I3
  lands can read the new fields by name.

### 5.5 Determinism gate

One gate-run after the last I3 sub-chip lands. Because every
field is additive and derived from extant state, the gate's
SHA-256 of `decisions.jsonl` is unchanged. If the gate byte-
identity breaks, we've misidentified a field as "derived" when
it actually introduces state — investigate before landing.

## 6. Execution sequence

```
  I3.0 (this doc) — scope commit, docs-only
         │
         ▼
  I3.a — conviction_boost / harvest_mass_ratio / path_integrity_ok
         │   (additive on PendingAnchor + Resolved)
         ▼
  determinism gate re-run (expected: byte-identical)
         │
         ▼
  I3.b — regime_label_at_tick / _at_resolve
         │   (additive on PendingAnchor + Resolved + record_tick sig)
         ▼
  determinism gate re-run
         │
         ▼
  I3.c — realized_bps_paper_model
         │   (additive on _ShadowTrade + new _fill_price_deterministic)
         ▼
  determinism gate re-run
         │
         ▼
  I3 ships; Q3 / Q5 / Q6 unblocked for live-tape measurement
         │
         │  (Phase 1 measurement writes on the same clock the
         │   H1 / H3 / H5 panels are keyed to — earliest
         │   2026-04-28 local on the current Bybit clock.)
         ▼
  ws3_q3_executor_bias_analyze.py / ws3_q5_harvest_ablation_analyze.py
   / ws3_q6_regime_transition_analyze.py — written as needed
```

Sub-chip ordering rationale:
- **I3.a first** because it's the purest mirror (no signature
  change, no cross-module coordination). Smallest blast radius.
- **I3.b second** because it adds a parameter to `record_tick`;
  has to touch engine.py. Slightly larger surface.
- **I3.c last** because it requires the new
  `_fill_price_deterministic` helper in paper.py and a harden-
  layer carve-out. Largest design decision.

Each sub-chip is its own commit with its own gate run; any single
one can be rolled back without affecting the others.

## 7. Acceptance criteria

Per `WS3_SCOPE.md` §10 interpreted for plumbing-only work:

**(a) Schema contract.** Every new field is additive, typed
`Optional[...]`, defaults None. No existing field changes name,
type, or semantics. Pre-I3 readers (listed in §5) remain green.

**(b) Determinism contract.** `verify_determinism.py --day <any>`
produces byte-identical `decisions.jsonl` before and after each
sub-chip lands. Any deviation blocks landing; investigate the
source of state introduction.

**(c) Downstream unblocked.** After all three sub-chips land, a
smoke script reads a freshly-generated `data/forecasts/<day>.jsonl`
and confirms all eight new fields (3 from I3.a, 2 from I3.b, 1
from I3.c via shadow records, + H5.I1's 2 when that lands) are
populated on non-null rows. The smoke is not a full Q3/Q5/Q6
analysis — those are separate, post-I3 tasks.

**Explicitly not an acceptance criterion:** any forecast-quality
metric (directional hit, aggregate-hit, PnL). I3 does not change
forecast behaviour; measuring forecast quality pre/post-I3 is
meaningless by design.

## 8. Parallelism

I3 reads schema but writes nothing observable to the projector
forecast path. It is safe to ship in parallel with:

- **H1 Phase 1 tape accrual** — no code touched; consumer-side
  only.
- **H3.C ship** (completed 2026-04-24) — disjoint surface
  (`chess.py:70-88, 1001` vs. I3's `backscore.py`, `shadow.py`,
  `engine.py:382`).
- **H3 Phase 1 opening** (if H3.I1 lands after I3) — H3.I1 adds
  `tribe_votes` persistence to `Resolved`; purely additive, no
  conflict with I3's additions.
- **H5 Phase 0 audit** (in-flight 2026-04-24) — docs-only Phase
  0; no code conflict.
- **H5.I1 tick-forecast extensions** — `abs_activity_bps` +
  `step_bps_signflips` land on a different Forecast-level
  surface; I3 mirrors only fields already on Forecast, so I3 and
  H5.I1 are sequentially independent.

## 9. Open questions flagged for sub-chip implementation

1. **I3.a — `path_integrity_ok` semantics.** `driver_skill_avg`
   is None when the harvest path hasn't fired (sprint,
   pre-calibrator). Is `path_integrity_ok = None` the right
   sentinel or should it be `True` (vacuous pass, nothing to
   fail)? WS5 §5.2.3 implies None; confirm at implementation
   time by checking the analysis script's join behaviour.
2. **I3.b — resolve-time regime label dispatch.** The auditor's
   resolve loop runs inside `record_tick`; the current regime
   label is available via `regime_snapshot` at that same tick.
   Confirm whether "regime at resolve" means "at the tick where
   the anchor matured" (current `regime_snapshot`) or "at
   `target_ts`" (which is ≤ current tick, historical). Pick the
   former; document explicitly.
3. **I3.c — harden-layer carve-out vs. bypass.**
   `_fill_price_deterministic` is the carve-out path. Does the
   WS2 measurement *want* the harden-layer bias included (more
   realistic shadow-model comparison) or excluded (clean
   fill-model comparison)? WS2 §5.2.2 spec says "the same
   `_fill_price` machinery PaperExecutor uses" — ambiguous.
   Recommended default: **exclude** the harden layer; measure
   the fill-model bias in isolation. If harden-layer bias is
   requested later, it's a trivial second scalar.
4. **Tape growth sanity check.** After all three sub-chips plus
   H5.I1's two fields, the projected per-day tape growth is
   ~5 MB additive over the current baseline. Confirm this
   doesn't push retention-window costs past the 14-day budget
   before landing I3.c. Baseline measurement to be taken from
   the first full post-Bybit-swap day (2026-04-25 midnight+).

---

*Scope landed 2026-04-24 alongside H5 scope + H3.C ship. I3.a
is the next I3 commit once a single determinism-gate re-run on
tip confirms H3.C delta is green.*
