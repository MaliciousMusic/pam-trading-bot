"""Phase 9 smoke test — week-unit experiment with equity carry-over.

Two tests (fixture-based, no network):
  1. equity_carryover   — 2 days × 1 variant: day-1 end equity == day-2 start equity
  2. multi_variant_week — 2 days × 2 variants: carry-over correct for each variant

Usage:  python -m scripts.test_replay_phase9
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from src.core import serde
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.replay.orchestrator import VariantSpec
from src.replay.week_orchestrator import run_week


# ── Fixture data ──────────────────────────────────────────────────────────────

def _make_bars(n: int = 5) -> list[dict]:
    """Minimal bar dicts in kline_client normalized format."""
    return [
        {
            "open_time_ms": 1_700_000_000_000 + i * 60_000,
            "close_time_ms": 1_700_000_000_000 + i * 60_000 + 59_999,
            "open": 65_000.0 + i,
            "high": 65_010.0 + i,
            "low": 64_990.0 + i,
            "close": 65_005.0 + i,
            "volume": 10.0,
            "quote_volume": 650_000.0,
            "taker_buy_quote_volume": 325_000.0,
        }
        for i in range(n)
    ]


def _make_funding() -> list[dict]:
    return [
        {
            "funding_time_ms": 1_700_000_000_000,
            "funding_rate": 0.0001,
            "symbol": "BTCUSDT",
        }
    ]


def _write_fixtures(tmp: Path, days: list[str]) -> tuple[dict[str, Path], dict[str, Path]]:
    bar_paths: dict[str, Path] = {}
    fund_paths: dict[str, Path] = {}
    for d in days:
        bp = tmp / f"bars_{d}.json"
        fp = tmp / f"funding_{d}.json"
        bp.write_bytes(serde.dumps(_make_bars()))
        fp.write_bytes(serde.dumps(_make_funding()))
        bar_paths[d] = bp
        fund_paths[d] = fp
    return bar_paths, fund_paths


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_equity_carryover() -> None:
    """Day-1 end equity == day-2 start equity for a single variant."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        days = ["2024-01-15", "2024-01-16"]
        bar_paths, fund_paths = _write_fixtures(tmp, days)

        variants = [VariantSpec(variant_id="synth_6_seed_42", synth_rate=6, seed=42)]
        result = await run_week(
            start_day=date(2024, 1, 15),
            n_days=2,
            variants=variants,
            outputs_root=tmp / "replays",
            cache_root=tmp,
            symbol="BTCUSDT",
            max_concurrent=1,
            bars_fixture_paths=bar_paths,
            funding_fixture_paths=fund_paths,
            emit_report=False,
        )

        assert result.all_ok, f"week not ok: {[(d.day, d.multi.all_ok) for d in result.days]}"
        assert len(result.days) == 2

        day0 = result.days[0]
        day1 = result.days[1]

        vid = variants[0].variant_id
        r0 = next(o.result for o in day0.multi.variants if o.spec.variant_id == vid)
        r1 = next(o.result for o in day1.multi.variants if o.spec.variant_id == vid)

        assert r0 is not None and r1 is not None
        assert abs(r0.end_equity_usd - r1.start_equity_usd) < 0.01, (
            f"carry-over mismatch: day0.end={r0.end_equity_usd:.4f} "
            f"day1.start={r1.start_equity_usd:.4f}"
        )

        # week_manifest.json exists
        manifest_path = Path(result.week_manifest_path)
        assert manifest_path.exists(), "week_manifest.json not written"
        wm = serde.loads(manifest_path.read_bytes())
        assert wm["n_days"] == 2
        assert wm["all_ok"] is True

        print(
            f"  ok  equity_carryover  "
            f"day0.end=${r0.end_equity_usd:,.2f}  day1.start=${r1.start_equity_usd:,.2f}"
        )


async def test_multi_variant_week() -> None:
    """Two variants × two days: each variant's carry-over is independent."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        days = ["2024-02-01", "2024-02-02"]
        bar_paths, fund_paths = _write_fixtures(tmp, days)

        variants = [
            VariantSpec(variant_id="synth_6_seed_1", synth_rate=6, seed=1),
            VariantSpec(variant_id="synth_6_seed_2", synth_rate=6, seed=2),
        ]
        result = await run_week(
            start_day=date(2024, 2, 1),
            n_days=2,
            variants=variants,
            outputs_root=tmp / "replays",
            cache_root=tmp,
            symbol="BTCUSDT",
            max_concurrent=2,
            bars_fixture_paths=bar_paths,
            funding_fixture_paths=fund_paths,
            emit_report=False,
        )

        assert result.all_ok
        day0, day1 = result.days

        for v in variants:
            vid = v.variant_id
            r0 = next(o.result for o in day0.multi.variants if o.spec.variant_id == vid)
            r1 = next(o.result for o in day1.multi.variants if o.spec.variant_id == vid)
            assert r0 is not None and r1 is not None
            assert abs(r0.end_equity_usd - r1.start_equity_usd) < 0.01, (
                f"{vid}: carry mismatch end={r0.end_equity_usd:.4f} start={r1.start_equity_usd:.4f}"
            )
            print(
                f"  ok  {vid}  "
                f"day0.end=${r0.end_equity_usd:,.2f} -> day1.start=${r1.start_equity_usd:,.2f}"
            )

        # week_manifest has both days and both variants
        wm = serde.loads(Path(result.week_manifest_path).read_bytes())
        assert len(wm["days"]) == 2
        for d in wm["days"]:
            assert len(d["variants"]) == 2


def main() -> int:
    tests = [test_equity_carryover, test_multi_variant_week]
    print(f"running {len(tests)} phase-9 tests...")
    for t in tests:
        try:
            asyncio.run(t())
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            return 1
    print("all phase-9 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
