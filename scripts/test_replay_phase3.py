"""Phase 3 verification — the multi-variant orchestrator actually parallelizes.

Exit criterion for phase 3: running N variants through `run_multi_variant`
    1. produces well-formed outputs under `<outputs_root>/<run_id>/variants/<variant_id>/`
       (decisions.jsonl, manifest.json, worker.stdout.log, worker.stderr.log)
       plus a rollup multi_manifest.json,
    2. actually runs workers in parallel (parent wall is well under the sum of
       per-worker walls — the whole point of spawning subprocesses),
    3. preserves phase-2 determinism end-to-end across the subprocess boundary
       (same variant spec twice → byte-identical decisions.jsonl).

Runs against a 10-bar synthetic fixture — same shape as phase-2 — so the
test stays fast enough to gate a commit. No network.

Usage:  python -m scripts.test_replay_phase3
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from src.core import serde
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.replay.orchestrator import VariantSpec, run_multi_variant  # noqa: E402


FIXTURE_DAY = date(1970, 1, 2)
FIXTURE_SEED = 12345
FIXTURE_BARS = 10


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


def _build_fixture() -> tuple[list[dict], list[dict]]:
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
        bars.append(_fixture_bar(
            start_ms + i * 60_000, o, h, l, c,
            volume=max(0.1, rng.gauss(40.0, 15.0)),
            taker_buy_ratio=max(0.1, min(0.9, 0.5 + rng.gauss(0, 0.08))),
        ))
        price = c
    funding = [{"funding_time_ms": start_ms + 5 * 60_000, "funding_rate": 0.00012}]
    return bars, funding


def _write_fixtures(tmp: Path) -> tuple[Path, Path]:
    bars, funding = _build_fixture()
    bars_path = tmp / "bars.json"
    funding_path = tmp / "funding.json"
    bars_path.write_bytes(serde.dumps(bars))
    funding_path.write_bytes(serde.dumps(funding))
    return bars_path, funding_path


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _run(outputs: Path, cache: Path, bars_path: Path, funding_path: Path,
         variants: list[VariantSpec], run_id: str):
    return asyncio.run(run_multi_variant(
        day=FIXTURE_DAY,
        variants=variants,
        run_id=run_id,
        outputs_root=outputs,
        cache_root=cache,
        bars_fixture_path=bars_path,
        funding_fixture_path=funding_path,
    ))


def test_multi_variant_outputs_shape() -> None:
    """Each variant produces a complete artifact set, rollup is well-formed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        outputs = tmp_p / "outputs"
        cache = tmp_p / "cache"
        bars_p, funding_p = _write_fixtures(tmp_p)

        variants = [
            VariantSpec(variant_id="synth_6", synth_rate=6, seed=42),
            VariantSpec(variant_id="synth_60", synth_rate=60, seed=42),
        ]
        r = _run(outputs, cache, bars_p, funding_p, variants, run_id="test_shape")

        assert r.all_ok, (
            f"orchestrator reported not-ok: {[(o.spec.variant_id, o.exit_code) for o in r.variants]}"
        )
        for o in r.variants:
            vd = outputs / r.run_id / "variants" / o.spec.variant_id
            for name in ("decisions.jsonl", "manifest.json",
                         "worker.stdout.log", "worker.stderr.log"):
                p = vd / name
                assert p.exists() and p.stat().st_size >= 0, f"missing {p}"
            m = serde.loads((vd / "manifest.json").read_bytes())
            assert m["synth_rate"] == o.spec.synth_rate, f"manifest synth_rate mismatch for {o.spec.variant_id}"
            assert m["seed"] == o.spec.seed, f"manifest seed mismatch for {o.spec.variant_id}"
            assert m["ticks"] > 0
            assert o.result is not None
            assert o.result.ticks == m["ticks"]

        rollup_path = Path(r.rollup_path)
        assert rollup_path.exists()
        rollup = serde.loads(rollup_path.read_bytes())
        assert rollup["run_id"] == r.run_id
        assert len(rollup["variants"]) == len(variants)
        for v in rollup["variants"]:
            assert v["exit_code"] == 0
            assert v["ticks"] is not None
        print(f"  ok  {len(variants)} variants shaped correctly; rollup ok")


def test_multi_variant_runs_in_parallel() -> None:
    """Parent wall must be clearly less than the sum of per-worker walls — that's the
    whole point of using subprocesses instead of asyncio tasks. Bound is loose
    (<=0.85 * sum) so the test stays stable under CI jitter and Python startup
    overhead; we just want to prove it isn't serial."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        outputs = tmp_p / "outputs"
        cache = tmp_p / "cache"
        bars_p, funding_p = _write_fixtures(tmp_p)

        variants = [
            VariantSpec(variant_id="synth_6_a", synth_rate=6, seed=42),
            VariantSpec(variant_id="synth_6_b", synth_rate=6, seed=43),
        ]
        r = _run(outputs, cache, bars_p, funding_p, variants, run_id="test_parallel")
        assert r.all_ok, "variants must succeed before we measure wall"

        sum_worker_wall = sum(o.wall_seconds for o in r.variants)
        parent_wall = r.wall_seconds
        ratio = parent_wall / max(sum_worker_wall, 1e-6)
        assert ratio < 0.85, (
            f"orchestrator looks serial: parent={parent_wall:.1f}s "
            f"vs sum_workers={sum_worker_wall:.1f}s (ratio={ratio:.2f}) — "
            f"expected <0.85 for 2 workers running in parallel"
        )
        print(f"  ok  parent={parent_wall:.1f}s  sum_workers={sum_worker_wall:.1f}s  "
              f"ratio={ratio:.2f} (<0.85 means parallel)")


def test_determinism_through_subprocess() -> None:
    """Same (variant_id, synth_rate, seed) run twice → byte-identical decisions.jsonl.
    Phase-2 proved this within one process; phase 3 must preserve it across the
    subprocess boundary and independent Python starts."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        outputs = tmp_p / "outputs"
        cache = tmp_p / "cache"
        bars_p, funding_p = _write_fixtures(tmp_p)

        variants = [VariantSpec(variant_id="synth_6", synth_rate=6, seed=42)]
        r1 = _run(outputs, cache, bars_p, funding_p, variants, run_id="det_run_a")
        r2 = _run(outputs, cache, bars_p, funding_p, variants, run_id="det_run_b")

        assert r1.all_ok and r2.all_ok
        d1 = outputs / r1.run_id / "variants" / "synth_6" / "decisions.jsonl"
        d2 = outputs / r2.run_id / "variants" / "synth_6" / "decisions.jsonl"
        h1, h2 = _sha256(d1), _sha256(d2)
        assert h1 == h2, (
            f"determinism broken through subprocess: {h1} vs {h2}\n"
            f"  {d1}\n  {d2}"
        )
        size = d1.stat().st_size
        print(f"  ok  cross-process byte-identical ({size:,} bytes, hash={h1[:12]}...)")


def main() -> int:
    tests = [
        test_multi_variant_outputs_shape,
        test_multi_variant_runs_in_parallel,
        test_determinism_through_subprocess,
    ]
    print(f"running {len(tests)} phase-3 tests...")
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
    print("all phase-3 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
