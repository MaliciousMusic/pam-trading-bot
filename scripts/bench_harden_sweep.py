"""Phase 10-Harden — Multi-seed Merciless shock sweep.

Wraps `scripts.bench_harden_shock._run_one` in a loop across several seeds
so we can measure whether the -0.51 % reality tax we saw on seed 42 is a
structural constant or a lucky/unlucky draw.

Usage:
    python -m scripts.bench_harden_sweep \
        --day 2026-04-20 --max-ticks 500 \
        --seeds 42,123,777,2024 \
        --out docs/perf_baselines/harden_sweep.json \
        --md-out docs/perf_baselines/harden_sweep.md

Each seed drives BOTH the sprint RNG (tribes, world-shocks, projector,
etc.) AND the Harden overlay RNG (adversarial draws, latency sampling).
This is the strictest same-tape comparison: every other knob is held
constant across runs.

Exit codes:
  0   sweep produced
  2   bad args / a run failed
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from datetime import date
from pathlib import Path

from loguru import logger

from src.core import serde
from src.execution.harden import HardenConfig
# Reuse the single-seed machinery — no need to duplicate monkey-patch logic.
from scripts.bench_harden_shock import _run_one


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _parse_seeds(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


async def _sweep(args: argparse.Namespace) -> dict:
    day = _parse_day(args.day)
    seeds = _parse_seeds(args.seeds)
    pairs: list[dict] = []
    t0 = time.perf_counter()
    for i, seed in enumerate(seeds, 1):
        logger.info("[sweep] seed {}/{}: {}", i, len(seeds), seed)
        gentle_cfg = HardenConfig(enabled=False)
        merc_cfg = HardenConfig(
            enabled=True,
            adv_mult=args.adv_mult,
            adv_floor_bps=args.adv_floor_bps,
            p_spike=args.p_spike,
            spike_scale_s=args.spike_scale_s,
            spike_alpha=args.spike_alpha,
            sqrt_k=args.sqrt_k,
            seed=seed,
        )
        gentle = await _run_one(
            day=day, synth_rate=args.synth_rate, seed=seed,
            max_ticks=args.max_ticks, symbol=args.symbol,
            harden=gentle_cfg, label=f"gentle_s{seed}",
        )
        merc = await _run_one(
            day=day, synth_rate=args.synth_rate, seed=seed,
            max_ticks=args.max_ticks, symbol=args.symbol,
            harden=merc_cfg, label=f"merciless_s{seed}",
        )
        d_eq = merc["end_equity_usd"] - gentle["end_equity_usd"]
        d_pct = 100.0 * d_eq / gentle["start_equity_usd"]
        pairs.append({
            "seed": seed,
            "gentle": gentle,
            "merciless": merc,
            "delta": {
                "end_equity_usd": d_eq,
                "end_equity_pct_of_start": d_pct,
                "trade_count": merc["trades"] - gentle["trades"],
                "win_rate_pct": merc["win_rate_pct"] - gentle["win_rate_pct"],
                "entry_slip_bps": merc["entry_slip_bps_avg"] - gentle["entry_slip_bps_avg"],
                "wins_lost": gentle["wins"] - merc["wins"],
            },
        })
        logger.info("[sweep] seed {} done  tax={:+.3f}%  slip bump={:+.2f}bp",
                    seed, d_pct, pairs[-1]["delta"]["entry_slip_bps"])

    wall_s = time.perf_counter() - t0
    # --- aggregate statistics -----------------------------------------
    taxes_pct = [p["delta"]["end_equity_pct_of_start"] for p in pairs]
    slip_bumps = [p["delta"]["entry_slip_bps"] for p in pairs]
    win_rate_deltas = [p["delta"]["win_rate_pct"] for p in pairs]
    wins_lost = [p["delta"]["wins_lost"] for p in pairs]
    pareto_counts_merc = [p["merciless"]["pareto_latencies"] for p in pairs]
    deferred_counts_merc = [p["merciless"]["deferred_opens"] for p in pairs]
    stats = {
        "n": len(pairs),
        "tax_pct_mean": round(statistics.fmean(taxes_pct), 4),
        "tax_pct_median": round(statistics.median(taxes_pct), 4),
        "tax_pct_stdev": (round(statistics.stdev(taxes_pct), 4)
                          if len(taxes_pct) > 1 else 0.0),
        "tax_pct_min": round(min(taxes_pct), 4),
        "tax_pct_max": round(max(taxes_pct), 4),
        "slip_bump_bps_mean": round(statistics.fmean(slip_bumps), 3),
        "slip_bump_bps_stdev": (round(statistics.stdev(slip_bumps), 3)
                                if len(slip_bumps) > 1 else 0.0),
        "win_rate_delta_mean_pp": round(statistics.fmean(win_rate_deltas), 2),
        "win_rate_delta_stdev_pp": (round(statistics.stdev(win_rate_deltas), 2)
                                    if len(win_rate_deltas) > 1 else 0.0),
        "wins_lost_total": sum(wins_lost),
        "pareto_draws_total_merc": sum(pareto_counts_merc),
        "deferred_fills_total_merc": sum(deferred_counts_merc),
        "wall_seconds": round(wall_s, 1),
    }
    return {
        "params": {
            "day": str(day),
            "seeds": seeds,
            "synth_rate": args.synth_rate,
            "max_ticks": args.max_ticks,
            "symbol": args.symbol,
        },
        "pairs": pairs,
        "stats": stats,
    }


def _fmt_pct(x: float) -> str:
    return f"{x:+.3f} %"


def _fmt_bp(x: float) -> str:
    return f"{x:+.2f} bp"


def _markdown_report(payload: dict) -> str:
    stats = payload["stats"]
    pairs = payload["pairs"]
    lines: list[str] = []
    lines.append("# Phase 10-Harden - Merciless Shock Sweep")
    lines.append("")
    params = payload["params"]
    lines.append(f"Day **{params['day']}**  |  "
                 f"seeds **{', '.join(str(s) for s in params['seeds'])}**  |  "
                 f"ticks/seed **{params['max_ticks']}**  |  "
                 f"synth rate **{params['synth_rate']}**")
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")
    lines.append(f"- **Runs:** {stats['n']} seeded pairs "
                 f"(Gentle + Merciless each, same tape, same seed)")
    lines.append(f"- **Reality tax (mean):** {_fmt_pct(stats['tax_pct_mean'])}  "
                 f"stdev {stats['tax_pct_stdev']:.3f} pp  "
                 f"range [{stats['tax_pct_min']:+.3f} %, "
                 f"{stats['tax_pct_max']:+.3f} %]")
    lines.append(f"- **Reality tax (median):** {_fmt_pct(stats['tax_pct_median'])}")
    lines.append(f"- **Slippage inflation (mean):** "
                 f"{_fmt_bp(stats['slip_bump_bps_mean'])}  "
                 f"stdev {stats['slip_bump_bps_stdev']:.2f} bp")
    lines.append(f"- **Win-rate delta (mean):** "
                 f"{stats['win_rate_delta_mean_pp']:+.2f} pp  "
                 f"stdev {stats['win_rate_delta_stdev_pp']:.2f} pp")
    lines.append(f"- **Winners flipped to losers across sweep:** "
                 f"{stats['wins_lost_total']}")
    lines.append(f"- **Pareto-tail draws across sweep:** "
                 f"{stats['pareto_draws_total_merc']}  "
                 f"(deferred fills: {stats['deferred_fills_total_merc']})")
    lines.append(f"- **Sweep wall-time:** {stats['wall_seconds']} s")
    lines.append("")

    lines.append("## Per-seed breakdown")
    lines.append("")
    lines.append("| Seed | Gentle eq ($) | Merciless eq ($) | Tax ($) | Tax (%) | Slip bump (bp) | Win-rate delta | Pareto / Deferred |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in pairs:
        g = p["gentle"]; m = p["merciless"]; d = p["delta"]
        lines.append(
            f"| {p['seed']} "
            f"| {g['end_equity_usd']:,.2f} "
            f"| {m['end_equity_usd']:,.2f} "
            f"| {d['end_equity_usd']:+,.2f} "
            f"| {d['end_equity_pct_of_start']:+.3f} "
            f"| {d['entry_slip_bps']:+.2f} "
            f"| {d['win_rate_pct']:+.2f} pp "
            f"| {m['pareto_latencies']} / {m['deferred_opens']} |"
        )
    lines.append("")

    lines.append("## Trade-level integrity check")
    lines.append("")
    lines.append("Gentle and Merciless must produce the *same* council "
                 "decisions on the same seed (the decision layer is "
                 "isolated from the executor). A per-seed mismatch in "
                 "trade count would be a bug in the harden wiring.")
    lines.append("")
    lines.append("| Seed | Gentle trades | Merciless trades | Match? |")
    lines.append("|---:|---:|---:|:---:|")
    for p in pairs:
        gt = p["gentle"]["trades"]
        mt = p["merciless"]["trades"]
        lines.append(f"| {p['seed']} | {gt} | {mt} | "
                     f"{'OK' if gt == mt else 'FAIL'} |")
    lines.append("")

    # Verdict: is the tax a constant or seed-driven?
    spread = stats["tax_pct_max"] - stats["tax_pct_min"]
    lines.append("## Verdict")
    lines.append("")
    if stats["tax_pct_stdev"] < 0.15 and spread < 0.5:
        verdict = ("**CONSTANT-LIKE.** Reality tax is tight across seeds "
                   "(stdev < 0.15 pp, range < 0.5 pp). The -0.51 % "
                   "single-seed number appears to be a structural "
                   "friction constant, not a lucky draw.")
    elif stats["tax_pct_mean"] < 0 and stats["tax_pct_max"] < 0:
        verdict = ("**ALWAYS NEGATIVE, VARIABLE MAGNITUDE.** Every seed "
                   "loses under Merciless, but the magnitude varies by "
                   f"{spread:.2f} pp across seeds. The friction is real "
                   "but its size depends on which trades happen to get "
                   "taxed.")
    elif stats["tax_pct_mean"] < 0:
        verdict = ("**NEGATIVE MEAN, NOT UNIFORM.** Most seeds lose but "
                   "at least one seed breaks even or gains under "
                   "Merciless. Likely driven by which trades happened to "
                   "benefit from the adversarial-fill direction.")
    else:
        verdict = ("**NO SYSTEMATIC TAX.** Either the harden layer is "
                   "too gentle or we got lucky on this seed set. Needs "
                   "more samples before trusting the signal.")
    lines.append(verdict)
    lines.append("")
    lines.append("_Stability Gate reminder: 7 An-0 days under "
                 "`harden.enabled: true`, Sharpe > 2.0, zero guard alerts._")
    lines.append("")
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    payload = await _sweep(args)
    md = _markdown_report(payload)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(serde.dumps_indent(payload))
        logger.info("[sweep] wrote JSON snapshot -> {}", out)
    if args.md_out:
        md_out = Path(args.md_out)
        md_out.parent.mkdir(parents=True, exist_ok=True)
        md_out.write_text(md, encoding="utf-8")
        logger.info("[sweep] wrote markdown report -> {}", md_out)

    # Now stdout (tolerate cp1252 on Windows).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        print(md)
    except UnicodeEncodeError:
        print(md.encode("ascii", errors="replace").decode("ascii"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--seeds", default="42,123,777,2024",
                    help="Comma-separated seeds (default: 42,123,777,2024)")
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--max-ticks", type=int, default=500)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--adv-mult", type=float, default=0.5)
    ap.add_argument("--adv-floor-bps", type=float, default=1.5)
    ap.add_argument("--p-spike", type=float, default=0.02)
    ap.add_argument("--spike-scale-s", type=float, default=1.5)
    ap.add_argument("--spike-alpha", type=float, default=1.3)
    ap.add_argument("--sqrt-k", type=float, default=0.8)
    ap.add_argument("--out", help="JSON snapshot output path (optional)")
    ap.add_argument("--md-out", help="Markdown report output path (optional)")
    args = ap.parse_args()
    try:
        return asyncio.run(_amain(args))
    except Exception as e:
        logger.exception("[sweep] failed: {}", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
