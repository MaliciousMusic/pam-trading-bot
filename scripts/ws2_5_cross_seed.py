"""WS2.5 — cross-seed validation of a winning variant (parallel).

Fans out one chosen variant under a list of seeds against the same
day's tape, one subprocess per seed. Shares config-patching logic with
`ws2_5_run_variants` by invoking it in --single mode.

Usage:
    python -m scripts.ws2_5_cross_seed --day 2026-04-20 --variant v2_trend_only
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from loguru import logger

from scripts.ws2_5_run_variants import VARIANTS
from src.core.config import PROJECT_ROOT


DEFAULT_SEEDS = [42, 123, 777, 2024]


async def _run_parallel(
    variant_id: str,
    *,
    day: date,
    seeds: list[int],
    synth_rate: int,
    outputs_root: Path,
    max_ticks: int | None,
) -> None:
    procs: list[asyncio.subprocess.Process] = []
    log_paths: list[Path] = []
    log_dir = outputs_root / "_ws2_5_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    for seed in seeds:
        log_path = log_dir / f"{variant_id}_seed{seed}.log"
        log_paths.append(log_path)
        cmd = [
            sys.executable, "-m", "scripts.ws2_5_run_variants",
            "--day", day.isoformat(),
            "--seed", str(seed),
            "--synth-rate", str(synth_rate),
            "--variant", variant_id,
            "--single",
            "--outputs-root", str(outputs_root),
        ]
        if max_ticks is not None:
            cmd.extend(["--max-ticks", str(max_ticks)])
        logger.info("[ws2.5 cross] spawn seed={} -> {}", seed, log_path)
        logfile = log_path.open("wb")
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=logfile, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        procs.append(p)
    rcs = await asyncio.gather(*(p.wait() for p in procs))
    for seed, rc, log_path in zip(seeds, rcs, log_paths):
        logger.info("[ws2.5 cross] seed={} exit={}  (log: {})", seed, rc, log_path)
    if any(rc != 0 for rc in rcs):
        raise SystemExit("one or more seeds failed")


async def main_async(args: argparse.Namespace) -> None:
    outputs_root = args.outputs_root or (PROJECT_ROOT / "data" / "replays")
    if args.variant not in VARIANTS:
        raise SystemExit(f"unknown variant: {args.variant}")
    await _run_parallel(
        args.variant,
        day=args.day, seeds=args.seeds,
        synth_rate=args.synth_rate,
        outputs_root=outputs_root,
        max_ticks=args.max_ticks,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=date.fromisoformat)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--seeds", type=int, nargs="*", default=DEFAULT_SEEDS)
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--outputs-root", type=Path, default=None)
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
