# Phase 10-Harden - Merciless Shock Sweep

Day **2026-04-20**  |  seeds **42, 123, 777, 2024**  |  ticks/seed **500**  |  synth rate **60**

## Executive summary

- **Runs:** 4 seeded pairs (Gentle + Merciless each, same tape, same seed)
- **Reality tax (mean):** -0.450 %  stdev 0.055 pp  range [-0.508 %, -0.377 %]
- **Reality tax (median):** -0.456 %
- **Slippage inflation (mean):** +2.20 bp  stdev 0.02 bp
- **Win-rate delta (mean):** -7.01 pp  stdev 5.43 pp
- **Winners flipped to losers across sweep:** 26
- **Pareto-tail draws across sweep:** 8  (deferred fills: 8)
- **Sweep wall-time:** 334.3 s

## Per-seed breakdown

| Seed | Gentle eq ($) | Merciless eq ($) | Tax ($) | Tax (%) | Slip bump (bp) | Win-rate delta | Pareto / Deferred |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 42 | 49,393.12 | 49,138.96 | -254.16 | -0.508 | +2.21 | -7.84 pp | 0 / 0 |
| 123 | 49,436.38 | 49,247.70 | -188.68 | -0.377 | +2.22 | -5.19 pp | 5 / 5 |
| 777 | 49,345.64 | 49,112.29 | -233.35 | -0.467 | +2.21 | -13.98 pp | 0 / 0 |
| 2024 | 49,396.65 | 49,173.59 | -223.05 | -0.446 | +2.17 | -1.03 pp | 3 / 3 |

## Trade-level integrity check

Gentle and Merciless must produce the *same* council decisions on the same seed (the decision layer is isolated from the executor). A per-seed mismatch in trade count would be a bug in the harden wiring.

| Seed | Gentle trades | Merciless trades | Match? |
|---:|---:|---:|:---:|
| 42 | 102 | 102 | OK |
| 123 | 77 | 77 | OK |
| 777 | 93 | 93 | OK |
| 2024 | 97 | 97 | OK |

## Verdict

**CONSTANT-LIKE.** Reality tax is tight across seeds (stdev < 0.15 pp, range < 0.5 pp). The -0.51 % single-seed number appears to be a structural friction constant, not a lucky draw.

_Stability Gate reminder: 7 An-0 days under `harden.enabled: true`, Sharpe > 2.0, zero guard alerts._
