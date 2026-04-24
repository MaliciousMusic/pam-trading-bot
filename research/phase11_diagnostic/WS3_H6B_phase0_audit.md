# WS3 H6.B — scale × DNA-output amplification audit — Phase 0

**Phase 11 Diagnostic · Phase 0 audit (static, no code)**

| Item | Value |
|:--|:--|
| Box | 00:50 → 02:00 CET (70 min strict), hard abort 02:10 |
| Opened | 2026-04-25 00:50 CET |
| Verdict | **BUG-FOUND** — door #2 confirmed; DNA output × depth-scale saturates H5.A ceiling on thin books under routine conditions |
| Files audited | `src/projector/chess.py` (recursion + H5.A clamp + consensus), `src/projector/behavioral_dna.py` (post-clamp DNA output bounds), `src/projector/event_wave.py` (event_bps bound), prior audits H5/H6 |
| Code changed | **NONE** — audit-only per `feedback_audit_fix_checkin.md` |
| Scope acceptance | Map door #2 (scale × DNA) with worst-case analysis; identify fix options with honest quartile; §8 scan for candidate door #3 |

## 0. Headline

H6.A closed the **recursion feedback channel** (DNA input clamp at ±100 bps
bounds `prev_delta_bps` / `prev_state_bps` entering `DNAContext`). Live
validation at 00:47 showed this works as designed — clamp fires
2005–5416 times per projection, byte-deterministic, all tests green.

**But steady-state saturation ratio stayed at 100%.** The clamp bounds
the **recursive input** but not the **per-step output**: `step_bps =
(dna_delta + event_bps) · scale` where `scale = depth_ref_usd /
max(side_depth, depth_floor_usd) ∈ [1, 20]`. With clamped DNA input,
`ta_dna`'s output is bounded at ≈ 65 bps/step, but ta_dna × scale=20
yields 1305 bps — 2.6× the H5.A saturation ceiling of 500 bps. On BTC
perps with 5bps-side depth under $1.3M (routine during chop), `scale >
7.7` forces `step_bps > 500` regardless of the recursion clamp.

**Door #2 is a single-step amplification, not a chain recurrence.**
Unlike H6.A which required 2nd-order Jury/Schur analysis, here the
instability is trivial: a bounded input times an unbounded multiplier
gives an unbounded output. The fix must cap either the multiplier
(scale) or tighten the input clamp so the product stays below 500.

## 1. Q-answers (per extended scope §0)

### Q1 — What is the amplification path in a single step?

```
DNA input (clamped to ±100)
  → DNA output (bounded per-category; ta_dna ≤ 65 bps, retail-panic ≤ 404 bps)
    + event_bps (unbounded per event_wave.py, but typically << 100 bps)
      × scale ∈ [1, 20]                    ← DOOR #2 AMPLIFIER
        → per-tribe step_bps
          → consensus aggregation (weighted mean, ≤ max contributor)
            → step_bps_list[h] ≥ 500 ? → H5.A saturation_hit = True → conf=0
```

Scale is the only unbounded multiplier in the chain. Aggregation
cannot amplify past its max contributor. Event_bps is bounded per
schedule. DNA output is bounded by the H6.A clamp.

### Q2 — For which DNA category does scale × output breach 500 bps?

At `base_strength = 1.0`, DNA input = ±clamp=100 bps:

| Category | DNA output max | breach scale | Routine? |
|:--|--:|--:|:--|
| arb | 0.30 + 0.55·shock_sum + 0.18·100 = 18.3 + shock | > 27 | **NO** (scale cap ≈ 20) |
| microstructure | 0.20 + 0.70·shock + 0.10·100 + 0.25·100 = 35.2 + shock | > 14.2 | marginal |
| mm | 0.10 + 1.20·liq_shock + 0.35·micro + 0.40·100 = 40.1 + shock | > 12.5 | thin books |
| **ta** | **0.25 + 0.65·100 + 0.05·100 + 0.20·shock = 70.25 + shock** | **> 7.1** | **YES** (chop, trend) |
| event | 0.20 + 1.10·shock + 0.08·100 = 8.2 + 1.10·shock | > 60 (shock bound) | shock-dependent |
| hedger | 0.10 + 0.30·100 + 0.45·macro = 30.1 + shock | > 16.6 | marginal |
| **retail (panic)** | **0.15 + 4·100 + 0.04·100·100/100 + 1.5·shock = 404.15 + 1.5·shock** | **> 1.24** | **YES (any panic)** |
| retail (calm) | 0.15 + 0.40·100 = 40.15 | > 12.5 | marginal |
| mev | 0.35 + 1.45·shock + 0.55·100 = 55.35 + shock | > 9.0 | thin + shock |
| whale | 0.55 + 0.15·macro + 0.02·100 = 2.55 + shock | > 196 | never |

