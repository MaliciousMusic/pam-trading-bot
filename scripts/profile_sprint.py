"""Phase 2 — cProfile wrapper for the replay sprint.

Runs a capped sprint under `cProfile.Profile`, dumps the .prof file,
and prints the top-N functions by cumulative and internal time. The
goal is to rank the hot-spots the NumPy vectorization pass should
target.

Output format is intentionally boring — one stats block by `cumulative`
(where time is spent including callees, good for finding the expensive
subsystem), one block by `tottime` (time spent in the function itself
excluding callees, good for finding the leaf that actually burns CPU).

Usage:
    python -m scripts.profile_sprint --day 2026-04-20
    python -m scripts.profile_sprint --day 2026-04-20 --max-ticks 500 --top 40
    python -m scripts.profile_sprint --day 2026-04-20 --out /tmp/sprint.prof
    python -m scripts.profile_sprint --analyze /tmp/sprint.prof --top 30

The `--analyze` mode re-reads an existing .prof file without re-running
the sprint — handy for iterating on filter patterns against the same
trace.
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import pstats
import sys
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from loguru import logger

from src.replay.harness import run_sprint


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _run_sprint_wrapped(
    *, day: date, synth_rate: int, seed: int, max_ticks: int | None,
    outputs: Path, symbol: str,
) -> None:
    """Sync wrapper so cProfile sees the whole sprint as one callable."""
    asyncio.run(run_sprint(
        day=day, synth_rate=synth_rate, seed=seed,
        outputs_root=outputs, symbol=symbol,
        max_ticks=max_ticks,
        run_id="profile", variant_id="phase2",
    ))


def _print_stats(prof_path: Path, top: int, filter_pattern: str | None) -> None:
    """Dump top-N by cumulative and by tottime."""
    # Re-route pstats to a buffer so we can prefix lines and avoid color
    # noise from the loguru sink above.
    for sort_key in ("cumulative", "tottime"):
        buf = StringIO()
        p = pstats.Stats(str(prof_path), stream=buf)
        p.strip_dirs()
        p.sort_stats(sort_key)
        if filter_pattern:
            p.print_stats(filter_pattern, top)
        else:
            p.print_stats(top)
        print()
        print(f"=== top {top} by {sort_key} "
              f"{'(filter='+filter_pattern+')' if filter_pattern else ''} ===")
        print(buf.getvalue())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", type=_parse_day,
                    help="YYYY-MM-DD UTC day to replay "
                         "(required unless --analyze)")
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-ticks", type=int, default=500,
                    help="Cap the sprint length for fast iteration "
                         "(default 500). Pass 0 for full-day.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--out", type=Path, default=None,
                    help="Where to write the .prof file. Default: "
                         "./data/perf/sprint_<day>_<ticks>t.prof")
    ap.add_argument("--top", type=int, default=30,
                    help="How many functions to show in each stats block.")
    ap.add_argument("--filter",
                    help="pstats filter pattern (regex); restricts to "
                         "matching function names. Useful to zoom into "
                         "e.g. 'src/tribes' or 'projector'.")
    ap.add_argument("--analyze", type=Path, default=None,
                    help="Skip the run and just re-analyze an existing "
                         ".prof file.")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    if args.analyze is not None:
        if not args.analyze.exists():
            print(f"[prof] no such file: {args.analyze}")
            return 2
        _print_stats(args.analyze, args.top, args.filter)
        return 0

    if args.day is None:
        print("[prof] --day is required unless --analyze is given")
        return 2

    if args.out is None:
        perf_dir = Path("data") / "perf"
        perf_dir.mkdir(parents=True, exist_ok=True)
        ticks_tag = f"{args.max_ticks}t" if args.max_ticks else "full"
        args.out = perf_dir / f"sprint_{args.day.isoformat()}_{ticks_tag}.prof"

    print(f"[prof] running sprint: day={args.day} "
          f"max_ticks={args.max_ticks or 'full'} "
          f"synth_rate={args.synth_rate} seed={args.seed}")
    print(f"[prof] output will be: {args.out}")

    with tempfile.TemporaryDirectory(prefix="prof_") as tmp:
        outputs = Path(tmp)
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            _run_sprint_wrapped(
                day=args.day, synth_rate=args.synth_rate, seed=args.seed,
                max_ticks=(args.max_ticks or None),
                outputs=outputs, symbol=args.symbol,
            )
        finally:
            profiler.disable()
        profiler.dump_stats(str(args.out))

    print(f"[prof] wrote {args.out} ({args.out.stat().st_size / 1024:.1f} KB)")
    _print_stats(args.out, args.top, args.filter)
    return 0


if __name__ == "__main__":
    sys.exit(main())
