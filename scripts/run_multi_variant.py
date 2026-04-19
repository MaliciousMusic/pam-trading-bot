"""Run a multi-variant sprint over one historical day.

Fans out N variants of the same (day, symbol) across OS processes so they
actually run in parallel — no GIL serialization. Parent pre-fetches the
kline/funding cache once so workers all hit disk, not Binance.

Usage:
    # Three synth rates in parallel, default seed:
    python -m scripts.run_multi_variant --day 2026-04-15 \\
        --synth-rates 6,60,600

    # Seed sweep at one synth rate:
    python -m scripts.run_multi_variant --day 2026-04-15 \\
        --synth-rates 60 --seeds 1,2,3,4,5

    # Full grid (synth × seed cartesian):
    python -m scripts.run_multi_variant --day 2026-04-15 \\
        --synth-rates 6,60 --seeds 42,99

Variant ids default to `synth_<rate>` when only one seed is in play, or
`synth_<rate>_seed_<seed>` when seeds sweep — so output paths never
collide within a run.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path

from loguru import logger

from src.replay.orchestrator import VariantSpec, run_multi_variant


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _parse_int_list(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _build_variants(rates: list[int], seeds: list[int]) -> list[VariantSpec]:
    """Cartesian product (rate × seed). Disambiguate the variant_id only
    when a sweep is actually in play — the common single-seed case stays
    clean as `synth_<rate>`."""
    sweep_seeds = len(seeds) > 1
    out: list[VariantSpec] = []
    for r in rates:
        for s in seeds:
            vid = f"synth_{r}_seed_{s}" if sweep_seeds else f"synth_{r}"
            out.append(VariantSpec(variant_id=vid, synth_rate=r, seed=s))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day,
                    help="YYYY-MM-DD UTC day to replay")
    ap.add_argument("--synth-rates", default="6,60,600", type=_parse_int_list,
                    help="comma-separated events/bar rates (default 6,60,600)")
    ap.add_argument("--seeds", default="42", type=_parse_int_list,
                    help="comma-separated RNG seeds (default 42)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--run-id", default=None,
                    help="override auto-generated run id (default "
                         "multi_<day>_<HHMMSS>)")
    ap.add_argument("--outputs-root", default=None, type=Path,
                    help="override outputs dir (default data/replays)")
    ap.add_argument("--cache-root", default=None, type=Path,
                    help="override cache dir (default data/)")
    ap.add_argument("--max-concurrent", default=None, type=int,
                    help="cap parallel workers (default cpu_count-1, bounded "
                         "by n_variants)")
    args = ap.parse_args()

    variants = _build_variants(args.synth_rates, args.seeds)
    logger.info(
        "[multi] day={} rates={} seeds={} → {} variants",
        args.day.isoformat(), args.synth_rates, args.seeds, len(variants),
    )

    result = asyncio.run(run_multi_variant(
        day=args.day,
        variants=variants,
        run_id=args.run_id,
        outputs_root=args.outputs_root,
        cache_root=args.cache_root,
        symbol=args.symbol,
        max_concurrent=args.max_concurrent,
    ))

    print()
    print(f"  run_id       : {result.run_id}")
    print(f"  day          : {result.day}")
    print(f"  symbol       : {result.symbol}")
    print(f"  wall         : {result.wall_seconds:.1f}s  (parent)")
    print(f"  rollup       : {result.rollup_path}")
    print(f"  variants     : {len(result.variants)}")
    print()
    header = (f"    {'variant_id':<26} {'rate':>5} {'seed':>5} "
              f"{'code':>4} {'wall':>7} {'ticks':>6} {'trades':>6} "
              f"{'Δeq':>10}")
    print(header)
    print("    " + "-" * (len(header) - 4))
    for o in result.variants:
        if o.result is not None:
            ticks = f"{o.result.ticks}"
            trades = f"{o.result.trades}"
            deq = f"{o.result.equity_delta_usd:+,.2f}"
        else:
            ticks = trades = deq = "—"
        print(f"    {o.spec.variant_id:<26} {o.spec.synth_rate:>5} "
              f"{o.spec.seed:>5} {o.exit_code:>4} "
              f"{o.wall_seconds:>6.1f}s {ticks:>6} {trades:>6} {deq:>10}")

    if not result.all_ok:
        print()
        print("  WARNING: one or more variants failed — see worker.stderr.log"
              " in each variant dir")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
