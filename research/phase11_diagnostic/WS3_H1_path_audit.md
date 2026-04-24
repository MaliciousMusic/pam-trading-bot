# WS3 H1 Phase 0 — Confidence-formula structural audit

**Scope:** Per `WS3_H1_SCOPE.md` §2, read the
`ChessProjector.project` confidence-formula path end-to-end and
the six pair-checks P1-P6 against the on-disk source. Rule out
structural bugs before any Phase 1 instrumentation lands.

**Method:** Static code read — no sprint or live-tape measurement
executed for this audit. Every line citation was verified against
the on-disk source as of commit `6d9875a` (tip of
`feat/tensor-engine` at audit time).

**Verdict:** **CLEAN.** Six pair-checks resolved. No structural
bug found that justifies a pre-H1 fix commit. Two pair-checks (P1
decay-aware uncertainty, P2 path-efficiency penalty) are
**clean-by-design** — the formula computes what its stated intent
describes, but the intent itself has behavioural consequences that
**Phase 1 horizon-stratified measurement** must surface. One
pair-check (P6) arrived pre-audit as a bug-level concern and
**collapsed** once the exact code path was read — the scope doc's
P6 hypothesis was based on an incomplete read of the H2 audit
trace.

**Consequence for H1 execution:**

- No fix commit lands before H1.I1.
- H1 proceeds directly to Phase 1 instrumentation (`PendingAnchor`
  / `Resolved` decomposition fields) per `WS3_H1_SCOPE.md` §3.1.
- Phase 1 measurement must include horizon stratification as
  already committed in scope §3.2 — this is now doubly-required
  because P1 and P2 behaviours are horizon-dependent by construction.

## 1. Headline code path (chess.py:791-812)

The confidence formula, verbatim from
`src/projector/chess.py:791-812`:

```python
kernel = [self.confidence_decay ** i for i in range(n_steps)]          # 791
k_sum = sum(kernel) or 1.0                                              # 792
expected_move_bps = sum(b * w for b, w in zip(step_bps_list, kernel))   # 793
signal_quality = sum(                                                   # 794
    (1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)        # 795
) / k_sum                                                               # 796
abs_activity = sum(abs(b) * w for b, w in zip(step_bps_list, kernel))   # 797
directional = abs(expected_move_bps) / abs_activity if abs_activity > 0 else 0.0  # 798
base_conf = max(0.0, min(1.0, signal_quality * directional))            # 799
# …                                                                     # 801-806 comment
consensus_sign = 1.0 if expected_move_bps >= 0 else -1.0                # 807
diversity_score, category_mass = self._diversity_and_breakdown(         # 808
    initial_votes, pool_w, consensus_sign,                              # 809
)                                                                       # 810
diversity_mult = 0.6 + 0.4 * diversity_score                            # 811
final_conf = max(0.0, min(1.0, base_conf * diversity_mult))             # 812
```

Feeder values (traced below in pair checks):

| Name | Source | Units |
|:--|:--|:--|
| `step_bps_list` | `chess.py:776-780` (delta of `steps_bps`) | per-step Δbps |
| `step_aggs` | `chess.py:786-789` (`aggregate_votes` on `decayed_by_step[i]`) | `Aggregate` records |
| `kernel` | `chess.py:791` (`self.confidence_decay ** i`) | dimensionless |
| `initial_votes` | passed into `_diversity_and_breakdown` — the **undecayed** raw tribe votes as collected at the top of `project()` | `Vote` records |
| `pool_w` | passed in — tribe-level pool weight (post-TA-cap, pre-harvest-boost) | dimensionless |

## 2. Pair checks

### P1 — Source of `signal_quality` uncertainty (chess.py:794-796, 786-789, 523, 562-570, aggregator/voting.py:39-66)

**Check.** Does `step_aggs[i].uncertainty` reflect the pre-decay
vote coherence of the whole council, or the post-decay coherence
of the surviving council?

**Trace.**

1. `decayed_by_step: list[list] = [[] for _ in range(n_steps)]`
   (chess.py:523) — one bucket per horizon step.
