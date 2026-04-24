# WS3 H3 Phase 0 — Correlation-structure audit

**Scope:** Per `WS3_H3_SCOPE.md` §2, read the aggregator + diversity +
per-step aggregation path end-to-end and resolve pair-checks P1–P8 as
**bug-found / suspected / clean**. No intervention commit lands on
the back of this audit. Any fix found here ships as its own commit
(`WS3_SCOPE.md` §9 landing discipline).

**Method:** Static code read against `feat/tensor-engine` tip
(`a9a220c`), supplemented by numerical inspection of the live
`data/forecasts/2026-04-24.jsonl` produced under the post-venue-swap
bot. No Phase 1 measurement executed; the tape is used only to
constrain the P8 disambiguation (scope §9 Q1).

**Verdict summary:**

| # | Area | Verdict |
|:--|:--|:--|
| P1 | `agreement` treats correlated tribes as additive | **clean-by-design** — formula does what docstring says; Phase 1 must still measure bloc structure |
| P2 | Asymmetric `net_pressure` / `agreement` denominators | **clean-by-design** — intentional; abstainer-suppression is documented |
| P3 | Per-step decay applied to mass aggregation | **clean-by-design** — Phase 1 reads horizon-stratified anyway |
| P4 | `directional = |emove| / abs_activity` under step oscillation | **SUSPECTED-BUG** — live tape shows `ratio = abs_activity/|emove| = 99.000` **exactly** on 300 samples, reproduced from pure-alternating kernel under decay=0.98 / steps=250 |
| P5 | `diversity_score` over categories erases intra-category correlation | **clean-by-design** — formula matches docstring; this is the mechanism H3 Phase 1 must measure |
| P6 | `_TRIBE_CATEGORY` coverage gap | **BUG-FOUND** — 7 of the 24 loaded tribes silently default to `"ta"`. Ship standalone fix (H3.C) before H3 Phase 1 |
| P7 | No `N_eff` anywhere in live code | **clean-by-design** — this is the motivator for H3.I1 instrumentation |
| P8 | Live Bybit `directional_score ≈ 0` observation | **resolved** — not a venue-ingest gap; explained by P4 mechanical artifact, **not** by a correlation story |

**Headline consequences for H3 execution:**

1. **P6 fix (H3.C) ships as its own commit before H3 Phase 1** —
   tiny data-only change, re-runs determinism gate, trivially
   auditable.
2. **P4 is escalated out of H3's scope as a new diagnostic** —
   the cumulative-path oscillation mechanism driving the 99.0×
   ratio is a structural property of how `step_bps_list` is
   assembled, not of the correlation-aggregator path that H3
   targets. Flag as H5 candidate (`WS3_SCOPE.md` hypothesis slot
   reserved); do **not** bundle into H3.
3. **H3 Phase 1 measurement plan remains valid** — per-tribe
   signed-conviction correlation analysis (Shape A per
   `WS3_H3_SCOPE.md` §3.1) is independent of P4; the N_eff analysis
   reads `initial_votes` directly, not the cumulative `step_bps`
   path. H3.I1 / H3.I2 can land on schedule.
4. **P8 no longer requires disambiguation.** The live tape itself
   resolved it: `expected_move_bps` values are non-zero (e.g. –0.180
   bps), the ingest is healthy, and `directional_score ≈ 0.0101 =
   1/99` is a deterministic output of P4. No separate venue-ingest
   investigation is opened.

## 1. Audit inputs

| Item | Source |
|:--|:--|
| Tip commit | `a9a220c` (`feat/tensor-engine`, post-Bybit-swap) |
| Aggregator | `src/aggregator/voting.py:39-66` |
| Projector confidence path | `src/projector/chess.py:780-820` |
| Diversity / category map | `src/projector/chess.py:70-88, 988-1016` |
| Settings | `config/settings.yaml` — `projector_steps: 250`, `confidence_decay: 0.98`, `tick_seconds: 1` |
| Loaded tribes | `config/tribes/*.yaml` — 24 files |
| Live tape | `data/forecasts/2026-04-24.jsonl` — 1 166 rows under Bybit |

## 2. Pair checks

### P1 — `agreement` treats correlated tribes additively (voting.py:50-56)