**Two categories breach 500 bps at routine scale** (≤ 10):
`ta_dna` at scale > 7.1 (thin books on **any** regime including chop —
matches live observation: regime=chop 100% in the 5-min post-H6.A window)
and `retail_dna` panic at scale > 1.24 (but dormant in chop —
no shock + |state| ≤ clamp=100).

`ta` is the **chop-regime driver** of door #2. Retail panic is a
separate edge (cascade regimes), not the cause of the current 100%
saturation.

### Q3 — Scale distribution on live tape

Depth parameters: `depth_ref_usd = 10_000_000`, `depth_floor_usd =
500_000` (chess.py:169-170). Formula: `scale = depth_ref /
max(side_depth, depth_floor)`.

| BTC-perp 5bps-side depth | scale | ta_dna × scale (max) |
|--:|--:|--:|
| $10M (deep) | 1.0 | 70 bps |
| $5M | 2.0 | 140 bps |
| $3M | 3.3 | 234 bps |
| $2M | 5.0 | 350 bps |
| $1.4M (routine chop) | 7.1 | **500 bps ← breach begins** |
| $1M | 10.0 | 700 bps |
| $500K (floor) | 20.0 | **1,400 bps (2.8× ceiling)** |

BTC perps intra-day 5bps-side depth is typically $2-6M in normal hours,
dropping to $0.5-2M during chop or funding-rate flips. Live log shows
`emove` median 1184 bps / max 10074 bps across 339 projections —
consistent with scale in the 5-10 range dominating the tape.

### Q4 — Is event_bps a 3rd unbounded input to this amplifier?

`event_wave.py:66-80` computes `event_bps = side · impact_bps · Σᵢ
wᵢ · dmassᵢ`. Per-event `impact_bps` comes from the classified event
(can be 100-500 bps for "etf_approval"-class events). `Σ wᵢ · dmassᵢ`
is bounded to [0, 1] (CDF increment summed over tribe weights
normalized to 1).

So `event_bps_single_event ≤ impact_bps`. With multiple active events,
accumulates. Largest observed in config review: 287 bps (ETF approval
class). A single big event can push `total_bps = dna + event = 70 +
287 = 357 bps` pre-scale, then × scale=5 = 1785 bps.

**Event_bps is a latent co-driver but not the current cause.** The
5-min observation window had `regime=chop` with no scheduled events
firing (live engine did not inject large-impact events during the
window). Current saturation is DNA×scale alone. Event is a door that
opens in the opposite regime (macro news, ETF prints, liquidations).

### Q5 — Does the H5.A clamp reposition the problem or fix it?

H5.A saturates `step_bps_list[h]` at ±500 bps AND forces `conf=0`.
The clamp prevents downstream consumers (sigma fitter, jsonl, decision
engine) from seeing pathological magnitudes. But it's a **kill-switch,
not a saturation curve**: every triggering tick → hold.

Observed: 100% steady-state saturation_hit → 100% holds → 0 trades in
the 5-min window. The bot is in a structural "incoherent council"
state driven by routine scale×DNA breach, not by pathological tape.

The right outcome is NOT to make H5.A a softer clamp (that discards
signal). The right outcome is to prevent `step_bps` from breaching
500 in the first place by bounding the product upstream.

## 2. Mathematical bound (single-step)

Let `C = DNA_CTX_CLAMP_BPS = 100`, `S = scale ∈ [1, 20]`, `M =
MAX_STEP_BPS = 500` (H5.A ceiling), and `D_cat(C, base_strength,
shock)` = DNA output for category at clamped input.

Single-step non-breach condition:

```
|step_bps| = |D_cat · S| ≤ M
⟺ |D_cat| ≤ M / S
```

With `S_max = 20` this requires `|D_cat| ≤ 25 bps`. Per-category
checks (base_strength = 1, shock = 0):

