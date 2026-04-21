"""Baseline capture \u2014 Phase 0 contract for the tensor-engine refactor.

Runs one sprint replay on a recorded day and locks down the numbers the
later phases must match or beat. Specifically:

    - decisions.jsonl SHA-256     \u2192 parity target for Phase 2 tensor refactor
    - per-tick wall-clock p50/p99 \u2192 latency target
    - tracemalloc peak Python heap\u2192 memory ceiling
    - final equity + trades       \u2192 behavioral invariant

Scope note: this uses `SprintEngine`, which intentionally omits auditor,
calibrator, kernel_calibrator, and shadow_ledgers (see harness.py:63).
So this baseline locks the projector/decision/executor path. Shadow-ledger
parity will be captured at Phase 2 entry via a separate live-bot replay.

Usage:
    python -m scripts.capture_baseline --day 2026-04-20
    python -m scripts.capture_baseline --day 2026-04-20 --out data/baseline/custom.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sys
import time
import tracemalloc
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.core import serde  # noqa: E402

from src.core.config import PROJECT_ROOT, settings  # noqa: E402
from src.replay import harness as _harness  # noqa: E402
from src.replay.harness import run_sprint  # noqa: E402
from src.replay.kline_client import CachedHistoricalClient  # noqa: E402


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _fixture_dir(day: date, symbol: str) -> Path:
    return PROJECT_ROOT / "data" / "fixtures" / f"{symbol}_{day.isoformat()}"


async def _prefetch(day: date, symbol: str) -> tuple[list[dict], list[dict]]:
    """Load (or fetch+persist) the day's bars+funding so every phase's
    diff runs on byte-identical input. Shares the fixture layout with
    scripts/verify_determinism.py."""
    fdir = _fixture_dir(day, symbol)
    bars_path = fdir / "bars.json"
    funding_path = fdir / "funding.json"
    if bars_path.exists() and funding_path.exists():
        return (
            serde.loads(bars_path.read_bytes()),
            serde.loads(funding_path.read_bytes()),
        )
    fdir.mkdir(parents=True, exist_ok=True)
    client = CachedHistoricalClient(
        cache_root=PROJECT_ROOT / "data", symbol=symbol,
    )
    bars = await client.get_day_klines(day)
    funding = await client.get_day_funding(day)
    bars_path.write_bytes(serde.dumps(bars))
    funding_path.write_bytes(serde.dumps(funding))
    return bars, funding


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile; ms already. Empty list \u2192 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _instrument_sprint_engine() -> list[float]:
    """Monkey-patch SprintEngine._tick to record per-tick durations in ms.

    Returns the list; caller reads it after run_sprint completes.
    """
    durations_ms: list[float] = []
    original_tick = _harness.SprintEngine._tick

    def timed_tick(self, ts):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return original_tick(self, ts)
        finally:
            durations_ms.append((time.perf_counter() - t0) * 1000.0)

    _harness.SprintEngine._tick = timed_tick  # type: ignore[assignment]
    return durations_ms


async def _go(
    *, day: date, synth_rate: int, seed: int, out_root: Path,
    bars: list[dict], funding: list[dict], symbol: str,
) -> dict:
    result = await run_sprint(
        day=day,
        synth_rate=synth_rate,
        seed=seed,
        run_id=f"baseline_{day.isoformat()}",
        variant_id="phase0",
        outputs_root=out_root,
        symbol=symbol,
        bars=bars,
        funding=funding,
    )
    return {
        "run_id": result.run_id,
        "ticks": result.ticks,
        "events_consumed": result.events_consumed,
        "trades": result.trades,
        "wins": result.wins,
        "start_equity_usd": result.start_equity_usd,
        "end_equity_usd": result.end_equity_usd,
        "equity_delta_usd": result.equity_delta_usd,
        "decisions_path": result.decisions_path,
        "manifest_path": result.manifest_path,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day,
                    help="YYYY-MM-DD UTC day to replay")
    ap.add_argument("--synth-rate", type=int, default=60,
                    help="trade events per 1m bar (default 60, matches live cadence)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=None,
                    help="baseline JSON path (default data/baseline/phase0_<day>_<ts>.json)")
    args = ap.parse_args()

    baseline_dir = PROJECT_ROOT / "data" / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    if args.out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = baseline_dir / f"phase0_{args.day.isoformat()}_{stamp}.json"
    else:
        out_path = args.out

    sprint_outputs = baseline_dir / "sprint_runs"
    sprint_outputs.mkdir(parents=True, exist_ok=True)

    cfg = settings()
    clock_cfg = cfg.get("clock", {}) or {}
    symbol = (cfg.get("symbol") or "BTCUSDT")

    print(
        f"[baseline] day={args.day.isoformat()} synth_rate={args.synth_rate} "
        f"seed={args.seed} out={out_path}",
        flush=True,
    )
    print(
        f"[baseline] config: tick_seconds={clock_cfg.get('tick_seconds')} "
        f"projector_steps={clock_cfg.get('projector_steps')}",
        flush=True,
    )

    print("[baseline] loading fixture...", flush=True)
    bars, funding = asyncio.run(_prefetch(args.day, symbol))
    print(f"[baseline] {len(bars)} bars, {len(funding)} funding events", flush=True)

    tick_durations_ms = _instrument_sprint_engine()

    tracemalloc.start()
    t_start = time.perf_counter()
    sprint_info = asyncio.run(_go(
        day=args.day, synth_rate=args.synth_rate, seed=args.seed,
        out_root=sprint_outputs,
        bars=bars, funding=funding, symbol=symbol,
    ))
    t_total_s = time.perf_counter() - t_start
    mem_current, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    decisions_path = Path(sprint_info["decisions_path"])
    decisions_bytes = decisions_path.read_bytes()
    decisions_sha256 = hashlib.sha256(decisions_bytes).hexdigest()

    # Per-tick latency stats. SprintEngine._tick skips the first call
    # when last_price is None, but perf overhead is tiny and doesn't
    # distort the quantiles.
    avg_ms = sum(tick_durations_ms) / len(tick_durations_ms) if tick_durations_ms else 0.0
    p50_ms = _pct(tick_durations_ms, 50)
    p95_ms = _pct(tick_durations_ms, 95)
    p99_ms = _pct(tick_durations_ms, 99)
    max_ms = max(tick_durations_ms) if tick_durations_ms else 0.0

    baseline = {
        "phase": 0,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "branch": "feat/tensor-engine",
        "git_head_at_capture": None,  # filled by wrapper shell or git rev-parse below
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "day": args.day.isoformat(),
        "synth_rate": args.synth_rate,
        "seed": args.seed,
        "config": {
            "tick_seconds": clock_cfg.get("tick_seconds"),
            "projector_steps": clock_cfg.get("projector_steps"),
        },
        "sprint": sprint_info,
        "decisions_sha256": decisions_sha256,
        "decisions_bytes": len(decisions_bytes),
        "timing": {
            "total_wall_clock_s": round(t_total_s, 3),
            "ticks_instrumented": len(tick_durations_ms),
            "per_tick_ms": {
                "avg": round(avg_ms, 3),
                "p50": round(p50_ms, 3),
                "p95": round(p95_ms, 3),
                "p99": round(p99_ms, 3),
                "max": round(max_ms, 3),
            },
        },
        "memory": {
            # tracemalloc measures Python-heap allocations, not RSS. Good for
            # relative comparison across phases; not a replacement for true
            # RSS which comes online with psutil in Phase 0.5.
            "tracemalloc_peak_bytes": int(mem_peak),
            "tracemalloc_peak_mb": round(mem_peak / (1024 * 1024), 2),
            "tracemalloc_final_bytes": int(mem_current),
        },
        "scope_notes": [
            "SprintEngine path -- no shadow_ledgers, no auditor, no calibrator.",
            "Shadow ledger baseline deferred to Phase 2 entry (live-bot replay).",
            "tracemalloc tracks Python heap only; true RSS arrives in Phase 0.5.",
        ],
    }

    # Stamp git HEAD so the baseline is tied to a specific commit.
    try:
        import subprocess
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
        ).strip()
        baseline["git_head_at_capture"] = head
    except Exception:
        pass

    out_path.write_text(json.dumps(baseline, indent=2))
    print(f"[baseline] written {out_path}", flush=True)

    # Human summary. ASCII-only so Windows cp1252 stdout doesn't crash
    # the capture after the JSON is already written.
    print("", flush=True)
    print("  -- BASELINE (Phase 0) --", flush=True)
    print(f"  day              : {args.day.isoformat()}", flush=True)
    print(f"  ticks            : {sprint_info['ticks']:,}", flush=True)
    print(f"  events consumed  : {sprint_info['events_consumed']:,}", flush=True)
    print(f"  trades (wins)    : {sprint_info['trades']} ({sprint_info['wins']})", flush=True)
    print(f"  start equity     : ${sprint_info['start_equity_usd']:,.2f}", flush=True)
    print(f"  end equity       : ${sprint_info['end_equity_usd']:,.2f}", flush=True)
    print(f"  delta equity     : ${sprint_info['equity_delta_usd']:+,.2f}", flush=True)
    print(f"  wall clock       : {t_total_s:.2f}s", flush=True)
    print(
        f"  per-tick ms      : avg={avg_ms:.3f}  p50={p50_ms:.3f}  "
        f"p95={p95_ms:.3f}  p99={p99_ms:.3f}  max={max_ms:.3f}",
        flush=True,
    )
    print(f"  heap peak        : {mem_peak/(1024*1024):.2f} MB (tracemalloc)", flush=True)
    print(f"  decisions sha256 : {decisions_sha256}", flush=True)
    print(f"  decisions bytes  : {len(decisions_bytes):,}", flush=True)
    print(f"  output           : {out_path}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
