"""Determinism harness — Phase 0 gate for the tensor-engine refactor.

Runs the sprint replay twice on the same (day, seed, synth_rate) and
diffs the SHA-256 of the resulting decisions.jsonl. MUST be green before
any later phase ships, and re-run after every phase lands.

If the byte-identity contract breaks, the tensor refactor can't be
validated against the OO path and the replay-based audit is meaningless.

Usage:
    python -m scripts.verify_determinism --day 2026-04-20
    python -m scripts.verify_determinism --day 2026-04-20 --synth-rate 60 --seed 42

Exit 0 on bit-identical, 1 on drift.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import tempfile
from datetime import date
from pathlib import Path

from loguru import logger

# Silence the noisy INFO stream from the sprint engine — we only want
# harness-level output on stdout/stderr.
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.core import serde  # noqa: E402

from src.core.config import PROJECT_ROOT, settings  # noqa: E402
from src.replay.harness import run_sprint  # noqa: E402
from src.replay.kline_client import CachedHistoricalClient  # noqa: E402


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _sha256(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _fixture_dir(day: date, symbol: str) -> Path:
    return PROJECT_ROOT / "data" / "fixtures" / f"{symbol}_{day.isoformat()}"


async def _prefetch(day: date, symbol: str) -> tuple[list[dict], list[dict]]:
    """Fetch bars+funding once; persist under data/fixtures/<symbol>_<day>/
    so every later phase's diff uses byte-identical inputs. The built-in
    kline cache refuses to save partial-day responses (common on the most
    recent UTC day while Binance still finalizes), which leaked API variance
    into the determinism test. This fixture snapshot locks the inputs."""
    fdir = _fixture_dir(day, symbol)
    bars_path = fdir / "bars.json"
    funding_path = fdir / "funding.json"
    if bars_path.exists() and funding_path.exists():
        bars = serde.loads(bars_path.read_bytes())
        funding = serde.loads(funding_path.read_bytes())
        print(f"[det] using existing fixture at {fdir}", flush=True)
        return bars, funding
    fdir.mkdir(parents=True, exist_ok=True)
    client = CachedHistoricalClient(
        cache_root=PROJECT_ROOT / "data", symbol=symbol,
    )
    bars = await client.get_day_klines(day)
    funding = await client.get_day_funding(day)
    bars_path.write_bytes(serde.dumps(bars))
    funding_path.write_bytes(serde.dumps(funding))
    print(f"[det] fixture written to {fdir}", flush=True)
    return bars, funding


async def _run_once(
    *, day: date, synth_rate: int, seed: int, outputs: Path, tag: str,
    bars: list[dict], funding: list[dict], symbol: str,
    max_ticks: int | None,
) -> Path:
    result = await run_sprint(
        day=day,
        synth_rate=synth_rate,
        seed=seed,
        run_id=tag,
        variant_id="det_check",
        outputs_root=outputs,
        symbol=symbol,
        bars=bars,
        funding=funding,
        max_ticks=max_ticks,
    )
    return Path(result.decisions_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day,
                    help="YYYY-MM-DD UTC day to replay (must have cached klines)")
    ap.add_argument("--synth-rate", type=int, default=60,
                    help="trade events per 1m bar (default 60 \u2248 1/sec, matches live cadence)")
    ap.add_argument("--seed", type=int, default=42,
                    help="master RNG seed (both runs use this)")
    ap.add_argument("--max-ticks", type=int, default=None,
                    help="cap each run at N ticks for a fast determinism gate "
                         "(full-day by default). Determinism is an all-or-nothing "
                         "property -- a short window catches source-of-randomness "
                         "drift without paying full-day wall-clock.")
    args = ap.parse_args()

    print(
        f"[det] day={args.day.isoformat()} synth_rate={args.synth_rate} "
        f"seed={args.seed} max_ticks={args.max_ticks}",
        flush=True,
    )

    symbol = (settings().get("symbol") or "BTCUSDT")

    with tempfile.TemporaryDirectory(prefix="det_") as tmp:
        outputs = Path(tmp)

        print("[det] prefetching bars+funding once (shared input for both runs)...", flush=True)
        bars, funding = asyncio.run(_prefetch(args.day, symbol))
        print(f"[det] fetched {len(bars)} bars, {len(funding)} funding events", flush=True)

        print("[det] run A -- sprinting...", flush=True)
        path_a = asyncio.run(_run_once(
            day=args.day, synth_rate=args.synth_rate, seed=args.seed,
            outputs=outputs, tag="run_a",
            bars=bars, funding=funding, symbol=symbol,
            max_ticks=args.max_ticks,
        ))
        sha_a, bytes_a = _sha256(path_a)
        print(f"[det] run A done: sha256={sha_a[:16]}... ({bytes_a:,} bytes)", flush=True)

        print("[det] run B -- sprinting...", flush=True)
        path_b = asyncio.run(_run_once(
            day=args.day, synth_rate=args.synth_rate, seed=args.seed,
            outputs=outputs, tag="run_b",
            bars=bars, funding=funding, symbol=symbol,
            max_ticks=args.max_ticks,
        ))
        sha_b, bytes_b = _sha256(path_b)
        print(f"[det] run B done: sha256={sha_b[:16]}... ({bytes_b:,} bytes)", flush=True)

        if sha_a == sha_b and bytes_a == bytes_b:
            print("[det] PASS -- decisions.jsonl byte-identical across runs", flush=True)
            return 0

        print("[det] FAIL -- runs diverged", flush=True)
        print(f"[det]   A sha256 = {sha_a}", flush=True)
        print(f"[det]   B sha256 = {sha_b}", flush=True)
        print(f"[det]   A bytes  = {bytes_a}", flush=True)
        print(f"[det]   B bytes  = {bytes_b}", flush=True)
        # Drop a diff hint: first differing line number.
        a_lines = path_a.read_bytes().splitlines()
        b_lines = path_b.read_bytes().splitlines()
        for i, (la, lb) in enumerate(zip(a_lines, b_lines)):
            if la != lb:
                print(f"[det]   first divergence at line {i+1}", flush=True)
                print(f"[det]     A: {la[:200]!r}", flush=True)
                print(f"[det]     B: {lb[:200]!r}", flush=True)
                break
        else:
            if len(a_lines) != len(b_lines):
                print(
                    f"[det]   line count differs (A={len(a_lines)}, B={len(b_lines)})",
                    flush=True,
                )
        return 1


if __name__ == "__main__":
    sys.exit(main())