2. Inside the per-tribe × per-step walk (chess.py:562-570), for
   each step `step_i` the tribe appends a copy of its initial vote
   with conviction, buy_frac, sell_frac multiplied by `decay =
   math.exp(-elapsed_s / hs)` where `hs` is that tribe's horizon
   length. The tribe's `weight` field is **not** modified.
3. `step_aggs[i] = aggregate_votes(decayed_by_step[i], …)`
   (chess.py:786-789). The aggregator reads each vote's `weight`
   (undecayed), `buy_frac`, `sell_frac` (both decayed).
4. `aggregate_votes` (voting.py:39-66) computes
   `buy_mass = Σ weight × buy_frac`,
   `sell_mass = Σ weight × sell_frac`,
   `active_mass = buy_mass + sell_mass`,
   `agreement = |BM - SM| / active_mass`,
   `uncertainty = 1 - agreement`.

**Analytical consequence.** For a single tribe, `buy_frac` and
`sell_frac` both scale by the same per-tribe decay factor, so the
tribe's internal agreement is decay-invariant. Across tribes, decay
factors differ — scalpers' buy/sell masses shrink faster with step
than swing traders'. `agreement` at step `i` therefore measures the
coherence of the **surviving (non-decayed) council** at step `i`,
weighted implicitly by each tribe's surviving strength.

**Intent.** The chess.py:427-430 comment documents this as
intentional:
> "decayed_by_step still receives a per-step horizon-decayed copy
> so the confidence aggregate keeps its meaning (scalpers fade,
> swing traders persist)."

**Is it a bug?** No. But it is behaviour worth flagging for
Phase 1:

- At long horizons, `signal_quality` is NOT the same quantity as
  at short horizons. It's measuring a different effective council
  (the subset whose horizons still have presence at step `i`).
- Two non-null sub-regimes are possible:
  (a) If swing traders broadly agree at long horizons,
  `signal_quality` structurally **rises** with horizon (scalpers
  fade out, swing traders dominate and agree). Good.
  (b) If only scalpers have non-abstain conviction on this tick,
  at long horizons `buy_mass + sell_mass → 0`, and
  `uncertainty = 1 - |0|/ε → 1`. `signal_quality` structurally
  **falls** with horizon. Bad.
- Which sub-regime dominates on the live tape is an empirical
  question. Phase 1 horizon stratification (`WS3_H1_SCOPE.md` §3.2)
  is the test.

**Verdict.** **Clean-by-design.** No fix. Flag for Phase 1: if
post-instrumentation measurement shows `signal_quality` diverging
sharply by horizon (e.g. 15s mean 0.75 vs. 10m mean 0.25),
revisit this design decision under a separate scope.

---

### P2 — `directional` penalises zigzag paths (chess.py:793, 797, 798)

**Check.** `directional = |expected_move_bps| / abs_activity`.
Does this ratio structurally penalise convergent paths that
zigzag internally?

**Trace.**

- `expected_move_bps = sum(b × w for b, w in zip(step_bps_list, kernel))`
  — kernel-weighted sum of per-step deltas. A signed quantity.
- `abs_activity = sum(|b| × w for b, w in zip(step_bps_list, kernel))`
  — kernel-weighted sum of per-step |deltas|. Always ≥ 0.
- By the triangle inequality, `|Σ b·w| ≤ Σ |b·w|` ⇒
  `directional ∈ [0, 1]`, with equality only when all `b` share
  sign (monotonic path in signed units, relative to the chosen
  kernel weighting).

**Analytical consequence.** A path like `[+10, -8, +10, -8, +10]`
with kernel `[1, 1, 1, 1, 1]` has `expected_move = +14`,
`abs_activity = 46`, `directional = 0.30`. A monotonic path
`[+3, +3, +3, +3, +2]` has `expected_move = +14`,
`abs_activity = 14`, `directional = 1.00`. Same terminal, very
different `directional`.

**Intent.** The ratio is the classical **path-efficiency**
quantity — "what fraction of the weighted path length was net
displacement." This is a coherent, defensible statistical object:
a zigzag path IS less predictable than a monotonic one with the
same terminal, and `directional` correctly assigns lower confidence
to the zigzag case.

**Is it a bug?** No — the ratio does what the name suggests. But
its **load-bearing-ness for H1** is an empirical question:

