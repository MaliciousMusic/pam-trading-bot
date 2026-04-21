# Phase 10-Harden — Merciless Shock Report

Day **2026-04-20**  ·  seed **42**  ·  ticks **500**  ·  synth rate **60**

## Executive summary — Reality Tax

- **Equity delta (Merciless − Gentle):** $-254.16  (-0.508 % of starting equity)
- **Gentle end-equity:** $49,393.12  (-606.88 from start)
- **Merciless end-equity:** $49,138.96  (-861.04 from start)
- **Trade count:** Gentle 102 → Merciless 102  (Δ +0)
- **Win rate:** Gentle 27.5 % → Merciless 19.6 %

## Merciless-layer telemetry

- **Latency events logged:** 0  (fills deferred >= 1 tick)
- **Deferred fills that landed:** 0
- **Entry slippage avg (Merciless):** 4.80 bp  (max 5.41 bp, n=102)
- **Entry slippage avg (Gentle baseline):** 2.59 bp  (max 2.64 bp, n=102)
- **Slippage inflation (Merciless - Gentle):** +2.21 bp  (+85.3 %)
- **Exit slippage avg:** Gentle 0.00 bp -> Merciless 0.00 bp
- **Capacity clips:** Gentle 0 -> Merciless 0  (rejects: 0 -> 0)

## Side-by-side

| Metric | Gentle | Merciless | Delta |
|---|---:|---:|---:|
| Ticks | 500 | 500 | 0 |
| Trades | 102 | 102 | 0 |
| Wins | 28 | 20 | -8 |
| Win rate (%) | 27.5 | 19.6 | -7.8 |
| Start equity ($) | 50,000.00 | 50,000.00 | 0.00 |
| End equity ($) | 49,393.12 | 49,138.96 | -254.16 |
| Equity delta ($) | -606.88 | -861.04 | -254.16 |
| Entry slip avg (bp) | 2.59 | 4.80 | 2.21 |
| Entry slip max (bp) | 2.64 | 5.41 | 2.77 |
| Wall (s) | 29.69 | 30.91 | 1.22 |

## Verdict

**MEANINGFUL TAX** — adversarial conditions subtract real value but the strategy still books P&L.

_Next steps: if the tax is > 1 %, Phase 10-Harden work_
_continues until the council is robust under Merciless._
_Target: 7 An-0 days with Merciless=on, Sharpe > 2.0,_
_zero guard alerts._
