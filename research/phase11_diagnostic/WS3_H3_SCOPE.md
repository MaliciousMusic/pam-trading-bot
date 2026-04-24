# WS3 H3 — Tribe correlation / effective N

**Phase 11 Diagnostic · Scope document (pre-implementation)**

| Item | Value |
|:--|:--|
| Status | Scope-first, opened 2026-04-24 after H1.I1 closed + Bybit venue swap |
| Predecessors | WS3 H1 (Phase 1 data-gated, 2026-04-28 earliest), H2 (closed 2026-04-23) |
| Operating rule | `MEASUREMENT_INFRA.md` §2 — H-acceptance reads `data/forecasts/<day>.jsonl`, not sprint |
| Acceptance bar | Aggregate directional hit ≥ 52 % @ 20 s across 3-day panel (`WS3_SCOPE.md` D3) |
| Standing rule | Audit-before-tune (`WS3_SCOPE.md` §9) — Phase 0 structural audit before intervention |
| Parallelism | Safe to draft + Phase-0 audit while H1 Phase 1 tape accrues (no shared code edits) |

## 0. Headline

The ChessProjector council ingests 24 tribe votes, aggregates them into
a single `net_pressure` / `agreement` pair
(`src/aggregator/voting.py:39-66`), and computes a "diversity"
multiplier from Shannon entropy over **tribe categories**
(`src/projector/chess.py:988-1016`).

**H3 claim.** The 24-tribe council has an *effective* rank
`N_eff « 24`. Tribes are clustered into strategy blocs that co-vote on
shared features (flow imbalance, liquidation tape, HTF trend), and the
aggregator treats every vote as independent. The consequence: stated
`agreement` and `diversity_score` over-estimate the true independent
information; the weighted sum in `net_pressure` is dominated by the
largest bloc, not by a balanced council.

**Diagnostic weight.** HIGH prior, upgraded from council vote-
redundancy finding 2026-04-18 (`project_council_redundancy.md`): 3
tribes produced near-identical signals on that day's tape. If a similar
pattern holds at the full 24-tribe scale, `diversity_score` and
`agreement` both mis-measure independence.

**What H3 does *not* claim.** H3 does not claim the individual tribes
are wrong or that the aggregator formula is buggy in isolation. It
claims the collective has fewer degrees of freedom than the formula
assumes, so downstream confidence and sizing read the council as more
independent than it is.

**Live pre-observation (2026-04-24 Bybit tape, 370 rows, ~3 min):**
`directional_score = 0.0` on every row while `signal_quality` varied
healthily (0.06 → 0.28). That means `|expected_move_bps| = 0` despite
non-trivial per-step uncertainty ≠ 1. Two candidate readings:

1. **Perfect sign-balanced blocs.** Half the council votes long with
   conviction X, half votes short with conviction X — the signed sum
   cancels to zero, the unsigned "activity" is large. Classical
   correlation-structure artifact.
2. **Venue-ingest gap.** A Bybit-specific normalizer shape causes some
   tribe cohort to produce identical anti-votes each tick.

Reading (1) is the H3 claim; reading (2) is a Phase-0 audit obligation
(P8 below). Until Phase 1 measurement separates them, treat the
observation as **Phase-0 evidence**, not Phase-1 proof.

## 1. Antecedents — what's already known at H3 open

- **Council vote-redundancy (2026-04-18).** On a sampled session, 3
  tribes produced near-identical signals. Recorded as project memory;
  acts as the empirical prior that motivated adding H3 to Phase 11.
- **H1 Phase 0 verdict.** Confidence-formula path audit found no blocking
  bug (`WS3_H1_path_audit.md`). Formula computes what its docstring
  says. H3 does not re-derive the formula; it asks whether the *inputs*
  to the formula over-count independent signal.
- **H2 closed.** Kernel gain sits at ~1.57, within clamp range. H3
  reads live-tape against a calibrated kernel, not a saturated one.
