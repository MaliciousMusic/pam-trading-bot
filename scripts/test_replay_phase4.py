"""Phase 4 verification — the report builder produces a valid .zip.

No network, no subprocesses. Constructs a synthetic run directory that
mirrors the real harness output layout, then calls build_zip and validates
every invariant of the result.

Usage:  python -m scripts.test_replay_phase4
"""
from __future__ import annotations

import math
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from src.core import serde
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.replay.report import load_run_metrics  # noqa: E402
from src.replay.report_html import render_html  # noqa: E402
from src.replay.report_md import render_md      # noqa: E402
from src.replay.report_zip import build_zip     # noqa: E402


TRIBES = ["momentum_bot", "market_maker", "whale_accumulator",
          "copy_trader", "retail_app_newbie"]


def _make_tick_row(tick: int, base_price: float, equity: float) -> dict:
    votes = [
        {"tribe_id": t,
         "side": "buy" if tick % 3 != 0 else "sell",
         "conviction": 0.5 + (hash(t) % 10) / 30,
         "reason": "test"}
        for t in TRIBES
    ]
    return {
        "tick": tick,
        "ts": datetime(1970, 1, 2, 0, tick // 20, tick % 20, tzinfo=timezone.utc).isoformat(),
        "price": base_price + tick * 0.5,
        "regime": "chop" if tick % 4 == 0 else "trending_up",
        "votes": votes,
        "net_pressure": math.sin(tick * 0.3) * 0.6,
        "uncertainty": 0.3 + abs(math.cos(tick * 0.2)) * 0.4,
        "expected_move_bps": 5.0,
        "confidence": 0.55,
        "action": ["hold", "long", "hold", "hold", "close"][tick % 5],
        "action_reason": "test",
        "equity_usd": equity + tick * 0.1,
        "positions_open": tick % 3,
        "tribe_disagreement": 0.2,
    }


def _write_variant(variants_dir: Path, variant_id: str,
                   synth_rate: int, seed: int, n_ticks: int = 200) -> Path:
    vd = variants_dir / variant_id
    vd.mkdir(parents=True)

    manifest = {
        "run_id": variants_dir.parent.name,
        "variant_id": variant_id,
        "day": "1970-01-02",
        "symbol": "BTCUSDT",
        "seed": seed,
        "synth_rate": synth_rate,
        "tick_seconds": 3,
        "bars": 10,
        "funding_events": 1,
        "ticks": n_ticks,
        "events_consumed": n_ticks * synth_rate // 6,
        "start_equity_usd": 5000.0,
        "end_equity_usd": 5000.0 + n_ticks * 0.1,
        "equity_delta_usd": n_ticks * 0.1,
        "trades": n_ticks // 20,
        "wins": n_ticks // 40,
        "created_at_utc": "1970-01-02T00:00:00Z",
    }
    (vd / "manifest.json").write_bytes(serde.dumps_indent(manifest))

    with (vd / "decisions.jsonl").open("wb") as f:
        for i in range(1, n_ticks + 1):
            f.write(serde.dumps_indent(_make_tick_row(i, 30_000.0, 5000.0)))
            f.write(b"\n")

    ledger_dir = vd / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "1970-01-02.jsonl").write_bytes(b'{"type":"test"}\n')

    return vd


def _write_run(tmp: Path, n_variants: int = 2) -> Path:
    run_id = "test_phase4_run"
    run_dir = tmp / run_id
    run_dir.mkdir()

    variants: list[dict] = []
    vd = run_dir / "variants"
    vd.mkdir()
    for i in range(n_variants):
        rate = [6, 60][i % 2]
        vid = f"synth_{rate}"
        _write_variant(vd, vid, synth_rate=rate, seed=42)
        variants.append({
            "variant_id": vid, "synth_rate": rate, "seed": 42,
            "exit_code": 0, "wall_seconds": 90.0,
            "ticks": 200, "trades": 10, "wins": 5,
            "equity_delta_usd": 20.0,
        })

    rollup = {
        "run_id": run_id, "day": "1970-01-02", "symbol": "BTCUSDT",
        "start_utc": "1970-01-02T00:00:00Z", "end_utc": "1970-01-02T00:03:00Z",
        "wall_seconds": 90.5,
        "max_concurrent": 2,
        "variants": variants,
    }
    (run_dir / "multi_manifest.json").write_bytes(
        serde.dumps(rollup)
    )
    return run_dir


