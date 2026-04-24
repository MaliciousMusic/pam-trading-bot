# Pathological baseline — pre-H5.A snapshot

**Captured:** 2026-04-24 22:47 CET (before killing PID 16096)
**Purpose:** Freeze the tape + calibration state that exhibits the P4 attractor (`abs_activity_bps ≥ 1e9`, `directional_score = 1/99`) for post-H5.A comparison.

## Files

| File | SHA256 | Rows/size | Notes |
|---|---|---|---|
| `forecasts_2026-04-24.jsonl` | `70837e5c96fa6f033d4241f16dd9f37ac484e4477623d071b22644a9f73e880b` | 12,011 rows / 7.6 MB | Includes 2,633+ rows post-anchor 22:03:41 UTC with H5.I1 + I3 fields |
| `projector_gain.json` | `4979630280fa828eaf249815db3c2d81d05fd1e2d8adab4c0c648243873351aa` | 206 B | Persisted gain at kill-time |
| `tribe_skill.json` | `d04eca0247948e7c9e6186070dd38d3eca69940e96a13bc77687651056ec0396` | 15 KB | TribeCalibrator state |
| `bot_ui_2026-04-24_bybit_detached.err` | `2509e6168a4ae5e62c6ab00a7a14041642125217aa9495a3575f089acbf36e46` | 1.4 MB | Contains `correction clamped hi` warnings + log-emove ~10¹²⁰ bps |

## Known signatures in this baseline

- Per-anchor `expected_move_bps` (persisted): sane ±20 bps range
- Log-emove (in .err, `engine.py:387` format): astronomical ~±10¹²⁰ bps (see WS3_H5_SCOPE.md Q5)
- `abs_activity_bps` (H5.I1 scalar): 1,895/1,945 non-null values ≥ 1e9, last observed 3.46e72
- `step_bps_signflips` (H5.I1 scalar): reproducible across tribes
- `directional_score` (forecast field): 0.0101 ≈ 1/99 — P4 attractor reproduces
- No trades executed (conf → "council incoherent" → hold)

## Compare against

Post-H5.A tape at `data/forecasts/<YYYY-MM-DD>.jsonl` should show:
- `abs_activity_bps` in O(1–100) bps range
- Log-emove and jsonl-emove agreeing within rounding
- `directional_score` non-pinned to 1/99