- **H1 Phase 1 schema instrumentation landed.** Post-I1
  `data/forecasts/<day>.jsonl` carries `signal_quality`,
  `directional_score`, `base_conf`, `diversity_score`, `diversity_mult`
  per anchor. H3 Phase 1 reads the **same** archive plus any H3.I1
  additions; no schema collision.
- **`_TRIBE_CATEGORY` coverage gap (Phase-0 pre-finding).** The
  mapping at `chess.py:70-88` covers 17 tribes; 7 of the 24 loaded
  tribes silently default to `"ta"` at `chess.py:1001`
  (`capitulation_buyer`, `cascade_amplifier`, `news_scalper`,
  `options_dealer_gamma`, `put_call_fader`, `stablecoin_flow_buyer`,
  plus any newly-added tribe without an explicit entry). This
  artificially inflates the TA category's mass and is a candidate
  bug on H3's critical path — see P6 below.

## 2. Phase 0 — Correlation-structure audit (gating)

Before any measurement commit ships, read the aggregator + diversity +
per-step aggregation path end-to-end for structural bugs. Verdict
outcome gates Phase 1: bug-found → fix commit → re-audit; clean or
suspected-without-root-cause → proceed to Phase 1.

**Method:** static code read, same pattern as `WS3_H1_path_audit.md`
and `WS3_H2_path_audit.md`. No sprint or live-tape measurement in
Phase 0. Output: `WS3_H3_path_audit.md` with the pair-check trace and
a verdict ∈ {bug-found, suspected, clean}.

**Pair checks to perform**:

