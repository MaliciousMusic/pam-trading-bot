# Depth-Blind Live Ledgers (pre-`0b6e7ad`)

**Status: SUSPECT. Do not use for microstructure analysis.**

## What happened

From the moment `@depth20@100ms` was added to `config/ingress.streams`
until commit `0b6e7ad` landed on 2026-04-21 at 13:18 +0200, every
partial-depth frame delivered by Binance was silently dropped by the
normalizer in `src/ingress/binance_ws.py`.

Root cause: the normalizer's routing check was

```python
if "@depth" in stream_name and not data.get("e"):
```

That guard was correct for *spot* partial-depth frames (which have no
`e` field) but wrong for *futures* partial-depth frames, which carry
`"e": "depthUpdate"`. Since PAM runs on Binance Futures (BTCUSDT perp),
every depth frame matched `@depth20` in `stream_name` AND had `e` set,
so the `and not data.get("e")` half of the guard filtered them out.

Result: `MarketState.orderbook` never received an update from live.
`microstructure_prober` had no input to compute on, so it abstained on
every tick — producing the fake "0% activity" reading that looked
structural but was a subscription bug.

## What is in this folder

```
ledger/
  2026-04-18.jsonl   — 19.2 MB,  depth-blind
  2026-04-19.jsonl   —  5.5 MB,  depth-blind
  2026-04-20.jsonl   — 41.8 MB,  depth-blind
  2026-04-21.jsonl   — 19.5 MB,  depth-blind (fix landed later same day)
audit/
  2026-04-18.json    — daily audit summary, depth-blind
  2026-04-19.json    — daily audit summary, depth-blind
```

The `.jsonl` and `.json` payloads are gitignored — kept locally only,
because they are per-machine and large. This README is tracked so the
provenance is self-documenting for anyone who finds the folder later.

## What is still usable

- **Tribes that do not depend on orderbook state** are unaffected. The
  ledgers remain valid for analyzing those tribes (basis_arb, funding_fader,
  trend_follower, etc. — most of the 24).
- **Non-microstructure metrics** (vote-correlation, PCA orthogonality,
  tribe activity on TA/funding/options inputs) read correctly from these
  files. Only microstructure-conditional conclusions are tainted.

## What is NOT usable

- **Any conclusion that `microstructure_prober` is structurally mute,
  redundant, or low-activity.** It was data-starved, not design-flawed.
- **Any calibration baseline for `microstructure_prober`.** TribeCalibrator
  weights derived from these ledgers reflect a tribe that literally never
  voted — not a tribe that votes badly.
- **Any spread / depth-imbalance metric** read from `MarketState` on these
  days. The field defaults were carried forward untouched the whole run.

## The new "An 0"

The fresh `data/ledger/` and `data/audit/` directories (empty as of
2026-04-21) collect post-fix live output only. All new calibration,
correlation, and orthogonality analysis runs against that set.

For cross-epoch studies that intentionally span the fix boundary
(e.g. "how did the council change when microstructure came online"),
point the analyzers at both paths explicitly and annotate the chart.

## Fix reference

- Commit: `0b6e7ad` — `feat(phase-g1): yeux ouverts — fix @depth20 routing, relax options thresholds, add G3 proxy book`
- Branch: `feat/tensor-engine`
- Landed: 2026-04-21 13:18 +0200
