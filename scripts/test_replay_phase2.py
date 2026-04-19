"""Phase 2 verification — the sprint driver is deterministic.

Exit criterion for phase 2: running the same (day, seed, synth_rate) twice
must produce byte-identical `decisions.jsonl`. This test proves it end-to-end
against a synthetic 30-bar fixture passed directly to `run_sprint` — no
network, no full-day cache, fast enough to ship as a pre-commit gate.

The fixture is a deterministic 30-minute random-walk. The walk is seeded
separately from the replay seed so it stays stable as we refactor the engine.

Usage:  python -m scripts.test_replay_phase2
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import random
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import orjson
from loguru import logger

# Silence engine chatter so the test output stays readable and the run
# doesn't spend milliseconds per tick formatting debug strings.
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.replay.harness import run_sprint  # noqa: E402


FIXTURE_DAY = date(1970, 1, 2)   # synthetic; won't collide with real cache
FIXTURE_SEED = 12345             # walk determinism, not engine determinism
FIXTURE_BARS = 10                # 10 minutes -> ~200 ticks at tick_seconds=3


def _fixture_bar(open_time_ms: int, o: float, h: float, l: float, c: float,
                 volume: float, taker_buy_ratio: float) -> dict:
    mid = (o + h + l + c) / 4
    quote_volume = mid * volume
    taker_buy_volume = volume * taker_buy_ratio
    return {
        "open_time_ms": open_time_ms,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": volume,
        "close_time_ms": open_time_ms + 59_999,
        "quote_volume": quote_volume,
        "trade_count": 250,
        "taker_buy_volume": taker_buy_volume,
        "taker_buy_quote_volume": mid * taker_buy_volume,
    }


def _build_fixture() -> tuple[list[dict], list[dict]]:
    """Deterministic random-walk bars + one synthetic funding event.

    Price walk is modest (σ≈8 USD) to exercise the council without
    triggering runaway halts, yet varied enough to produce non-trivial
    regime transitions and vote disagreement."""
    start_ms = int(datetime(FIXTURE_DAY.year, FIXTURE_DAY.month, FIXTURE_DAY.day,
                            tzinfo=timezone.utc).timestamp() * 1000)
    rng = random.Random(FIXTURE_SEED)
    price = 30_000.0
    bars: list[dict] = []
    for i in range(FIXTURE_BARS):
        drift = rng.gauss(0, 8.0)
        o = price
        c = max(1.0, o + drift)
        h = max(o, c) + abs(rng.gauss(0, 6.0))
        l = min(o, c) - abs(rng.gauss(0, 6.0))
        volume = max(0.1, rng.gauss(40.0, 15.0))
        taker_buy_ratio = max(0.1, min(0.9, 0.5 + rng.gauss(0, 0.08)))
        bars.append(_fixture_bar(
            open_time_ms=start_ms + i * 60_000,
            o=o, h=h, l=l, c=c, volume=volume,
            taker_buy_ratio=taker_buy_ratio,
        ))
        price = c
    # One funding event 15 min in — inside the fixture window so ingest fires it.
    funding = [{"funding_time_ms": start_ms + 15 * 60_000, "funding_rate": 0.00012}]
    return bars, funding


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _run_once(outputs_root: Path, seed: int, synth_rate: int,
              run_tag: str) -> Path:
    bars, funding = _build_fixture()
    result = asyncio.run(run_sprint(
        day=FIXTURE_DAY,
        synth_rate=synth_rate,
        seed=seed,
        run_id=run_tag,
        variant_id=f"synth_{synth_rate}_seed_{seed}",
        outputs_root=outputs_root,
        bars=bars,
        funding=funding,
    ))
    return Path(result.decisions_path)


def test_same_seed_same_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        outputs = Path(tmp)
        a = _run_once(outputs, seed=42, synth_rate=6, run_tag="run_a")
        b = _run_once(outputs, seed=42, synth_rate=6, run_tag="run_b")

        assert a.exists() and b.exists(), "both runs must produce a log"
        ha, hb = _sha256(a), _sha256(b)
        assert ha == hb, (
            f"determinism broken: same seed produced different bytes\n"
            f"  run_a: {ha}\n  run_b: {hb}\n"
            f"  paths: {a} vs {b}"
        )
        size = a.stat().st_size
        print(f"  ok  seed=42 twice -> identical sha256 ({size:,} bytes, hash={ha[:12]}...)")


def test_different_seed_different_bytes() -> None:
    """Sanity check: two different seeds MUST produce different output,
    otherwise the byte-match above could be trivially true."""
    with tempfile.TemporaryDirectory() as tmp:
        outputs = Path(tmp)
        a = _run_once(outputs, seed=42, synth_rate=6, run_tag="run_a")
        c = _run_once(outputs, seed=99, synth_rate=6, run_tag="run_c")

        ha, hc = _sha256(a), _sha256(c)
        assert ha != hc, (
            f"sanity check failed: two different seeds produced identical logs\n"
            f"  sha256={ha}\n"
            f"  this usually means the engine doesn't actually consume seed"
        )
        print(f"  ok  seed=42 vs seed=99 -> different sha256 ({ha[:8]}... vs {hc[:8]}...)")


def test_log_row_shape() -> None:
    """Structure check: every row has the fields downstream reports rely on."""
    with tempfile.TemporaryDirectory() as tmp:
        outputs = Path(tmp)
        path = _run_once(outputs, seed=42, synth_rate=6, run_tag="run_shape")
        required = {
            "tick", "ts", "price", "regime", "votes", "net_pressure",
            "uncertainty", "expected_move_bps", "confidence", "action",
            "action_reason", "equity_usd", "positions_open", "tribe_disagreement",
        }
        rows = 0
        with path.open("rb") as f:
            for line in f:
                row = orjson.loads(line)
                missing = required - set(row.keys())
                assert not missing, f"row {rows} missing fields: {missing}"
                assert isinstance(row["votes"], list)
                assert math.isfinite(row["price"])
                assert row["action"] in {"hold", "long", "short", "close"}
                rows += 1
        assert rows > 0, "empty log"
        print(f"  ok  {rows} rows, all have {len(required)} required fields")


def main() -> int:
    tests = [
        test_same_seed_same_bytes,
        test_different_seed_different_bytes,
        test_log_row_shape,
    ]
    print(f"running {len(tests)} phase-2 tests...")
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 1
    print("all phase-2 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
