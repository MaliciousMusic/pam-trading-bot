"""WS2.5 — Regime-entry gate sprint runner (parallel subprocesses).

Runs a fixed set of `(allowed_entry_regimes, high_conf_override)` variants
against one day's tape at a chosen seed. Each variant executes in its
own subprocess so Python's GIL doesn't serialize them.

Config is patched in-process inside each subprocess by mutating the
cached settings dict — never touches `config/settings.yaml`. Each
variant builds a fresh engine stack via `run_sprint()`.

Usage — run all four variants in parallel:
    python -m scripts.ws2_5_run_variants --day 2026-04-20 --seed 42

Run one variant in this process (used by the orchestrator's fan-out):
    python -m scripts.ws2_5_run_variants --day 2026-04-20 --seed 42 \
        --variant v1_baseline --single
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from datetime import date
from pathlib import Path

from loguru import logger

from src.core.config import PROJECT_ROOT, load, settings
from src.replay.harness import run_sprint


VARIANTS: dict[str, dict] = {
    "v1_baseline": {
        "allowed_entry_regimes": [],
        "high_conf_override": 1.0,
    },
    "v2_trend_only": {
        "allowed_entry_regimes": ["trend_up", "trend_down"],
        "high_conf_override": 1.0,
    },
    "v3_trend_cascade": {
        "allowed_entry_regimes": [
            "trend_up", "trend_down", "cascade_up", "cascade_down",
        ],
        "high_conf_override": 1.0,
    },
    "v4_trend_high_conf": {
        "allowed_entry_regimes": ["trend_up", "trend_down"],
        "high_conf_override": 0.4,
    },
}


def _patch_cfg(patch: dict) -> dict:
    load.cache_clear()
    cfg = settings()
    dec = cfg.setdefault("decision", {})
    for k, v in patch.items():
        dec[k] = copy.deepcopy(v)
    return cfg


async def _run_single(
    variant_id: str,
    *,
    day: date,
    seed: int,
    synth_rate: int,
    outputs_root: Path,
    max_ticks: int | None,
) -> None:
    patch = VARIANTS[variant_id]
    cfg = _patch_cfg(patch)
    dec = cfg["decision"]
    logger.info(
        "[ws2.5] start variant={} seed={} allowed={} high_conf={} max_ticks={}",
        variant_id, seed,
        dec.get("allowed_entry_regimes"),
        dec.get("high_conf_override"),
        max_ticks,
    )
    run_id = f"ws2_5_{variant_id}_seed{seed}"
    result = await run_sprint(
        day=day,
        synth_rate=synth_rate,
        seed=seed,
        run_id=run_id,
        variant_id=variant_id,
        outputs_root=outputs_root,
        max_ticks=max_ticks,
    )
    logger.info(
        "[ws2.5] done  variant={} seed={} ticks={} trades={} Δeq=${:+.2f}",
        variant_id, seed, result.ticks, result.trades, result.equity_delta_usd,
    )


async def _run_parallel(
    wanted: list[str],
    *,
    day: date,
    seed: int,
    synth_rate: int,
    outputs_root: Path,
    max_ticks: int | None,
) -> None:
    """Fan out each variant as a subprocess of this same script in
    --single mode. Running as subprocesses (not asyncio tasks) sidesteps
    the GIL — each variant gets its own core."""
    procs: list[asyncio.subprocess.Process] = []
    log_paths: list[Path] = []
    log_dir = outputs_root / "_ws2_5_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for vid in wanted:
        log_path = log_dir / f"{vid}_seed{seed}.log"
        log_paths.append(log_path)
        cmd = [
            sys.executable, "-m", "scripts.ws2_5_run_variants",
            "--day", day.isoformat(),
            "--seed", str(seed),
            "--synth-rate", str(synth_rate),
            "--variant", vid,
            "--single",
            "--outputs-root", str(outputs_root),
        ]
        if max_ticks is not None:
            cmd.extend(["--max-ticks", str(max_ticks)])
        logger.info("[ws2.5] spawn {} → {}", vid, log_path)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        logfile = log_path.open("wb")
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=logfile,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        procs.append(p)
    rcs = await asyncio.gather(*(p.wait() for p in procs))
    for vid, rc, log_path in zip(wanted, rcs, log_paths):
        logger.info("[ws2.5] {} exit={}  (log: {})", vid, rc, log_path)
    if any(rc != 0 for rc in rcs):
        raise SystemExit("one or more variants failed — see per-variant logs")


async def main_async(args: argparse.Namespace) -> None:
    outputs_root = args.outputs_root or (PROJECT_ROOT / "data" / "replays")
    if args.single:
        if not args.variant:
            raise SystemExit("--single requires --variant")
        if args.variant not in VARIANTS:
            raise SystemExit(f"unknown variant: {args.variant}")
        await _run_single(
            args.variant,
            day=args.day, seed=args.seed,
            synth_rate=args.synth_rate,
            outputs_root=outputs_root,
            max_ticks=args.max_ticks,
        )
        return
    wanted = args.variants or list(VARIANTS.keys())
    for vid in wanted:
        if vid not in VARIANTS:
            raise SystemExit(f"unknown variant: {vid}")
    await _run_parallel(
        wanted,
        day=args.day, seed=args.seed,
        synth_rate=args.synth_rate,
        outputs_root=outputs_root,
        max_ticks=args.max_ticks,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=date.fromisoformat)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--variants", nargs="*", default=None,
                    help="subset of variant ids to run (default: all 4)")
    ap.add_argument("--variant", default=None,
                    help="single variant id — used with --single")
    ap.add_argument("--single", action="store_true",
                    help="run only the one --variant in this process")
    ap.add_argument("--outputs-root", type=Path, default=None)
    ap.add_argument("--max-ticks", type=int, default=None,
                    help="cap tick count (dev only — full day is default)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