| Category | D_cat max | D_cat ≤ 25? |
|:--|--:|:--|
| arb | 18.3 | ✓ |
| microstructure | 35.2 | ✗ |
| mm | 40.1 | ✗ |
| **ta** | **70.25** | **✗** |
| event | 8.2 | ✓ |
| hedger | 30.1 | ✗ |
| **retail panic** | **404.15** | **✗✗✗** |
| retail calm | 40.15 | ✗ |
| mev | 55.35 | ✗ |
| whale | 2.55 | ✓ |

7 of 10 categories breach when scale is at the cap. Only 3 (arb,
event-sans-shock, whale) stay in budget.

No Jury/Schur needed — this is a linear inequality, not a stability
problem. The H6.A recurrence analysis showed `cum_bps` is bounded
post-clamp; H6.B shows `step_bps` is not.

## 3. Why H6.A clamp is insufficient on its own

**H6.A closes the recursion channel.** It prevents `cum_bps` from
growing unbounded geometrically through `prev_delta` feedback.

**It does NOT cap single-step output.** The clamp affects `DNAContext`
inputs only. The multiplication `(dna_delta + event_bps) × scale` at
chess.py:631 is the amplifier, and neither operand is constrained to
a budget that survives the multiplication.

Empirical verification from the 5-min live run (ticks 100-299, post-
warmup):

| Metric | Value |
|---:|--:|
| Saturation ratio (steady-state) | 100% |
| DNA clamp fires/projection (median) | 3,732 |
| Per-projection DNA evaluations (est.) | ~3,000–4,000 |
| Clamp activation rate per DNA call | ~100% |
| emove magnitude (median) | 1,184 bps |
| Trades executed | 0 |

Clamp is firing almost every DNA call yet emove still exceeds 500 bps
on ~100% of ticks. **Confirms door #2 is the dominant amplifier
post-H6.A.**

## 4. Root-cause ladder (H6.B extension)

| # | Layer | Status | Notes |
|:-:|:--|:--|:--|
| 1 | DNA coefficients (TA 0.65·prev_delta, retail panic 4.0·prev_delta) | 🟡 **bounded by H6.A** | Clamp caps input at 100; output bounded per §1 Q2 |
| 2 | Depth-scale multiplier (scale ∈ [1, 20]) | 🔴 **bug-found** | Amplifies bounded DNA output past H5.A ceiling |
| 3 | H5.A saturation ceiling (MAX_STEP_BPS = 500) | 🟡 **dimensioning mismatch** | Sized against ~25σ on BTC 5s-bar, but DNA×scale routinely breaches |
| 4 | event_bps (event_wave) | 🟡 **latent driver** | Not dominant in chop; door #3 candidate in news/cascade regimes |
| 5 | base_strength / effective_gain (calibrator output) | 🟢 WAI | default 8 bps/unit_pressure, regime_mult 1.0 → bounded intent_drift |
| 6 | Consensus aggregation (pool weights × tribe curves) | 🟢 WAI | Weighted mean, cannot exceed max contributor |

**Primary bug row: 2.** Secondary: 3 (ceiling dimensioning). Fix
should target row 2; row 3 could be revisited if tuning scale breaks
legitimate trending signal.

## 5. Numerical verification

**Scenario:** regime=chop, single TA tribe, base_strength=1.0, `cum_bps`
saturates clamp at 100 bps within 3 steps (per H6.A test
`_run_ta_chain(scale=1.0, clamp=True)` — chain reaches 100 bps
attractor fast).

At the clamped attractor (`prev_state_bps = prev_delta_bps = 100`):

```
ta_dna_output = 0.25·1.0 + 0.65·100 + (-0.05·100 since |state|>200 false, actually |state|=100 so fatigue=0)
             + 0.20·shock_sum
             = 0.25 + 65 + 0 + 0  (chop, shocks rare)
             = 65.25 bps
```

Per scale:

| scale | step_bps | saturates (>500)? |
|:--|--:|:--|
| 1 | 65.25 | no |
| 3 | 195.75 | no |
| 5 | 326.25 | no |
| 7.1 | **463.28** | **threshold** |
| 8 | 522 | **YES** |
| 10 | 652.5 | **YES** |
| 20 | 1305 | **YES** (2.6× ceiling) |