- If real BTC price paths between tick-zero and the 20s/1m/5m/10m
  horizons routinely reverse once or twice within the window
  (plausible — minute-level chop exists), `directional` sits
  structurally sub-0.5 even for correctly-signed forecasts.
- This is the direct target of Option B
  (`directional = |emove| / max(|step_i · kernel_i|)`, scope §4.2) —
  it drops the path-length denominator in favour of a max-single-step
  normalizer, which does not penalise zigzag.

**Verdict.** **Clean-by-design.** No fix. Option B is specifically
positioned to address this case if Phase 1 identifies `directional`
as the binding factor.

---

### P3 — `diversity_mult` floor-multiplier form (chess.py:811)

**Check.** `diversity_mult = 0.6 + 0.4 × diversity_score`. Is
this a *floor applied only when diverse*, or a *multiplier applied
to every tick* (and if the latter, how severely does it compound
with sub-1 `base_conf`)?

**Trace.** Line 811 is unconditional. `diversity_mult ∈ [0.6, 1.0]`.
`final_conf = base_conf × diversity_mult` is line 812, also
unconditional. Every tick takes a 0-40 % haircut proportional to
`(1 - diversity_score)`.

**Analytical consequence.** For a tick with `base_conf = 0.20`
(typical per WS1):
- `diversity_score = 0` (monolithic bloc) → `final_conf = 0.12`.
- `diversity_score = 1` (fully diverse) → `final_conf = 0.20`.
- Range: 0.12 - 0.20 depending only on diversity.

The **scope doc's P3 framing** ("every step's bloc status
compounds 0.6×") was incorrect — `diversity_mult` is computed
**once per tick**, not per step. No per-step compounding. Single
multiplier.

**Intent.** chess.py:801-806 comment documents this:
> "Floor at 0.6× so a true single-bloc call still fires when the
> signal itself is clear."

The design choice: prefer a *guaranteed participation with
attenuation* over a *binary cutoff when diverse*. Defensible for
keeping single-bloc high-confidence calls in play.

**Is it a bug?** No. The *magnitude* of the 40 % haircut is large
relative to typical `base_conf ~ 0.20`, but that's an intervention
question (Option C reshapes this to remove the floor haircut)
not an audit question.

**Verdict.** **Clean** (scope doc's compounding claim retracted).
Magnitude question defers to Phase 1 + Option C if `diversity_score`
mean sits low in the live-tape distribution.

---

### P4 — Sign-conditionality of `diversity_score` (chess.py:807-810, 984-1012)

**Check.** `_diversity_and_breakdown` receives `consensus_sign`
and computes entropy over the dominant-side mass only. Does this
bias against sign-divided councils, and if so, does the bias
double-count with `base_conf`'s sign-division penalty?

**Trace.** `_diversity_and_breakdown` (chess.py:984-1012):

```python
key = "buy" if consensus_sign >= 0 else "sell"                  # 1003
active_shares = [m[key] for m in category_mass.values() if m[key] > 0]  # 1004
# … Shannon entropy over active_shares, normalized by log(|active|)
```

So diversity is computed **only** over the dominant-side mass.
A category whose mass is entirely on the minority side is excluded
from the entropy calculation (its `m[key] = 0` → filtered by the
`> 0` clause).

**Analytical consequence.** Compare two councils:

- Sign-aligned: 4 categories all net-buy. `active_shares` has 4
  entries; diversity ≈ close to `log(4)/log(4) = 1.0` if masses
  are balanced. High `diversity_mult`.
- Sign-divided: 3 categories net-buy, 3 net-sell, net-buy consensus
  (weakly). `active_shares` has 3 entries (from the buy side).
  Diversity ≈ `log(3)/log(3) = 1.0` if buy-side masses are
  balanced. Also high `diversity_mult`.

Both cases get `diversity_mult ≈ 1.0`. But their `base_conf`
differ: sign-divided has low `base_conf` (high `abs_activity` from
both-side deltas, low `|expected_move|` from cancellation). So
sign-division is already penalised at `base_conf`, and the
sign-conditional diversity definition does not add a second
penalty. No double-count.

**Is there a hidden issue?** The sign-conditional definition
**does not treat sign-divided councils as "more diverse"** — it
treats them as "the dominant side is less diverse (fewer categories
contributing to the net direction)." This is a reasonable
definition: "diversity of the actual directional argument being
made." It's **not** the same as "diversity of opinion across the
council"; that would be a different metric (e.g. a Shannon entropy
over buy/sell/abstain fractions across all categories).