| # | Check | Why it matters |
|:--|:------|:---------------|
| P1 | `aggregate_votes` computes `agreement = \|buy_mass − sell_mass\| / active_mass`. Each tribe's contribution is `weight × buy_frac` / `weight × sell_frac`. Two tribes with weight 0.1 each and identical buy_frac=0.8 contribute 0.16 to buy_mass — indistinguishable from one tribe with weight 0.2 and buy_frac=0.8. | Agreement treats correlated tribes as additive independent votes. If 4 TA tribes co-vote long with conviction 0.6 each, their combined mass is 4× a single tribe's — yet they're likely reading the same HTF feature. Agreement goes up, uncertainty goes down, signal_quality compounds — but information didn't quadruple. |
| P2 | `net_pressure = (buy_mass − sell_mass) / total_weight`. `total_weight` includes **abstainers** (`weight` of tribes with buy_frac + sell_frac = 0). `agreement` uses `active_mass = buy_mass + sell_mass`, which **excludes** abstainers. | Asymmetric denominators: net_pressure is suppressed by silent tribes but agreement ignores them. A council where 20 of 24 abstain and 4 are mass-opposed gets `net_pressure ≈ 0` (suppressed by the 20 denominators) AND `agreement = 0` (because the 4 are opposed). Symptomatically reads as "council disagrees strongly" when it actually reads as "4 tribes disagree, 20 are silent." Observable but under-diagnosed. |
| P3 | Per-step aggregation: `aggregate_votes(decayed_by_step[i], …)` at `chess.py:787`. `decayed_by_step` applies per-tribe horizon decay, so vote at step i from a 30-s-horizon tribe is scaled very differently from a 600-s-horizon tribe. | Mass-based agreement at step i reads the *decayed* masses, which scale differently per tribe. A long-horizon tribe's vote at step 250 contributes near-full mass; a short-horizon tribe's contributes ~0. Agreement at far horizons is effectively measured over a subset of tribes, not the full council. Whether that's correct-by-design or a mis-read of "how agreement should be interpreted across horizon" is a judgement call the audit must make explicit. |
| P4 | `expected_move_bps = Σ step_bps_i · decay_i` at `chess.py:793`. `step_bps_list` is the consensus cumulative curve (carried over from H2 path audit). `abs_activity = Σ \|step_bps_i\| · decay_i`. Under internal sign reversals in the cumulative path, `|emove|` can be ≤ `abs_activity` by a large margin. | Restated from H1 P6 for H3's purposes. If the council's cumulative curve zigzags but converges (common in chop), `directional_score = \|emove\| / abs_activity` is structurally small even when the *terminal* direction is clear. H3 observation (dir=0 universal) could be this zigzag-cumulative artifact rather than a genuine correlation finding. Audit must disentangle the two. |
| P5 | `_diversity_and_breakdown` (`chess.py:988-1016`) aggregates tribe masses into categories **before** computing Shannon entropy. Within-category mass is summed; the entropy sees "one chunk per category." | Intra-category tribe correlation is invisible to `diversity_score`. If `_TRIBE_CATEGORY["retail"]` has 3 tribes all co-voting long, their combined mass is one "retail" chunk in the entropy — the fact that it's 3 independent (or pseudo-independent) votes is erased. The metric literally cannot detect crowded intra-category trades. This is the strongest lead for H3 — the formula is measuring category diversity, not tribe diversity. |
| P6 | `_TRIBE_CATEGORY` covers 17 tribes; `chess.py:1001` defaults missing tribes to `"ta"`. 7 of the 24 loaded tribes fall through: `capitulation_buyer`, `cascade_amplifier`, `news_scalper`, `options_dealer_gamma`, `put_call_fader`, `stablecoin_flow_buyer`, plus any new additions without an entry. | Concrete Phase-0 bug candidate. These 7 tribes are not TA — they're cascade-regime, news, options, or stablecoin flow. Silently dumping them into TA inflates the TA category's mass by ~29 % of the loaded tribe count and mis-represents the council's true category distribution. Diversity Shannon entropy reads artificially as "TA-dominated." Fix surface is tiny (add 7 entries); impact on `diversity_score` is non-trivial. **Prefer bug-found verdict + standalone fix commit before H3 Phase 1.** |
| P7 | There is no `N_eff` / effective-rank quantity anywhere in the aggregator or projector today. `vote_count` at `voting.py:64` is the raw tribe count, not an independence-adjusted measure. | H3 Phase 1 measurement requires an `N_eff` proxy. Instrumentation-first: the live tape must persist per-tick per-tribe conviction vectors (or a lower-rank summary) so an offline analysis script can compute a correlation matrix across the loaded tribes and from it an effective rank. Payload cost must be bounded; see §3.1. |
| P8 | Live Bybit observation: `directional_score = 0.0` universal across 370 rows while `signal_quality ∈ [0.06, 0.28]`. Two candidate causes: (a) real sign-balanced bloc cancellation (H3-relevant), (b) Bybit-ingest gap causing some tribe cohort to produce identical anti-votes (Bybit-specific artifact, not H3). | Audit must rule out (b) before Phase 1 anchors against this tape. Concretely: sample 3-5 ticks from the live Bybit tape, inspect `pool_w` and per-tribe `(buy_frac, sell_frac)` vectors, and confirm the observed dir=0 is genuine sign cancellation across active tribes — not all tribes reading the same abstain or all tribes defaulting to a venue-mismatched feature. If (b), the finding is a Bybit ingress bug on a downstream feature path, not H3 evidence; file as a separate issue and let Phase 1 wait for a clean tape. |

**Expected audit output.** Either:
- **bug-found.** Concrete discrepancy (most likely P6 — the stale
  `_TRIBE_CATEGORY` coverage gap — or P8 if the Bybit dir=0 is a
  venue-specific ingest gap). Fix commit lands as a standalone change;
  H3 Phase 1 re-opens post-fix.
- **clean.** No structural bug; formula and aggregator compute what
  their docstrings say. Proceed to Phase 1 with the null hypothesis
  "the 24-tribe council has N_eff << 24 and `diversity_score` /
  `agreement` over-count independence."
- **suspected.** Behaviour plausibly wrong but requires live-tape
  measurement to confirm. Annotate and proceed to Phase 1 treating
  the suspected item as a candidate explanation for the findings.

**Landing discipline.** Per `WS3_SCOPE.md` §9, Phase 0 ships as a
docs commit only. Any bug-fix (particularly P6) surfaces as a
separate commit with its own determinism gate. Stacking audit + fix
is forbidden (same rule that forced R2 into A and B).

