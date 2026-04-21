# Stability Gate (G2) — Target Pass Criteria

> **Status:** OPEN — pending first run under Merciless.
> **Opened:** 2026-04-21 (post Phase 10-Harden freeze, commit pending).
> **Blocks:** any live-capital discussion.

---

## Gate target

**7 consecutive An-0 days under `harden.enabled: true`** with the following invariants holding across the entire window:

| Criterion | Threshold | Source of truth |
|---|---|---|
| Merciless enabled | `harden.enabled == true` on every tick of every session | `config/settings.yaml`, run_metadata |
| Sharpe ratio (7-day, daily-returns basis) | **> 2.0** | `scripts/compute_sharpe.py` (TODO) over session equity curves |
| Guard alerts | **zero** — no peak-DD halt, no daily-loss halt, no session-DD halt, no black-swan detonation | `logs/*.log` grep `GUARD_HALT` / `BLACK_SWAN` |
| Determinism | each day's replay reproduces its live SHA exactly | `scripts/verify_determinism.py` |
| Reality-tax drift | daily Merciless-vs-Gentle tax stays within the frozen envelope: mean `−0.45 % ± 3σ` where σ=0.055 pp (stdev from the 4-seed sweep) | `scripts/bench_harden_shock.py` nightly |

"An-0" = Anno Zero — the post-depth-fix era starting at commit `0b6e7ad`. All pre-`0b6e7ad` ledgers are archived and **do not count**. See `archive/` for the depth-blind epoch.

---

## How a day counts

A day counts toward the 7 only if:

1. The live session ran `config/settings.yaml` with `harden.enabled: true` from open to close.
2. No manual restart / operator override fired mid-session.
3. The nightly determinism gate passes on that day's event tape.
4. The daily shock-bench (`scripts/bench_harden_shock.py` on that day's synthetic replay) produces a tax value inside the envelope.
5. Zero guard-halt events were emitted.

Any failure resets the counter to **zero**. No partial credit.

---

## What a pass unlocks

A clean 7-day window opens the next architecture conversation:

- **Budget discussion** — first free-data-only constraint review since the project start. See `project_budget_constraint.md`.
- **Live-capital bridge** — design of the paper→live handoff (size ramp, kill-switch cadence, live position cap).
- **Phase 12 unlock** — microstructure L2 tribe can queue for development.

None of those conversations happen before the gate passes.

---

## What a fail teaches

If the gate fails (most likely via Sharpe < 2 or a guard halt), the failure mode is itself the signal:

- **Sharpe < 2 but no halts** → the council's edge is real but thin under live frictions. Investigate entry selectivity (tighter `min_conf`, stricter regime gating) before adding new tribes.
- **Peak-DD halt** → position sizing curve is too aggressive for the tail distribution. Revisit `sizing.max_pct_equity` and the `sigma_shrink_floor`.
- **Black-swan detonation** → either a legitimate tail event (good — the guard worked) or a calibration drift in `black_swan_min_ref_sigma_bps`. Diff σ-baseline pre- and post-cooldown.
- **Reality-tax drift outside envelope** → the Merciless overlay is interacting with a structural change in the tape (new regime, new volatility baseline). Re-run the sweep and re-freeze the envelope.

Each fail is a structured signal, not a setback. The gate is an instrument, not a verdict.

---

## Open sub-tasks

- [ ] `scripts/compute_sharpe.py` — 7-day rolling Sharpe over session equity curves (not yet written).
- [ ] Nightly cron wiring — `bench_harden_shock` over the prior day's tape, envelope check, alert on breach.
- [ ] Live session log schema stamp — every session header must record `harden.enabled` so gate auditing is fast.

These are the minimum instrumentation tasks before G2 can be machine-checked. Until then, the gate runs on manual audit.