**Verdict.** **Clean.** The sign-conditional definition is
deliberate and coherent. No double-count with `base_conf`'s
sign-division penalty. Whether the definition is the *right* one
for the sizing decision is an H3-style aggregator question, not
an H1 audit question.

---

### P5 — Kernel reuse across numerator and denominator (chess.py:791, 793, 794-796, 797)

**Check.** The `kernel` list is built once (line 791). Is the
**same** list reused consistently in (a) `expected_move_bps`
numerator, (b) `signal_quality` sum, (c) `abs_activity`
denominator?

**Trace.** Literal inspection of lines 793, 794-796, 797:

- Line 793: `sum(b * w for b, w in zip(step_bps_list, kernel))` — uses `kernel`.
- Lines 794-796: `sum((1.0 - s.uncertainty) * w for s, w in zip(step_aggs, kernel)) / k_sum` — uses `kernel`, normalized by `k_sum = sum(kernel)`.
- Line 797: `sum(abs(b) * w for b, w in zip(step_bps_list, kernel))` — uses `kernel`.

All three zip `kernel` in index-aligned order with `step_bps_list`
or `step_aggs` (both of length `n_steps`). No slicing, no
reversal, no re-indexing. `directional = abs(emove) / abs_activity`
has the same `kernel` entry weighting step `i` on both sides of
the ratio.

**Analytical consequence.** `directional` is kernel-invariant with
respect to a global rescale of `kernel` (scaling all `w_i` by a
constant `c` cancels between numerator and denominator). It is
**not** kernel-invariant with respect to the *shape* of the
kernel (geometric decay with `confidence_decay`) — a steeper decay
downweights long-horizon steps on both sides of the ratio
equally, but the resulting `directional` still reflects the
short-horizon path-efficiency more heavily.

**Is it a bug?** No. Perfect symmetry across numerator and
denominator. `signal_quality` uses the same kernel but divides by
`k_sum` separately, so it's a normalized weighted average —
also structurally clean.

**Verdict.** **Clean.** Kernel reuse is consistent; no asymmetric
weighting that could skew `directional`.

---

### P6 — `step_bps_list` units: cumulative vs deltas (chess.py:760-762, 776-780, 793, 797)

**Check.** **Scope doc H1 §2 P6 pre-audit hypothesis:** `step_bps_list`
is the cumulative curve (inherited from `steps_bps`), which would
make `abs_activity = Σ|cumulative_i|·kernel` a monotonically-growing
denominator with no textbook interpretation — a bug-level concern.

**Trace.**

1. `steps_bps` is built at chess.py:742-762 by per-step consensus
   aggregation over `curve[h]` values. Each tribe's `curve` is the
   cumulative-bps path. So `steps_bps[h]` is the weighted-average
   **cumulative** consensus bps at step `h`. Matches the H2 audit
   trace (row 7 of `WS3_H2_path_audit.md` §1).
2. **chess.py:776-780:**
   ```python
   step_bps_list: list[float] = []
   prev_cum = 0.0
   for cum in steps_bps:
       step_bps_list.append(cum - prev_cum)
       prev_cum = cum
   ```
   This explicitly converts cumulative `steps_bps` into per-step
   **deltas** in `step_bps_list`. Starting from `prev_cum = 0.0`,
   so `step_bps_list[0] = steps_bps[0]` (move over first interval),
   `step_bps_list[1] = steps_bps[1] - steps_bps[0]`, etc.
3. Line 793 uses `step_bps_list` (deltas) for `expected_move_bps`.
4. Line 797 uses `step_bps_list` (deltas) for `abs_activity`.

**Analytical consequence.** With `step_bps_list` being per-step
deltas Δ_i and kernel w_i = d^i:

- `expected_move_bps = Σ Δ_i · w_i` = kernel-weighted sum of path
  deltas. With a decaying kernel (d < 1), near-horizon deltas are
  weighted more heavily. Signed quantity.
- `abs_activity = Σ |Δ_i| · w_i` = kernel-weighted **path length**
  (L1 norm of the kernel-weighted delta vector).