## 3. Phase 1 — Live-tape correlation measurement

### 3.1 Instrumentation (gating commit — H3.I1)

The live archive does not currently persist per-tribe convictions or
a correlation snapshot. Phase 1 opens with a single instrumentation
commit.

**Scope:** extend `PendingAnchor` / `Resolved` with a bounded per-tick
per-tribe conviction summary sufficient to compute an offline
correlation matrix. Two candidate shapes (pick one at
commit-design time; Phase 0 audit of the shapes is part of H3.I1):

- **Shape A (raw).** Persist `tribe_votes: dict[tribe_id → signed_conviction]`
  on each resolved anchor. 24 floats × ~21 k records/day ≈ 4 MB/day
  before compression. Enables the cleanest downstream analysis.
- **Shape B (summary).** Persist two scalars: `n_active_tribes`,
  `conviction_concentration_herfindahl` (= `Σ (w_i · |c_i|)²` normalized
  by `(Σ w_i · |c_i|)²`). 2 floats/record ≈ 0.3 MB/day. Lighter, less
  analytically flexible.

Default: **Shape A** if the payload budget allows (measure post-H1.I1
daily jsonl growth to decide). The 4 MB/day cost is small against a
14-day retention window and buys every future analysis we might
want without a second instrumentation round.

**Determinism cost:** zero if the new fields are derived from
values already computed in the projector path (they are — each
tribe's `(v, signed, strength)` tuple exists in `initial_votes` at
`chess.py:991`). Determinism gate must PASS on the instrumentation
commit before Phase 1 measurement ships.

**Backfill:** impossible. Historical `data/forecasts/<day>.jsonl`
does not carry per-tribe convictions; H3 acceptance runs against
post-instrumentation days only. This is a real scope cost and
shifts the H3 panel dates forward from the H1 panel dates by the
H3.I1 landing delta.

### 3.2 Measurement

On the post-instrumentation live-tape archive, compute:

- **Per-tribe signed-conviction matrix** over a day: rows = ticks,
  columns = tribes. Values = `signed_conviction` (buy_frac − sell_frac
  × weight or similar — pick the representation at H3.I2 design time).
- **Pairwise correlation matrix** over the 24 tribes: Pearson ρ on
  the signed-conviction time series. Visible clusters in the matrix
  are the "blocs" H3 claims exist.
- **Effective rank (N_eff).** Three candidate proxies (report all
  three, they measure different things):
  - `N_eff^(corr) = trace(Σ) / λ_max(Σ)` — Kaiser rule style.
  - `N_eff^(part) = exp(H(p))` where `p_i = λ_i / Σ λ_j` and H is
    Shannon entropy — "participation ratio."
  - `N_eff^(95%) = min k s.t. Σ_{i≤k} λ_i / Σ λ_j ≥ 0.95` — variance-
    explained threshold.
- **Per-category intra-correlation.** For each category in
  `_TRIBE_CATEGORY` with n ≥ 2 tribes, mean pairwise ρ within the
  category vs. mean pairwise ρ across categories. If within > across,
  `diversity_score`'s category-aggregation (P5) is masking real
  correlation.
- **Conditional: on the trade-firing subset** (anchors where
  `forecast_conf` ≥ `decision.min_conf`). Is N_eff different when
  the council actually fires? Distinguishes "correlation is always
  there but decisions are gated" from "correlation collapses exactly
  at the decision boundary."

**Script:** `scripts/ws3_h3_corr_analyze.py` (new), reads
post-H3.I1 `data/forecasts/<day>.jsonl`, emits the three N_eff
measures + correlation heatmap + per-category intra-corr table to
stdout + JSON artefact to `research/phase11_diagnostic/_ws3_h3_corr/`.

### 3.3 Expected signatures if H3 true

