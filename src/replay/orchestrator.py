"""Multi-variant sprint orchestrator — fan out N variants across processes.

Runs N variants of the same (day, symbol) concurrently as separate OS
processes, each invoking `scripts.run_replay`. Pre-fetches the day's cache
once in the parent so workers hit disk rather than racing for the same
Binance endpoint. Collects per-variant `SprintResult` from the manifest
each worker writes, plus parent-side wall-clock so we can measure the
parallelism win.

Why subprocesses, not asyncio tasks:
  The engine is CPU-bound (tribe voting + projector walk). Python's GIL
  makes N asyncio tasks in one process serial for this work. Real
  subprocesses use all cores — wall time = max(single), not sum.

Why manifest re-read instead of stdout pipe:
  The worker already writes a complete manifest.json as part of its
  determinism contract. Piping stdout would duplicate that contract and
  introduce encoding surprises on Windows. File-based handshake is
  robust and leaves a post-mortem trail on failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import orjson
from loguru import logger

from ..core.config import PROJECT_ROOT, settings
from .harness import SprintResult
from .kline_client import CachedHistoricalClient


@dataclass(slots=True, frozen=True)
class VariantSpec:
    """One knob combination to run.

    `seed` is separate from `variant_id` so the same name can re-run under
    a different seed without output-path collision — the orchestrator
    composes the directory from `variant_id` only, so pick unique ids
    (e.g. `synth_60_seed_42` vs `synth_60_seed_99`).

    `tribes_dir` (Phase 13) points the worker at a per-variant council.
    None → use the canonical `config/tribes/` baseline. Phase 9 variants
    that only differ in synth_rate/seed leave it None; evolved generations
    set it to `config/tribes_variants/<gen>/<variant_id>/`.
    """
    variant_id: str
    synth_rate: int
    seed: int = 42
    tribes_dir: Path | None = None


@dataclass(slots=True, frozen=True)
class VariantOutcome:
    spec: VariantSpec
    result: SprintResult | None
    exit_code: int
    wall_seconds: float
    stdout_path: Path
    stderr_path: Path


@dataclass(slots=True)
class MultiVariantResult:
    run_id: str
    day: str
    symbol: str
    variants: list[VariantOutcome]
    start_utc: str
    end_utc: str
    wall_seconds: float
    rollup_path: str
    zip_path: str | None = None

    @property
    def all_ok(self) -> bool:
        return all(o.exit_code == 0 and o.result is not None for o in self.variants)


async def _prefetch_day(
    day: date, symbol: str, cache_root: Path
) -> tuple[int, int]:
    client = CachedHistoricalClient(cache_root=cache_root, symbol=symbol)
    bars, funding = await asyncio.gather(
        client.get_day_klines(day),
        client.get_day_funding(day),
    )
    return len(bars), len(funding)


def _worker_cmd(
    *, day: date, spec: VariantSpec, run_id: str, outputs_root: Path,
    cache_root: Path, symbol: str,
    bars_fixture_path: Path | None, funding_fixture_path: Path | None,
    starting_equity: float | None = None,
) -> list[str]:
    cmd = [
        sys.executable, "-u", "-m", "scripts.run_replay",
        "--day", day.isoformat(),
        "--synth-rate", str(spec.synth_rate),
        "--seed", str(spec.seed),
        "--symbol", symbol,
        "--run-id", run_id,
        "--variant-id", spec.variant_id,
        "--outputs-root", str(outputs_root),
        "--cache-root", str(cache_root),
    ]
    if bars_fixture_path is not None:
        cmd += ["--bars-fixture", str(bars_fixture_path)]
    if funding_fixture_path is not None:
        cmd += ["--funding-fixture", str(funding_fixture_path)]
    if starting_equity is not None:
        cmd += ["--starting-equity", str(starting_equity)]
    if spec.tribes_dir is not None:
        cmd += ["--tribes-dir", str(spec.tribes_dir)]
    return cmd


async def _run_worker(
    *, day: date, spec: VariantSpec, run_id: str, outputs_root: Path,
    cache_root: Path, symbol: str, sem: asyncio.Semaphore,
    env: dict[str, str],
    bars_fixture_path: Path | None, funding_fixture_path: Path | None,
    starting_equity: float | None = None,
) -> VariantOutcome:
    async with sem:
        variant_dir = outputs_root / run_id / "variants" / spec.variant_id
        variant_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = variant_dir / "worker.stdout.log"
        stderr_path = variant_dir / "worker.stderr.log"
        cmd = _worker_cmd(
            day=day, spec=spec, run_id=run_id, outputs_root=outputs_root,
            cache_root=cache_root, symbol=symbol,
            bars_fixture_path=bars_fixture_path,
            funding_fixture_path=funding_fixture_path,
            starting_equity=starting_equity,
        )
        started = time.monotonic()
        logger.info("[orch] spawn {}  cmd={}", spec.variant_id, " ".join(cmd))
        with stdout_path.open("wb") as out_f, stderr_path.open("wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=out_f, stderr=err_f, env=env,
                cwd=str(PROJECT_ROOT),
            )
            exit_code = await proc.wait()
        wall = time.monotonic() - started

        result: SprintResult | None = None
        manifest_path = variant_dir / "manifest.json"
        if exit_code == 0 and manifest_path.exists():
            m = orjson.loads(manifest_path.read_bytes())
            result = SprintResult(
                run_id=m["run_id"],
                variant_id=m["variant_id"],
                day=m["day"],
                seed=m["seed"],
                synth_rate=m["synth_rate"],
                ticks=m["ticks"],
                events_consumed=m["events_consumed"],
                start_equity_usd=m["start_equity_usd"],
                end_equity_usd=m["end_equity_usd"],
                equity_delta_usd=m["equity_delta_usd"],
                trades=m["trades"],
                wins=m["wins"],
                decisions_path=str(variant_dir / "decisions.jsonl"),
                manifest_path=str(manifest_path),
            )
        logger.info(
            "[orch] done  {}  code={}  wall={:.1f}s  trades={}",
            spec.variant_id, exit_code, wall,
            result.trades if result else "?",
        )
        return VariantOutcome(
            spec=spec, result=result, exit_code=exit_code, wall_seconds=wall,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )


async def run_multi_variant(
    *,
    day: date,
    variants: Sequence[VariantSpec],
    run_id: str | None = None,
    outputs_root: Path | None = None,
    cache_root: Path | None = None,
    symbol: str = "BTCUSDT",
    max_concurrent: int | None = None,
    bars_fixture_path: Path | None = None,
    funding_fixture_path: Path | None = None,
    emit_report: bool = True,
    starting_equities: dict[str, float] | None = None,
) -> MultiVariantResult:
    """Spawn one subprocess per variant and wait for all to finish.

    Parent pre-fetches the day's cache so workers read from disk instead
    of racing for the Binance endpoint. Per-variant stdout/stderr are
    captured to files in each variant's output dir — a reader doesn't
    need to be streaming at the time a worker crashes.
    """
    if not variants:
        raise ValueError("run_multi_variant: variants is empty")
    seen: set[str] = set()
    for v in variants:
        if v.variant_id in seen:
            raise ValueError(f"duplicate variant_id: {v.variant_id!r}")
        seen.add(v.variant_id)

    cfg = settings()
    repl = cfg.get("replay", {}) or {}
    if outputs_root is None:
        outputs_root = PROJECT_ROOT / repl.get("outputs_dir", "data/replays")
    if cache_root is None:
        cache_root = PROJECT_ROOT / repl.get("cache_dir", "data")
    outputs_root = Path(outputs_root)
    cache_root = Path(cache_root)

    if run_id is None:
        run_id = f"multi_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"

    if max_concurrent is None:
        max_concurrent = min(len(variants), max(1, (os.cpu_count() or 2) - 1))
    max_concurrent = max(1, int(max_concurrent))

    logger.info(
        "[orch] run={}  day={}  n_variants={}  max_parallel={}",
        run_id, day.isoformat(), len(variants), max_concurrent,
    )

    # Fixtures bypass the cache entirely — no network, no kline/funding I/O
    # in the workers. Used by tests and custom scenarios.
    if bars_fixture_path is None and funding_fixture_path is None:
        bars_n, funding_n = await _prefetch_day(day, symbol, cache_root)
        logger.info("[orch] cache warm — {} bars + {} funding", bars_n, funding_n)
    else:
        logger.info(
            "[orch] fixture mode — bars={} funding={}",
            bars_fixture_path, funding_fixture_path,
        )

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    sem = asyncio.Semaphore(max_concurrent)
    started = time.monotonic()
    start_utc = datetime.utcnow().isoformat() + "Z"

    outcomes = await asyncio.gather(*[
        _run_worker(
            day=day, spec=v, run_id=run_id, outputs_root=outputs_root,
            cache_root=cache_root, symbol=symbol, sem=sem, env=env,
            bars_fixture_path=bars_fixture_path,
            funding_fixture_path=funding_fixture_path,
            starting_equity=(starting_equities or {}).get(v.variant_id),
        )
        for v in variants
    ])

    wall = time.monotonic() - started
    end_utc = datetime.utcnow().isoformat() + "Z"

    rollup = {
        "run_id": run_id,
        "day": day.isoformat(),
        "symbol": symbol,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "wall_seconds": wall,
        "max_concurrent": max_concurrent,
        "variants": [
            {
                "variant_id": o.spec.variant_id,
                "synth_rate": o.spec.synth_rate,
                "seed": o.spec.seed,
                "exit_code": o.exit_code,
                "wall_seconds": o.wall_seconds,
                "ticks": o.result.ticks if o.result else None,
                "trades": o.result.trades if o.result else None,
                "wins": o.result.wins if o.result else None,
                "equity_delta_usd": o.result.equity_delta_usd if o.result else None,
            }
            for o in outcomes
        ],
    }
    rollup_path = outputs_root / run_id / "multi_manifest.json"
    rollup_path.parent.mkdir(parents=True, exist_ok=True)
    rollup_path.write_bytes(orjson.dumps(rollup, option=orjson.OPT_INDENT_2))
    logger.info(
        "[orch] wrote {}  total_wall={:.1f}s",
        rollup_path, wall,
    )

    zip_path: str | None = None
    all_ok = all(o.exit_code == 0 for o in outcomes)
    if emit_report and all_ok:
        try:
            from .report_zip import build_zip
            zp = build_zip(rollup_path.parent)
            zip_path = str(zp)
            logger.info("[orch] report zip -> {}", zp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[orch] report build failed: {}", exc)

    return MultiVariantResult(
        run_id=run_id, day=day.isoformat(), symbol=symbol,
        variants=list(outcomes), start_utc=start_utc, end_utc=end_utc,
        wall_seconds=wall, rollup_path=str(rollup_path), zip_path=zip_path,
    )