- `directional = |kernel-weighted displacement| / kernel-weighted
  path length` = **classical path-efficiency**, bounded in [0, 1],
  with a clean geometric meaning.

**Scope doc's P6 hypothesis is retracted.** The cumulative-branch
concern does not apply — lines 776-780 ensure the delta branch is
always active. The scope doc's P6 writeup was based on an incomplete
read of the H2 audit trace (which correctly identified `steps_bps`
as cumulative, but did not carry the delta conversion at 776-780
into H1 scope).

**Verdict.** **Clean.** No bug. The formula is the classical
path-efficiency formulation on the delta vector, with a decaying
kernel that biases toward near-horizon deltas. This is the most
important finding of this audit: P6 arrived as a bug-level
concern and collapsed on a direct code read.

---

## 3. Summary table

| Check | Target | Verdict | Phase-1 implication |
|:--|:--|:--|:--|
| P1 | `signal_quality` uncertainty source | Clean-by-design | Horizon stratification will surface whether decay-aware uncertainty creates a monotonic pattern. |
| P2 | `directional` zigzag penalty | Clean-by-design | If Phase 1 identifies `directional_score` as the binding factor, Option B directly addresses this. |
| P3 | `diversity_mult` floor-multiplier shape | Clean | Magnitude question defers to Option C if `diversity_score` mean is low. |
| P4 | Sign-conditional diversity | Clean | No action. |
| P5 | Kernel reuse consistency | Clean | No action. |
| P6 | `step_bps_list` units (delta vs cumulative) | **Clean (hypothesis retracted)** | Delta branch active; directional is path-efficiency. Scope doc §2 P6 writeup supersedes. |

## 4. Clean-by-design footnote — why not "SUSPECTED"?

The H2 audit landed at **SUSPECTED BUG** because it found two
candidates whose intent was unclear and whose observed behaviour
could not be explained by the stated contract. H1's audit is
materially different:

- P1's decay-aware uncertainty is documented at chess.py:427-430
  as intentional; the design rationale ("scalpers fade, swing
  traders persist") matches the code.
- P2's path-efficiency penalty is the standard interpretation of
  the ratio; no alternative contract is implied by the code or
  comments.
- P3, P4, P5, P6 all passed cleanly on inspection.

"Clean-by-design" here means: **the code implements its documented
intent, and Phase 1 measurement will reveal whether the intent's
consequences are what H1 needs to address.** Neither P1 nor P2
warrants a pre-Phase-1 fix commit; both are in scope for H1
intervention options (B for P2 directly, A or C for P3's magnitude,
nothing pre-defined for P1's horizon-dependence but flagged for
Phase 1 stratification).

## 5. Consequences for `WS3_H1_SCOPE.md`

This audit modifies the scope doc in one respect:

- **§2 P6 retraction.** The P6 pair-check writeup in the scope
  doc describes a "cumulative branch active" bug-level concern
  that was incorrect. The delta conversion at chess.py:776-780
  makes the delta branch active unconditionally. This audit's §2
  P6 is authoritative; the scope doc's §2 P6 is superseded. No
  scope-doc edit needed — this audit document is the citation
  target going forward.

All other scope-doc content (§3 instrumentation, §4 options, §5
acceptance, §11 parallel-workstream policy) stands unchanged.

## 6. Next actions

Per `WS3_H1_SCOPE.md` §7 execution sequence:

1. **H1.0 audit:** complete (this document).
2. **H1.I1 (auditor instrumentation):** next concrete action.
   Extend `PendingAnchor` / `Resolved` with `signal_quality`,
   `directional_score`, `base_conf`, `diversity_score`,
   `diversity_mult`. Plumb through `record_tick` and `_persist`.
   Determinism gate between.
3. **Forward accumulation:** 3+ production days with the
   instrumented auditor before H1.I2 Phase 1 measurement has
   signal. Parallel workstreams permitted per scope §11.

No pre-H1 fix commit. Audit-before-tune standing rule (`WS3_SCOPE.md`
§9) discharged with a clean verdict.

---

*Audited 2026-04-24 on `feat/tensor-engine`, tip `6d9875a`.
Pair-check citations verified against on-disk source at audit time.*