Matches the live observation. For `scale ≥ 8`, ta_dna alone (no shock,
no event, no retail panic) breaches 500 bps.

BTC-perp 5bps-side depth < $1.25M → scale > 8. Historical tape shows
this is routine in Asian hours, weekends, and around funding flips.

## 6. Fix options

### Option A — tighten `ta_dna` momentum coefficient (inside `behavioral_dna.py`)

Drop `momentum_amp = 0.65 · prev_delta_bps` to `0.25 · prev_delta_bps`
so ta_dna max = 0.25 + 0.25·100 + 0.20·shock = 25.25 + shock. Budget
fits scale=20 (25·20 = 500, equal). Retail panic: drop `chase = 4.0
· prev_delta` to `0.25 · prev_delta`, drop `overshoot` to 0.

- **LOC:** ~4 lines in behavioral_dna.py
- **Pros:** single file, fits scale=20 budget exactly. No new
  parameter. No projector-core changes.
- **Cons:** **semantic change** — TA momentum amplification is a
  design-intent coefficient (fast-follower vs slow-follower behavior
  is encoded in this ratio). Dropping 0.65 → 0.25 makes TA indistinguishable
  from microstructure's 0.10 prev_delta coupling. Retail panic
  becomes a rounding error. Needs design review.
- **Worst case quartile:** MEDIUM — invasive at the DNA layer; needs
  determinism gate + design justification + regression on simulated
  trending regimes (not just chop).

### Option B — cap `scale` at DEPTH_SCALE_MAX (inside `chess.py`)

Add `scale = min(scale, DEPTH_SCALE_MAX)` at chess.py:628-630.
`DEPTH_SCALE_MAX = 7` keeps ta_dna max 70.25·7 = 491.75 ≤ 500;
`DEPTH_SCALE_MAX = 5` gives 351 with 30% headroom.

- **LOC:** ~3 lines in chess.py
- **Pros:** 1 file, 1 constant, physically justifiable — depth > 20×
  amplification is a microstructure artifact, not a real economic
  signal (a 5-second move getting 20× weight because the book is
  1.25× thinner than normal is not "market says listen harder", it's
  "our divisor happened to land near the floor"). Preserves every
  DNA semantic. Preserves all H6.A containment.
- **Cons:** caps thin-book amplification in *legitimate* cascade
  regimes (cascade_up / cascade_down), where current design wants
  scale up to 20 to reflect that thin books really do pay more in
  liquidation territory. Needs regression on cascade tape
  (2026-04-17 cascade sample from Phase 0 baseline).
- **Worst case quartile:** LOW–MEDIUM — 1 file, 1 constant, no DNA
  touched, no recursion math touched. Determinism gate + regression
  on cascade tape. Cascade_up/down regression is the only real risk;
  chop/normal/trend regression is by construction safer (current
  tape is chop, capping helps it).

### Option C — tighten `DNA_CTX_CLAMP_BPS` from 100 → 25 (inside `chess.py`)

Drop the H6.A clamp constant from 100 → 25. Then ta_dna max = 0.25 +
0.65·25 = 16.5 bps, × scale=20 = 330 bps < 500.

- **LOC:** 1 line (constant change)
- **Pros:** 1 parameter change, no new code path, leverages the
  existing H6.A machinery.
- **Cons:** **25 bps is inside the legitimate BTC 5-sec move
  distribution** (2026-04 realized σ ≈ 30 bps/5s per H6.A commentary
  — 25 is below 1σ). Every legitimate 5-sec trending motion would
  clamp. The recursion already demonstrates a "clamped at 100"
  attractor; dropping to 25 makes the attractor the dominant
  steady-state for *every* DNA input, destroying all legitimate
  momentum signal below 25 bps.
- **Worst case quartile:** HIGH — breaks the H6.A design intent
  ("preserve legitimate momentum signal below 100 bps"). Not
  recommended standalone.

### Option D — coordinated pair: scale cap (B) + clamp relax OR coefficient tune (A-lite)

Two-lever fix:
- **D1:** Option B at `DEPTH_SCALE_MAX = 5` + keep H6.A clamp at 100.
  Gives 2-3× headroom on ta_dna, addresses retail panic with the
  existing clamp (already bounded at 404·5 = 2020 bps — still breaches).