- `N_eff^(corr)` ≤ 8 (i.e. ≤ ⅓ of the 24 tribes' apparent count).
- `N_eff^(part)` ≤ 10.
- `N_eff^(95%)` ≤ 12.
- Mean within-category pairwise ρ ≥ 0.6; mean across-category ρ ≤ 0.3.
- Visible bloc structure: at least 2 tribe pairs with ρ ≥ 0.85.
- On the trade-firing subset, `N_eff` is **not** materially higher
  than overall (if it were, the gating already handles correlation —
  H3 would be a correlation story without an intervention handle).

### 3.4 Kill criterion

**H3 is falsified** if `N_eff^(part)` ≥ 16 AND mean within-category
pairwise ρ < 0.3 AND no tribe pair has ρ ≥ 0.7 on the full-day
panel. In that case the council's 24 tribes *are* largely
independent; `diversity_score` and `agreement` are measuring the
right quantity. H3 closes as null; any remaining forecast-quality
gap passes to H4 (not yet opened) or surfaces as a request for a
different framing.

## 4. Intervention options (ship only one per H3 pass)

Per `WS3_SCOPE.md` §7 execution rules. Each option targets the
correlation mechanism Phase 1 identifies.

### 4.1 Option A — N_eff-weighted aggregation

Replace raw `net_pressure` / `agreement` with correlation-adjusted
equivalents. Compute a rolling (e.g. last-1000-tick) correlation
matrix `Σ`; derive per-tribe weights `w_i^eff = w_i / Σ_j |ρ_{ij}|`
or similar; use `w_i^eff` in place of `w_i` in `aggregate_votes`.

**Prefer A** if Phase 1 shows a clean bloc structure with 2-4
dominant blocs and low intra-bloc dispersion. The weighting
naturally deflates over-represented blocs.

**Determinism cost:** rolling state addition (correlation matrix
cache). Needs a persistence contract similar to
`projector_gain.json` / `tribe_skill.json`. Determinism gate
re-run with deterministic seeding is non-trivial.

### 4.2 Option B — Fix `_diversity_and_breakdown` at the tribe level

Replace the category-aggregate Shannon entropy with a tribe-level
Shannon entropy over the dominant-side mass contributions:

```
active_tribe_masses = [w_i × v_i.buy_frac if consensus_sign >= 0
                       else w_i × v_i.sell_frac
                       for i in active_tribes]
probs = [m / Σ m for m in active_tribe_masses]
diversity_score = entropy(probs) / log(len(probs))
```

**Prefer B** if Phase 1 shows within-category ρ ≥ 0.6 but
cross-category ρ low — confirms P5 is the dominant mechanism.

**Determinism cost:** pure-function change; trivial gate re-run.

### 4.3 Option C — Fix `_TRIBE_CATEGORY` coverage (P6 standalone)

Not really an "intervention option" — more a correctness fix that
the Phase-0 audit will likely surface as bug-found. Add the 7
missing tribes to `_TRIBE_CATEGORY` with correct category labels
(and a linter/test asserting every loaded tribe has an explicit
entry). Impact on `diversity_score` distribution is measurable;
may partially close H3 without needing A or B.

**Prefer C first** as a prerequisite of A or B, regardless of
Phase-1 outcome. Stacks cleanly (the fix is independent of
whichever larger intervention lands next).

**Determinism cost:** data change (constant dict); gate must re-run
because Shannon entropy output changes. Small.

### 4.4 Combined-option discipline

Per `WS3_SCOPE.md` §7: one intervention commit per H3 pass (apart
from C, which is a standalone correctness fix that should precede
any larger intervention). If A lifts aggregate but leaves within-
category correlation unaddressed, B is the natural follow-up. Do
not bundle A + B in one commit.

## 5. Acceptance criteria

Per `WS3_SCOPE.md` §10:

**(a)** H3 Option A or B (with C assumed landed) ships and panel-
aggregate directional hit at 20 s ≥ 52 % across (post-H3.I1 ≥ 3-day
panel). Measurement determinism gate must pass and no panel day can
materially regress.

**(b)** Both A and B ship, each measured in isolation, and neither
clears 52 %. H3 is written up as null; Phase 11's remaining H-list
contains no better lead on the confidence-quality gap — escalate to
a scope re-open.

**Partial-lift stopping rule.** Same as H1 §5: if A lands in the
48-51 % range, pause before running B. Partial lift admits the same
two readings (H3 partially true, additive elsewhere vs. H3 near-
exhausted). Do not autopilot through the option list.

**Explicitly not an acceptance criterion:** post-friction net PnL.
H3 is signal diagnosis only (D3).

## 6. Instrumentation catalog (new work)

| Commit | Scope | Determinism impact | Lines |
|:--|:--|:--|:--|
| H3.0  | `WS3_H3_path_audit.md` — Phase 0 audit report | Docs only | N/A |
| H3.I1 | Persist per-tribe signed conviction on `Resolved` (Shape A or B) | None if derived from `initial_votes` in-place | ~30 |
| H3.I2 | `scripts/ws3_h3_corr_analyze.py` — correlation/N_eff analysis | N/A (post-hoc) | ~180 |
| H3.C  | `_TRIBE_CATEGORY` coverage fix (if P6 bug-found) | Small — Shannon output changes | ~15 |
| H3.A / H3.B | One of the two larger options, if Phase 1 justifies | One commit each; gate between | ~40-80 |

All instrumentation commits ship **before** any mechanism change
(§9 standing rule). H3.0 is a docs commit; H3.I1 is a chore commit;
H3.I2 is a docs/analysis commit; H3.C is a chore/fix commit; H3.A
and H3.B are feature commits.

## 7. Execution sequence

```
  H3.0 (Phase 0 audit) ── bug-found? ── yes ──▶ standalone fix (typically H3.C)
          │ no                                             │
          ▼                                                ▼
  H3.I1 (auditor instrumentation)                    re-audit post-fix
          │                                                │
          ▼                                                │
  determinism gate pass                                    │
          │                                                │
          ▼                                                ▼
  3+ days of production run         (merge back into main flow once
  accumulate live-tape data         H3.0 verdict = clean/suspected)
          │
          ▼
  H3.I2 (analysis script) — Phase 1 measurement
          │
          ▼
  Signature matches §3.3? ── no ──▶ H3 null → scope re-open
          │ yes
          ▼
  Choose A or B per the Phase 1 bloc structure
          │
          ▼
  Ship single intervention → measurement determinism gate
          │
          ▼
  Panel-aggregate ≥ 52 % @ 20 s? ── yes ──▶ H3 closes accepted
          │ no                               │ partial (48-51 %)
          ▼                                  ▼
  fall-through to the other option      pause, reviewer D4
```

## 8. Parallelism with H1

H3 Phase 0 and H1 Phase 1 are **safely parallel**:

- H1 Phase 1 consumes `data/forecasts/<day>.jsonl` only — no code
  edits required while the tape accrues.
- H3 Phase 0 is a docs-only static read. No overlap with any file
  H1 Phase 1 touches.
- H3.I1 (instrumentation commit) would ship **after** H3.0 verdict
  and before Phase 1 measurement. Landing H3.I1 while H1 Phase 1
  tape is still accruing is fine — the existing H1 fields remain
  backward-compatible; H3.I1 is purely additive.

The H3 Phase 1 measurement starts its own ≥ 3-day panel clock on
H3.I1 landing date. That clock is independent from H1's 2026-04-28
earliest start.

## 9. Open questions flagged for the audit

1. **P8 disambiguation.** Is the live Bybit `directional_score = 0.0`
   universal observation a correlation finding or a venue-ingest
   gap? The audit must sample concrete per-tribe votes from a live
   tick and report back in `WS3_H3_path_audit.md` §3.
2. **Shape A vs. B payload cost.** After H1.I1 is running for a full
   Bybit day, confirm daily jsonl growth fits the 4 MB/day budget
   Shape A implies. If the full day blows past 10 MB, reconsider
   Shape B.
3. **Correlation window length.** Is the right window for the live
   correlation cache (if Option A ships) 1 min / 10 min / 1 hour?
   Out of Phase-0 scope; flagged for H3.I2 design.

---

*Scope landed 2026-04-24. Phase 0 audit is the next H3 commit.*
