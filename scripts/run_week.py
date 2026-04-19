"""Run a week-unit experiment across M consecutive days.

Usage:
    python -m scripts.run_week --start 2026-04-14 --days 5
    python -m scripts.run_week --start 2026-04-14 --days 5 --synth-rates 6 60 --seed 42

Runs all (synth_rate × seed) variant combinations across N consecutive days.
Equity carries over: end equity of variant V on day N becomes its starting
equity on day N+1.

Writes all outputs under data/replays/<week_id>/ including week_manifest.json
and (if all variants pass) week_report.zip.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path

from loguru import logger

from src.replay.orchestrator import VariantSpec
from src.replay.week_orchestrator import run_week


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, type=_parse_day,
                    help="First day of the week (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=5,
                    help="Number of consecutive days (default 5)")
    ap.add_argument("--synth-rates", type=int, nargs="+", default=[60],
                    help="Trade events per 1m bar (default [60])")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="Max parallel variants per day (default: cpu_count-1)")
    ap.add_argument("--week-id", default=None,
                    help="Override auto-generated week id")
    ap.add_argument("--outputs-root", default=None, type=Path)
    ap.add_argument("--cache-root", default=None, type=Path)
    args = ap.parse_args()

    variants = [
        VariantSpec(
            variant_id=f"synth_{rate}_seed_{args.seed}",
            synth_rate=rate,
            seed=args.seed,
        )
        for rate in args.synth_rates
    ]

    logger.info(
        "[week-cli] start={} days={} variants={} symbol={}",
        args.start.isoformat(), args.days,
        [v.variant_id for v in variants], args.symbol,
    )

    result = asyncio.run(run_week(
        start_day=args.start,
        n_days=args.days,
        variants=variants,
        week_id=args.week_id,
        outputs_root=args.outputs_root,
        cache_root=args.cache_root,
        symbol=args.symbol,
        max_concurrent=args.max_concurrent,
        emit_report=True,
    ))

    print()
    print(f"  week_id       : {result.week_id}")
    print(f"  start_day     : {result.start_day}")
    print(f"  n_days        : {result.n_days}")
    print(f"  all_ok        : {result.all_ok}")
    print()
    for dr in result.days:
        for outcome in dr.multi.variants:
            r = outcome.result
            if r:
                print(
                    f"  {dr.day}  {r.variant_id:30s}"
                    f"  start=${r.start_equity_usd:>9,.2f}"
                    f"  end=${r.end_equity_usd:>9,.2f}"
                    f"  Δ=${r.equity_delta_usd:>+9,.2f}"
                    f"  trades={r.trades}"
                )
            else:
                print(f"  {dr.day}  {outcome.spec.variant_id:30s}  FAILED (exit={outcome.exit_code})")
    print()
    print(f"  manifest      : {result.week_manifest_path}")
    if result.zip_path:
        print(f"  report zip    : {result.zip_path}")


if __name__ == "__main__":
    main()