- **D2:** Option B at `DEPTH_SCALE_MAX = 5` + Option A applied ONLY
  to `retail_dna` panic (drop chase 4.0 → 1.0; overshoot 0.04 →
  0.01). Keeps ta_dna untouched.

- **LOC:** 3–7 lines across 2 files
- **Pros:** D2 addresses both dominant breaching categories without
  semantically neutralizing TA (which is a design-legitimate
  amplifier).
- **Cons:** 2 files, needs both arguments defended to user, bigger
  regression surface.
- **Worst case quartile:** MEDIUM (D2) to HIGH (D1 — retail panic
  still breaches).

## 7. Recommendation

**Option B (scale cap) at `DEPTH_SCALE_MAX = 5`, with explicit
regression on cascade_up/cascade_down tape.**

### Justification

1. **Smallest blast radius.** 1 file, 1 constant, no DNA math
   changed, no recursion math changed. H6.A containment is preserved.

2. **Physical interpretation.** `scale > 5` corresponds to 5bps-side
   depth < $2M — a real market condition, but one where extrapolating
   "this 5-second move is 5× more informative per dollar than normal"
   has diminishing validity. Depth-scale 20× is saying a $500k-deep
   book makes every wiggle 20× as actionable. That's overfitting the
   multiplier to the microstructure.

3. **Closes door #2 cleanly.** At scale=5, every category in §2
   except retail panic fits the 500 bps budget (retail panic = 404·5
   = 2020 > 500). Retail panic is shock-triggered and dormant in
   chop. If door #2 in cascade regimes becomes the next problem,
   that's a known scope (H6.C) — not a surprise.

4. **Testable.** Determinism gate + regression on 2026-04-20 chop
   tape (expected ~0% clamp activations post-fix) + 2026-04-17
   cascade sample (verify cascade regime still produces tradeable
   signal, not clamped-flat).

5. **Reversible.** If regression shows cascade tape gets strangled,
   bump `DEPTH_SCALE_MAX` to 7 or 10 — single-constant tuning knob.

### Worst-case quartile: LOW–MEDIUM

- Code surface: 1 file (`chess.py`), ~3 LOC (constant + one line in
  depth-scale block).
- Determinism risk: ZERO (`min()` is pure, byte-deterministic —
  equivalent to H6.A's `max/min` clamp pattern already proven in
  det gate).
- Regression risk: cascade tape may produce weaker signal. Mitigated
  by running the regression upfront before user sign-off on H6.B fix.
- Semantic risk: changes the design intent from "depth amplifies up
  to 20×" to "depth amplifies up to 5× then saturates". Needs user
  acknowledgment.

### Estimated H6.B fix box (tomorrow)

| Phase | Budget |
|:--|--:|
| Implementation (add DEPTH_SCALE_MAX constant + cap line + tests) | 30 min |
| Determinism gate (--max-ticks=1000 on 2026-04-20) | 10 min |
| Regression chop tape (confirm saturation_hit drops to ~0%) | 15 min |
| Regression cascade tape (verify signal preserved, tradeable) | 20 min |
| Live 5-min restart + observation | 15 min |
| Commit + push + handoff update | 15 min |
| **Total** | **~1h50 budget, 2h strict, 2h15 hard abort** |

## 8. Other potential doors (scan, not audit-depth)

Per user extension: 5-10 min scan for candidate door #3. Up to 3
candidates, why likely non-dominant, what would promote them.

### Candidate #3a — `event_bps` (event_wave)

**Mechanism.** `total_bps = dna_delta + event_bps` before × scale.
Large-impact events (ETF approvals, FOMC macros) inject 100-300 bps
into `event_bps` via the log-normal latency wave. At scale=5 this
gives step_bps = (70 + 287) × 5 = 1785 bps.

**Why non-dominant now.** 5-min observation window had zero
large-impact events scheduled. Live tape with calm news flow keeps
event_bps close to 0. The fix for door #2 (scale cap) also mitigates
this — at scale=5 a 287-bps event gives 1435 bps peak, still above
500, but event waves are transient (bounded total `impact_bps` over
horizon) vs ta_dna's per-step ceiling which is sustained.