**Check.** Two tribes with `weight = 0.1` each and `buy_frac = 0.8`
contribute `2 × 0.08 = 0.16` to `buy_mass` — indistinguishable from
one tribe with `weight = 0.2, buy_frac = 0.8`.

**Trace.** `voting.py:51` performs `buy_mass += v.weight *
v.buy_frac` once per vote in a simple accumulation loop; there is no
normalisation by distinctness. `agreement = |buy_mass - sell_mass| /
active_mass` (line 56) consumes the accumulated masses without any
rank-reduction step.

**Verdict.** **clean-by-design.** The docstring at `voting.py:19-22`
explicitly describes this behaviour ("`buy_mass = Σ weight_i *
buy_frac_i`"). No structural bug — but the behaviour **is** the
mechanism H3's main claim targets. H3 Phase 1 instrumentation
(per-tribe conviction persistence) is required to measure whether
co-voting tribes inflate mass beyond what independence would
justify. No pre-Phase-1 fix.

### P2 — Asymmetric `net_pressure` / `agreement` denominators (voting.py:53-56)

**Check.** `net_pressure = (buy_mass − sell_mass) / total_weight`
uses `total_weight`, which **includes abstainers** (line 45: every
vote contributes to `total_weight` regardless of side). `agreement
= |buy_mass − sell_mass| / active_mass` excludes them (line 55:
`active_mass = buy_mass + sell_mass`).

**Failure mode.** 20 abstaining tribes + 4 mass-opposed tribes ⇒
`net_pressure ≈ 0` (suppressed by 20 silent denominators), AND
`agreement = 0` (4 opposed tribes disagree). Symptomatically reads
"council silent AND disagreeing" — but the truth is "silent
majority, minority disagrees."

**Trace.** Docstring at `voting.py:26-27` explicitly flags this as
intentional: "Abstaining tribes add weight to the denominator only,
correctly suppressing net_pressure when a silent majority holds."
The asymmetry is a **deliberate design choice** to decouple "how
much directional push does the council produce" from "how coherent
is the active council."

**Verdict.** **clean-by-design.** The asymmetry is intended and
documented. H3's concern here is secondary: the abstainer-
suppression *cooperates* with P1's bloc over-counting (if silent
tribes are silent because a bloc of non-silent tribes dominates,
both `net_pressure` and `agreement` can read the bloc as the whole
council). This cross-term is what Phase 1's per-tribe conviction
matrix will expose.

### P3 — Per-step decay on mass aggregation (chess.py:786-789)

**Check.** `step_aggs[i] = aggregate_votes(decayed_by_step[i], …)`.
`decayed_by_step` applies per-tribe horizon decay so that the
"mass" contribution of a 30-s-horizon tribe at step 200 is
effectively zero, while a 600-s-horizon tribe's mass is near-full.

**Failure mode.** Agreement at far-horizon steps is measured over
a *subset* of the council — the short-horizon tribes are
effectively absent. `signal_quality`, which integrates
`(1 - uncertainty) · kernel_i` over all steps, thus reads
"coherence of the surviving council at each horizon."

**Verdict.** **clean-by-design.** This is the same observation
recorded in `WS3_H1_path_audit.md` P1 under different framing.
The behaviour is intended — short-horizon tribes shouldn't
dominate long-horizon confidence. H3 Phase 1 measurements are
horizon-stratified by construction (scope §3.2), so the
horizon-dependence is surfaced at measurement time, not at
audit time.

### P4 — **SUSPECTED-BUG** — `directional` under cumulative-path oscillation (chess.py:793-798)

**Formula under scrutiny.**

```python
expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))
abs_activity = sum(abs(b) * w for b, w in zip(step_bps_list, kernel))
directional = abs(expected_move_bps) / abs_activity if abs_activity > 0 else 0.0
```

**Live observation.** Over 300 non-trivial rows from
`data/forecasts/2026-04-24.jsonl` (filter: `|emove| > 0.01`),
the implied ratio `abs_activity / |emove|` is **exactly 99.000**
(median, p90, and max all equal 99.000; zero dispersion).
`directional_score` on these rows is exactly `0.010101010…` ≈
`1/99`. Sample:

| horizon | emove (bps) | abs_activity (bps) | ratio |
|:--|:--|:--|:--|
| 1s | –0.180 | 17.832 | 99.000 |
| 1s | –0.184 | 18.179 | 99.000 |
| 1s | +0.056 | 5.500 | 99.000 |
| 1s | –0.165 | 16.316 | 99.000 |

(One outlier row at ratio = 1.000 corresponds to a near-flat
cumulative — `|emove| ≈ abs_activity`, the non-oscillating
limit.)

**Analytical reproduction.** Under `decay = 0.98`, `n_steps =
250`, and `step_bps_list = [+X, –X, +X, –X, …]` (perfect
alternation):

- Signed sum: `Σ (–1)^i · 0.98^i = 1 / (1 + 0.98) = 0.5018`
- Absolute sum: `Σ 0.98^i = (1 – 0.98^250) / 0.02 = 49.6798`
- Ratio: `49.6798 / 0.5018 = 99.000`

The 99.000 attractor is **exactly** reproduced by the pure-
alternation limit. The live tape sitting on that attractor
means `step_bps_list` is perfectly alternating in sign at
tick-level almost every tick.

**Why this is suspected-bug, not a correlation finding.**

The P4 check was originally framed in `WS3_H3_SCOPE.md` §2 as
"sign reversals in the cumulative path can make `|emove| ≤
abs_activity` by a large margin." What the tape shows is
stronger: the path is on the **pure-alternation attractor**
across ~300 diverse ticks, which means the cumulative `step_bps`
is flipping sign *every single step* almost deterministically.
That is not the emergent behaviour a correlation-clustered
council would produce — blocs would give sign-persistent
segments, not per-step flips. Something inside the
`step_bps_list` construction path (`chess.py:~750–780`,
`step_bps` / `steps_bps` / `decayed_by_step` pipeline) is
producing systematic per-step alternation.

Candidate causes (not verified — deliberately out of H3 scope):

1. Per-tribe walk delta vs. cumulative is being double-differenced.
2. A sign-flip in `decayed_by_step` accumulation.
3. Tribe votes canceling each other exactly one step out of phase
   due to a timestamping off-by-one.

**Verdict.** **SUSPECTED-BUG.** Live tape is anomalously
deterministic (zero dispersion of the ratio across 300 diverse
rows, exactly matching the pure-alternation analytical limit).
This is *not* an H3 correlation finding — it's a
`step_bps_list`-construction finding.

**Landing discipline.**
- H3 Phase 1 can still ship: the N_eff measurement reads
  `initial_votes` (the undecayed raw votes) directly, not
  `step_bps_list`, so P4 does not contaminate H3.I1 / H3.I2.
- P4 opens its own diagnostic slot. Proposed name: **H5 —
  cumulative-path oscillation**. Entered into
  `WS3_SCOPE.md` hypothesis list as a new H-thread with HIGH
  diagnostic priority (it structurally caps `base_conf = signal_quality
  × directional` at `signal_quality / 99` on the attractor,
  which is likely the dominant confidence-gap explanation
  for Phase 11's aggregate-hit shortfall — bigger than H1 or H3
  combined).
- No P4 fix lands before the H5 scope is written. Respect the
  audit-before-fix rule that covered H1 and H3.

### P5 — `_diversity_and_breakdown` entropy over categories (chess.py:988-1016)

**Check.** `category_mass` is built by accumulating `pool_w ×
buy_frac` / `pool_w × sell_frac` per tribe into buckets keyed on
`_TRIBE_CATEGORY[tid]` (lines 999-1005). The Shannon entropy at
line 1013 is computed over the **category-level** masses
(`active_shares = [m[key] for m in category_mass.values() …]`),
not over the per-tribe masses.

**Failure mode.** If 3 tribes in the same category co-vote long,
their combined mass is one "category chunk" in the entropy.
Intra-category correlation is **invisible** to `diversity_score`
by construction.

**Verdict.** **clean-by-design.** The formula matches its
docstring ("Shannon entropy over tribe categories"). This
behaviour is the **H3 Option B target** (`WS3_H3_SCOPE.md`
§4.2) — swap to per-tribe entropy if Phase 1 shows intra-
category ρ >> inter-category ρ. No pre-Phase-1 fix.

### P6 — **BUG-FOUND** — `_TRIBE_CATEGORY` coverage gap (chess.py:70-88, 1001)

**Check.** `_TRIBE_CATEGORY` is a 17-entry dict literal
(lines 70-88). Line 1001: `cat = _TRIBE_CATEGORY.get(tid,
"ta")` — missing tribes **silently default to `"ta"`**.

**Loaded tribes vs. mapping.**

```
config/tribes/*.yaml            _TRIBE_CATEGORY entry
─────────────────────────────────────────────────────
basis_arb                       arb ✓
capitulation_buyer              MISSING → "ta"
cascade_amplifier               MISSING → "ta"
contrarian_funding_fader        arb ✓
copy_trader                     retail ✓
cross_venue_arb                 arb ✓
cta_systematic_trend            MISSING → "ta"
leverage_cycle                  hedger ✓
liquidation_hunter              event ✓
mev_frontrunner                 mev ✓
microstructure_prober           microstructure ✓
mm_algo                         mm ✓
news_fader                      event ✓
news_scalper                    MISSING → "ta"
options_dealer_gamma            MISSING → "ta"
options_hedger_proxy            hedger ✓
passive_whale_twap              whale ✓
put_call_fader                  MISSING → "ta"
retail_app_newbie               retail ✓
retail_degen_leveraged          retail ✓
retail_grid_bot                 mm ✓
stablecoin_flow_buyer           MISSING → "ta"
swing_trend_follower            ta ✓
ta_day_trader                   ta ✓
─────────────────────────────────────────────────────
Mapped   : 17
Defaulted: 7  (→ inflate "ta" category by 7/24 ≈ 29 %)
```

**Proposed corrections** (audit recommendation, not landed in
this commit):

| Tribe | Category — rationale |
|:--|:--|
| capitulation_buyer | `event` — reacts to liquidation / flush tape, same family as `liquidation_hunter` |
| cascade_amplifier | `mev` — amplifies in-progress liquidation cascades, MEV-style extraction |
| cta_systematic_trend | `ta` — **stays `ta`** (the default was coincidentally correct here); add explicit entry to defeat the silent default |
| news_scalper | `event` — fast reaction to news tape, same family as `news_fader` |
| options_dealer_gamma | `hedger` — dealer-side gamma / delta hedging, same family as `options_hedger_proxy` |
| put_call_fader | `hedger` — fades put/call skew, hedger-adjacent |
| stablecoin_flow_buyer | `retail` — stablecoin-rail inflow is primarily retail on-ramp |

Net effect on category distribution at steady state:

- `"ta"` drops from a silent 17 / 24 inflation back to 2 tribes.
- `"event"` gains 2, `"hedger"` gains 2, `"mev"` gains 1, `"retail"`
  gains 1.
- Shannon entropy over 9 populated categories goes **up**
  (diversity redistributes); `diversity_score` shifts higher on
  average.

**Fix surface.** ~10 lines in `chess.py`:

1. Add the 7 entries to `_TRIBE_CATEGORY` at lines 70-88.
2. Remove the silent default at line 1001 — replace `_TRIBE_CATEGORY.get(tid, "ta")`
   with `_TRIBE_CATEGORY[tid]` to make future additions fail loudly.
3. Optional: add a lint / startup-assertion that every loaded
   tribe id is present in `_TRIBE_CATEGORY`.

**Verdict.** **BUG-FOUND.** Concrete, reproducible, fixable in
isolation. Ship as H3.C — a standalone commit with its own
determinism gate — before H3.I1. Determinism impact: Shannon
entropy output distribution changes, but the change is
deterministic and a fresh determinism-gate run will re-anchor
cleanly. Partial-lift expected but not measured at audit time
(partial-lift measurement is a Phase-1 question).

### P7 — No `N_eff` quantity exists today (voting.py:64, chess.py entire)

**Check.** `Aggregate.vote_count = len(votes)` (voting.py:64) is
the raw tribe count; no correlation-adjusted proxy exists
anywhere.

**Verdict.** **clean-by-design.** This is the *motivation* for
H3.I1 instrumentation — H3 Phase 1 must persist per-tribe signed
convictions so an offline script can compute N_eff. No pre-
Phase-1 fix required; the missing quantity is scope §3.

### P8 — Live Bybit `directional_score ≈ 0` — **RESOLVED by P4**

**Original framing.** `WS3_H3_SCOPE.md` §0 logged `directional_score
= 0.0` universal on the 370-row pre-snapshot, with two candidate
readings: (a) sign-balanced bloc cancellation (H3-relevant), (b)
venue-ingest gap (Bybit artifact).

**What the current tape shows.** Extended to 1 166 rows at audit
time. `directional_score` is **not** universally zero — median
0, stdev 0.20, max 1.0, null rate 13 %. The modal non-zero value
is `0.010101010…` = 1 / 99, not arbitrary small noise. Per-row
`expected_move_bps` is non-zero (median ≈ 0.1 bps, occasional
± 0.2 bps) — the ingest is *not* producing abstain-only votes or
flat curves.

**Both candidate readings fail.**

- Reading (a) — sign-balanced bloc cancellation — would produce
  a *distribution* of `ratio = abs_activity / |emove|`, not an
  exact attractor at 99.000. Rejected.
- Reading (b) — venue-ingest gap — would produce identical
  per-tribe votes each tick, likely abstain or all-one-side.
  Rejected: the tape shows directional variance across rows and
  `net_pressure` varies sign.

**True reading.** P4's cumulative-path-oscillation artifact
mechanically fixes the ratio at 99 under `decay = 0.98, steps
= 250`. The "dir = 0 universal" observation was a numerical
artifact of looking at `directional_score = 1/99 ≈ 0.01` rounded
to early display precision — **not** a correlation story.

**Verdict.** **resolved.** No open P8 question. The Bybit venue
swap is healthy. H3's correlation claim is neither confirmed
nor refuted by this observation — it remains a Phase-1
measurement obligation, now un-contaminated by the artifact.

## 3. What this audit changes

| Before audit | After audit |
|:--|:--|
| H3 Phase 1 gated on P6 bug-fix | Unchanged — P6 confirmed bug-found, H3.C ships standalone |
| P8 disambiguation required as a scope-§9 open question | Resolved — no separate Bybit audit needed |
| Primary confidence-gap suspect: correlation bloc / `diversity_score` masking | **Reframed** — P4 cumulative-path oscillation is a stronger mechanical suspect; H3's claim is still possible but is now a **secondary** driver behind P4 |
| H3 scope covers "confidence gap from correlation" | H3 scope narrows: **correlation-only**. The cumulative-path artifact surfaces as a new diagnostic (H5) |

## 4. Commits to land on the back of this audit

1. **H3.0 (this doc)** — docs-only commit.
2. **H3.C** — `_TRIBE_CATEGORY` coverage fix, per P6. Standalone;
   determinism gate re-runs; ~10 lines. Lands before H3.I1.
3. **H5 scope doc** — new scope doc for the P4 cumulative-path
   diagnostic. Does **not** bundle into H3; separate execution.
   To be drafted next (after H3.C ships).

Phase 1 measurement (H3.I1, H3.I2) remains on hold until H3.C
ships and the determinism gate confirms green. No schedule
slippage: H3 Phase 1 was already gated on a 3-day post-
instrumentation panel, so the H3.C + determinism-gate delta
fits inside that clock.

## 5. Open questions deferred to H3 Phase 1

1. **Does P6's fix alone close part of H3?** Possible: redistributing
   mass out of the silent `"ta"` inflation may lift
   `diversity_score` enough that `diversity_mult` rises measurably.
   Measure at H3.I2 time after Phase 1 accumulates ≥ 1 day of
   post-H3.C tape.
2. **Does P4 invalidate H3's kill criterion?** Scope §3.4 uses
   N_eff^(part), within-category ρ, and per-pair ρ — none depend
   on the cumulative-path oscillation. P4 does **not** invalidate
   H3's kill criterion, but it does mean that closing H3 alone
   (even fully) cannot fix the confidence gap if P4 is the
   dominant mechanism. Acceptance-criterion interaction deferred
   to post-H5-scope.
3. **Shape A vs. Shape B payload decision.** Still pending a
   full-Bybit-day jsonl size measurement; re-evaluate at H3.I1
   commit-design time.

---

*Phase 0 audit landed 2026-04-24. Next: H3.C commit (P6 fix), then
H5 scope doc (P4 diagnostic), then H3.I1 (Phase 1 instrumentation).*