def test_load_run_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = _write_run(Path(tmp))
        metrics = load_run_metrics(run_dir)

        assert metrics.run_id == "test_phase4_run"
        assert metrics.day == "1970-01-02"
        assert metrics.is_multi
        assert len(metrics.variants) == 2

        for vm in metrics.variants:
            assert vm.ticks == 200
            assert vm.trades == 10
            assert vm.wins == 5
            assert len(vm.equity_curve) > 0
            assert len(vm.price_curve) > 0
            assert "hold" in vm.action_counts or "long" in vm.action_counts
            assert len(vm.regime_counts) > 0
            assert len(vm.tribe_net) == len(TRIBES)

    print(f"  ok  metrics loaded: {len(metrics.variants)} variants, "
          f"{metrics.variants[0].ticks} ticks, "
          f"{len(metrics.variants[0].tribe_net)} tribes")


def test_render_html() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = _write_run(Path(tmp))
        metrics = load_run_metrics(run_dir)
        html = render_html(metrics)

    assert "<!DOCTYPE html>" in html
    assert "PAM Replay" in html
    assert "test_phase4_run" in html
    assert "<svg" in html
    assert "Variant comparison" in html  # multi-variant section present
    assert "Equity curves" in html
    assert "synth_6" in html
    assert "synth_60" in html
    print(f"  ok  HTML rendered ({len(html):,} chars, contains SVG and comparison table)")


def test_render_md() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = _write_run(Path(tmp))
        metrics = load_run_metrics(run_dir)
        md = render_md(metrics)

    assert "# PAM Replay Report" in md
    assert "test_phase4_run" in md
    assert "synth_6" in md
    assert "Variant comparison" in md
    assert "win rate" in md.lower() or "win_rate" in md or "Win" in md
    print(f"  ok  Markdown rendered ({len(md):,} chars)")


def test_build_zip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        run_dir = _write_run(tmp_p)
        zip_path = build_zip(run_dir)

        assert zip_path.exists()
        size_kb = zip_path.stat().st_size / 1024
        assert size_kb > 1, "zip suspiciously small"

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            html_entry = next((n for n in names if n.endswith("report.html")), None)
            assert html_entry, f"no report.html in {names}"
            html_content = zf.read(html_entry).decode("utf-8")

        assert any(n.endswith("report.md") for n in names), f"no report.md in {names}"
        assert any("multi_manifest.json" in n for n in names)
        assert any("decisions.jsonl" in n for n in names)
        assert any("manifest.json" in n for n in names)
        assert "<!DOCTYPE html>" in html_content

    print(f"  ok  zip built ({size_kb:.0f} KB), {len(names)} entries, "
          f"report.html + report.md + artifacts present")


def test_single_variant_run() -> None:
    """Report handles bare single-variant layout (no multi_manifest.json)."""
    with tempfile.TemporaryDirectory() as tmp:
        run_id = "single_test"
        run_dir = Path(tmp) / run_id
        run_dir.mkdir()
        _write_variant(run_dir / "variants", "synth_60", synth_rate=60, seed=42)

        metrics = load_run_metrics(run_dir)
        assert len(metrics.variants) == 1
        assert not metrics.is_multi
        html = render_html(metrics)
        assert "Variant comparison" not in html  # single → no comparison table
        zip_path = build_zip(run_dir)
        assert zip_path.exists()

    print(f"  ok  single-variant run: no comparison table, zip built")


def main() -> int:
    tests = [
        test_load_run_metrics,
        test_render_html,
        test_render_md,
        test_build_zip,
        test_single_variant_run,
    ]
    print(f"running {len(tests)} phase-4 tests...")
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
    print("all phase-4 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