**Promotes to H6.C if.** Saturation_hit spikes on days with scheduled
macro events, even after H6.B lands. Instrumentation: log
`event_bps` at `project:621` and correlate with saturation ticks.
Fix shape: cap `event_bps` contribution per step (e.g. cap at 100
bps/step even if event says 287 bps, letting the wave spread the
remaining impact across future steps).

### Candidate #3b — `base_strength` amplification via calibrator

**Mechanism.** `base_strength_pre = v0.conviction · sk0 ·
effective_gain` where `effective_gain = bps_per_unit_pressure ·
regime_multiplier`. Default `bps_per_unit_pressure = 8.0`. If
`regime_multiplier` spikes (e.g. to 3.0 during classifier transitions)
and `sk0` (skill multiplier from calibrator) also spikes, then
`base_strength` can reach 24+. `intent_drift = 0.25 · 24 = 6 bps`
for ta_dna — small but feeds the recursion seed larger.

**Why non-dominant now.** Default `regime_multiplier = 1.0` and
calibrator `sk0` is bounded by design to [0.25, 4.0] window per H6
audit §4. Max `base_strength = 1 · 4 · 8 = 32`. `intent_drift ≤ 8
bps` — small vs the 100-bps clamp attractor from the recursion.
H6.A audit §4 traced the calibrator path and concluded "gain is
diagnostic, not causal" — same logic applies here.

**Promotes to H6.C if.** Regression reveals `regime_multiplier`
transients push base_strength into a regime where intent_drift alone
dominates post-H6.B. Instrumentation: log `effective_gain · v0.conviction
· sk0` per tribe per tick and check if any single tribe contributes
> 50% of emove. Fix shape: cap `effective_gain` at a ceiling (e.g.
20) in chess.py:315.

### Candidate #3c — consensus aggregation weight concentration

**Mechanism.** `Consensus_h = Σ (Path_{i,h} · W_{i,h} · pool_weight_i) /
Σ (W_{i,h} · pool_weight_i)`. If one tribe's `pool_weight` is very
large (post regime-multiplier + skill + capitulation boost) and its
`curve[h]` is near the per-tribe clamp attractor (+100 bps), its
contribution dominates the consensus. Upper bound: consensus ≤ max
tribe's step_bps, which is already bounded by the per-tribe analysis
above. So aggregation can't create a NEW amplification, only preserve
one that exists upstream.

**Why non-dominant now.** Weighted mean of bounded inputs is bounded.
The capitulation multiplier (chess.py:711) boosts one tribe by 3×,
but only on 2-of-2 loser post-flip conditions (cascade-specific).
In chop regime this never triggers. TA family cap (chess.py:716-...)
prevents TA cluster over-voting.

**Promotes to H6.C if.** Single-tribe dominance is observed post-H6.B
and the dominant tribe is breaching the H5.A ceiling. Instrumentation:
log the top-3 pool-weighted contributors per projection step.
Fix shape: cap any single tribe's contribution at some fraction of
consensus (e.g. no tribe contributes > 40% of step consensus).

---

**Scan conclusion:** none of #3a/#3b/#3c are dominant drivers in the
currently observed chop-regime 100%-saturation state. They are
**latent drivers that could surface** after H6.B lands on door #2. All
three have clear instrumentation paths and known fix shapes; none
requires reopening H6 Phase 0 scope.

## 9. Acceptance vs H6.B scope §0

> "Map door #2 (scale × DNA-output), derive worst-case amplification,
> propose fix with worst-case quartile; §8 scan up to 3 candidate
> door #3 with non-dominance argument."

**Satisfied.**

- §1-5: door #2 mapped, worst-case derived, empirical match to 5-min
  observation confirmed.
- §6-7: 4 fix options (A/B/C/D) with honest quartile; reco = Option B
  (scale cap at 5), justification, regression plan, box estimate.
- §8: 3 candidate doors (event_bps, base_strength, consensus
  concentration) with non-dominance arguments and promotion criteria.

## 10. Box accounting

- Box: 00:50 → 02:00 CET (70 min strict budget).
- Used: ~50 min (read → math → write).
- Remaining box: ~20 min for commit + handoff + check-in.
- Hard abort: 02:10 CET.

---

**Next action:** commit this audit, update handoff with H6.B fix
decision cadrée for tomorrow, final check-in per
`feedback_audit_fix_checkin.md`.

**No fix tonight. No push tonight.** Just audit + handoff.
