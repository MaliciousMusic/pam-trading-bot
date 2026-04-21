# Phase 10-Harden — Research Archive

**Chapter closed:** 2026-04-21. The engine now runs against the Merciless overlay by default (`config/settings.yaml` → `harden.enabled: true`). All artifacts below are frozen snapshots from the R&D sprint that produced this baseline.

---

## Contents

| File | What it is |
|---|---|
| `harden_shock.json` | Single-seed (seed=42) Gentle-vs-Merciless shock pair — raw payload |
| `harden_shock.md` | Human-readable shock report — seed 42, 2026-04-20, 500 ticks |
| `harden_sweep.json` | 4-seed sweep (`{42, 123, 777, 2024}`) — raw payload |
| `harden_sweep.md` | Aggregated sweep report — friction-constant verdict |

---

## Methodology

Same day, same tape, same tribe config, same event stream. Toggle one variable: `harden.enabled`. Every other knob is held constant across the Gentle/Merciless pair.

The Merciless overlay is three independently-gated adversarial layers:

1. **Adversarial fill** — `adv_bps = clip(adv_mult × vol_30s_bps + adv_floor_bps, 0, adv_cap_bps)`, added to every fill. Defaults: `adv_mult=0.5`, `adv_floor_bps=1.5`, `adv_cap_bps=20`.
2. **Fat-tailed latency** — `Pareto(α=1.3, scale=1.5 s)` with probability `p_spike=0.02`, else `Exponential(mean=0.08 s)`. Hard-capped at `spike_cap_s=30 s`. Draws ≥ 1 tick defer the fill to a future tick's mark (`PaperExecutor._pending_fills`).
3. **Sqrt-slippage overlay** — `depth_bps_effective = depth_bps × (1 + sqrt_k × √(size / sqrt_ref_usd))`. Default: `sqrt_k=0.8`, `sqrt_ref_usd=100_000`.

Determinism: `HardenState` owns a seeded `numpy.random.default_rng` (PCG64). Same `(day, seed, synth_rate, harden.seed)` → bit-identical adversarial trajectory.

---

## Headline findings

### Single-seed shock (seed 42)
- Reality tax: **−0.508 % of starting equity** (−$254.16)
- Entry slippage: **2.59 → 4.80 bp** (+2.21 bp, +85 %)
- Wins: **28 → 20** (−8; same 102 trade decisions, different outcomes)
- Win rate: **27.5 % → 19.6 %**

### Multi-seed sweep `{42, 123, 777, 2024}`
- Reality tax: mean **−0.450 %**, stdev **0.055 pp**, range `[−0.508 %, −0.377 %]`
- Slippage inflation: mean **+2.20 bp**, stdev **0.02 bp** — structural constant
- Win-rate delta: mean **−7.01 pp**, stdev **5.43 pp** — seed-variable (which trades get taxed changes)
- Pareto-tail draws: **8** total over 4×500 ticks — empirical rate aligns with `p_spike=0.02`
- Trade counts match Gentle-to-Merciless for all 4 seeds → decision layer cleanly isolated from executor overlay

**Verdict: CONSTANT-LIKE.** The Merciless overlay subtracts ≈0.45 pp of starting equity per tape on the current knobs. This is the production-time reality tax the council must carry.

---

## Reproduction

From repo root, `.venv314t` active:

```bash
# Single-seed shock
python -m scripts.bench_harden_shock --day 2026-04-20 --max-ticks 500 --seed 42 \
  --out research/phase10_harden/harden_shock.json \
  --md-out research/phase10_harden/harden_shock.md

# 4-seed sweep
python -m scripts.bench_harden_sweep --day 2026-04-20 --max-ticks 500 \
  --seeds 42,123,777,2024 \
  --out research/phase10_harden/harden_sweep.json \
  --md-out research/phase10_harden/harden_sweep.md
```

Both scripts inject `HardenConfig` via a scoped monkey-patch of `settings()` — no yaml edit needed. The frozen `harden.enabled: true` in `config/settings.yaml` is for live sprint / watch / live mode.

---

## What's next

This chapter is closed. See `docs/STABILITY_GATE.md` for the G2 gate conditions the engine must now clear before any live-capital conversation.
