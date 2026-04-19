"""Run a sprint-mode replay of one historical day.

Usage:
    python -m scripts.run_replay --day 2026-04-15
    python -m scripts.run_replay --day 2026-04-15 --synth-rate 60
    python -m scripts.run_replay --day 2026-04-15 --synth-rate 6 --seed 7

Fetches 1m klines + 8h funding for the given day (cached under `data/`),
synthesizes a trade+funding event stream at `--synth-rate` events/min per
bar, replays it through the full tribes→council→decision stack, and writes
a per-tick decisions.jsonl to `data/replays/<run_id>/variants/<variant_id>/`.

Cold start intentionally — no warmup, no HTF pre-fill. Determinism: same
(day, seed, synth-rate) produces byte-identical decisions.jsonl.

Phase 3 will add multi-variant orchestration (synth 6/60/600 in parallel).
This CLI stays focused on single-variant runs as the building block.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path

import orjson
from loguru import logger

from src.replay.harness import run_sprint


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day,
                    help="YYYY-MM-DD UTC day to replay")
    ap.add_argument("--synth-rate", type=int, default=60,
                    help="trade events per 1m bar (default 60 ≈ 1/sec)")
    ap.add_argument("--seed", type=int, default=42,
                    help="master RNG seed for deterministic tribe streams")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--run-id", default=None,
                    help="override auto-generated run id")
    ap.add_argument("--variant-id", default=None,
                    help="override auto-generated variant id "
                         "(default: synth_<rate>)")
    ap.add_argument("--outputs-root", default=None, type=Path,
                    help="override outputs dir (default data/replays) — used by "
                         "the multi-variant orchestrator to point workers at "
                         "a shared run dir")
    ap.add_argument("--cache-root", default=None, type=Path,
                    help="override kline/funding cache dir (default data/) — "
                         "orchestrator pre-fetches to this dir so workers hit "
                         "the cache instead of re-fetching in parallel")
    ap.add_argument("--bars-fixture", default=None, type=Path,
                    help="JSON array of bar dicts; bypasses cache/network. "
                         "Primary use: tests and custom stress scenarios.")
    ap.add_argument("--funding-fixture", default=None, type=Path,
                    help="JSON array of funding dicts; bypasses cache/network.")
    ap.add_argument("--starting-equity", type=float, default=None,
                    help="starting equity USD (overrides config base_capital_usd). "
                         "Used by week orchestrator to carry equity across days.")
    ap.add_argument("--tribes-dir", default=None, type=Path,
                    help="path to tribe YAMLs for this variant; defaults to "
                         "config/tribes/. Phase 13 evolutionary loop points "
                         "this at config/tribes_variants/<gen>/variant_<id>/.")
    args = ap.parse_args()

    bars = funding = None
    if args.bars_fixture is not None:
        bars = orjson.loads(args.bars_fixture.read_bytes())
    if args.funding_fixture is not None:
        funding = orjson.loads(args.funding_fixture.read_bytes())

    logger.info(
        "[replay] day={} synth_rate={} seed={} symbol={}",
        args.day.isoformat(), args.synth_rate, args.seed, args.symbol,
    )

    result = asyncio.run(run_sprint(
        day=args.day,
        synth_rate=args.synth_rate,
        seed=args.seed,
        run_id=args.run_id,
        variant_id=args.variant_id,
        symbol=args.symbol,
        outputs_root=args.outputs_root,
        cache_root=args.cache_root,
        bars=bars,
        funding=funding,
        starting_equity_usd=args.starting_equity,
        tribes_dir=args.tribes_dir,
    ))

    print()
    print(f"  run_id        : {result.run_id}")
    print(f"  variant_id    : {result.variant_id}")
    print(f"  day           : {result.day}")
    print(f"  seed          : {result.seed}")
    print(f"  synth_rate    : {result.synth_rate}")
    print(f"  ticks         : {result.ticks}")
    print(f"  events        : {result.events_consumed}")
    print(f"  trades        : {result.trades}  (wins={result.wins})")
    print(f"  start_equity  : ${result.start_equity_usd:,.2f}")
    print(f"  end_equity    : ${result.end_equity_usd:,.2f}")
    print(f"  Δequity       : ${result.equity_delta_usd:+,.2f}")
    print(f"  decisions     : {result.decisions_path}")
    print(f"  manifest      : {result.manifest_path}")


if __name__ == "__main__":
    main()
