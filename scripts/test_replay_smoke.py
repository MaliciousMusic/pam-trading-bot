"""Smoke test — one tiny sprint through the harness, verify it completes.

This is the minimal proof-of-life for phase 2. If this doesn't finish
cleanly, the determinism test can't either. Runs a 10-bar fixture
(~200 ticks) and prints sha256 of the output.

Usage:  python -m scripts.test_replay_smoke
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.replay.harness import run_sprint  # noqa: E402


def _fixture_bar(open_time_ms, o, h, l, c, volume, taker_buy_ratio):
    mid = (o + h + l + c) / 4
    return {
        "open_time_ms": open_time_ms,
        "open": o, "high": h, "low": l, "close": c,
        "volume": volume, "close_time_ms": open_time_ms + 59_999,
        "quote_volume": mid * volume, "trade_count": 250,
        "taker_buy_volume": volume * taker_buy_ratio,
        "taker_buy_quote_volume": mid * volume * taker_buy_ratio,
    }


def _build_fixture(nbars: int):
    start_ms = int(datetime(1970, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    rng = random.Random(12345)
    price = 30_000.0
    bars = []
    for i in range(nbars):
        drift = rng.gauss(0, 8.0)
        o = price
        c = max(1.0, o + drift)
        h = max(o, c) + abs(rng.gauss(0, 6.0))
        l = min(o, c) - abs(rng.gauss(0, 6.0))
        bars.append(_fixture_bar(
            start_ms + i * 60_000, o, h, l, c,
            volume=max(0.1, rng.gauss(40.0, 15.0)),
            taker_buy_ratio=max(0.1, min(0.9, 0.5 + rng.gauss(0, 0.08))),
        ))
        price = c
    funding = [{"funding_time_ms": start_ms + 5 * 60_000, "funding_rate": 0.00012}]
    return bars, funding


async def _go(outputs, tag):
    bars, funding = _build_fixture(10)
    return await run_sprint(
        day=date(1970, 1, 2),
        synth_rate=6,
        seed=42,
        run_id=tag,
        variant_id="smoke",
        outputs_root=outputs,
        bars=bars,
        funding=funding,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        outputs = Path(tmp)
        print("[smoke] starting sprint A...", flush=True)
        ra = asyncio.run(_go(outputs, "run_a"))
        print(f"[smoke] A done: ticks={ra.ticks} trades={ra.trades}", flush=True)

        print("[smoke] starting sprint B...", flush=True)
        rb = asyncio.run(_go(outputs, "run_b"))
        print(f"[smoke] B done: ticks={rb.ticks} trades={rb.trades}", flush=True)

        a = Path(ra.decisions_path).read_bytes()
        b = Path(rb.decisions_path).read_bytes()
        ha = hashlib.sha256(a).hexdigest()
        hb = hashlib.sha256(b).hexdigest()

        print(f"[smoke] A sha256={ha[:16]}...  ({len(a)} bytes)", flush=True)
        print(f"[smoke] B sha256={hb[:16]}...  ({len(b)} bytes)", flush=True)
        if ha == hb:
            print("[smoke] PASS: byte-identical", flush=True)
            return 0
        print("[smoke] FAIL: bytes differ", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
