"""WS3 H2 Step 4 — live-feedback attribution driver.

Runs a full-day sprint on each panel day (2026-04-18 .. 2026-04-21)
with the KernelCalibrator wired into SprintEngine (committed in feat
686164b). Each sprint writes `<run_dir>/forecasts/<day>.jsonl` with
resolved-anchor records where the projector's
`bps_per_unit_pressure` was being updated tick-by-tick from the
calibrator's correct-direction median. Those anchors are aggregated
under a single root and fed to `ws3_h2_option1a_snapshot.py --tag
live-feedback` so the resulting markdown sits alongside the
pre-wiring offline snapshot in `_ws3_h2_snapshots/` for diff.

The pre-wiring snapshot (option1a_20260422T160057Z.md) read anchors
from the LIVE production path — `data/forecasts/<day>.jsonl` — where
the calibrator WAS active but the projector consumer wasn't getting
the gain update. That discrepancy is what the reviewer called the
"determinism-PASS false-positive" — the attribution simulation could
replay the correction but it had to start from anchors produced under
an un-updated gain.

With this driver:
  - The sprint produces anchors where `expected_move_bps` reflects
    the evolving gain each tick.
  - The snapshot script then simulates the calibrator ON TOP OF
    those already-evolved anchors. The resulting `correct-dir corr`
    median is the *post-convergence* correction — it should land
    much closer to 1.0 than the 1.5-2.5 we saw in the offline
    snapshot, because the gain has already been walked toward its
    fixed point during the sprint itself.

Exit 0 on success. Panel days with missing resolved anchors (e.g.
04-21 shortened tape from the known divergence) are skipped with a
warning, not errored — the reviewer keeps 04-21 as diagnostic, not
acceptance.

Usage:
    PYTHONPATH=. python -m scripts.ws3_h2_step4_live_attribution
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from loguru import logger

# Silence the INFO firehose from the sprint engine; keep WARN+.
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.core import serde  # noqa: E402
from src.core.config import PROJECT_ROOT, settings  # noqa: E402
from src.replay.harness import run_sprint  # noqa: E402
from src.replay.kline_client import CachedHistoricalClient  # noqa: E402


PANEL: list[date] = [
    date(2026, 4, 18),
    date(2026, 4, 19),
    date(2026, 4, 20),
    date(2026, 4, 21),
]


async def _prefetch_day(day: date, symbol: str) -> tuple[list[dict], list[dict]]:
    fdir = PROJECT_ROOT / "data" / "fixtures" / f"{symbol}_{day.isoformat()}"
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


async def _sprint_day(
    day: date, symbol: str, outputs_root: Path,
) -> Path:
    bars, funding = await _prefetch_day(day, symbol)
    result = await run_sprint(
        day=day,
        synth_rate=60,
        seed=42,
        run_id=f"ws3h2_step4_{day.isoformat()}",
        variant_id="live_feedback",
        outputs_root=outputs_root,
        symbol=symbol,
        bars=bars,
        funding=funding,
        max_ticks=None,
    )
    return Path(result.decisions_path).parent


def main() -> int:
    symbol = settings().get("symbol") or "BTCUSDT"

    # Persistent aggregation root — the final snapshot points here via
    # --forecasts-root, and the per-day resolved-anchor files stay on
    # disk for spot-inspection alongside the snapshot markdown.
    agg_root = (
        PROJECT_ROOT
        / "research"
        / "phase11_diagnostic"
        / "_ws3_h2_step4_forecasts"
    )
    if agg_root.exists():
        shutil.rmtree(agg_root)
    agg_root.mkdir(parents=True)

    successful_days: list[date] = []

    with tempfile.TemporaryDirectory(prefix="ws3h2_step4_") as tmp:
        outputs = Path(tmp)
        for day in PANEL:
            print(f"[step4] sprinting {day.isoformat()} ...", flush=True)
            run_dir = asyncio.run(_sprint_day(day, symbol, outputs))
            src_anchors = run_dir / "forecasts" / f"{day.isoformat()}.jsonl"
            if not src_anchors.exists():
                # Happens when a day has no resolved anchors (04-21's
                # divergence, possibly). Reviewer accepts skipping.
                print(
                    f"[step4]   WARN missing {src_anchors} — skipping",
                    flush=True,
                )
                continue
            shutil.copy(src_anchors, agg_root / f"{day.isoformat()}.jsonl")
            gain_path = run_dir / "calibration" / "projector_gain.json"
            if gain_path.exists():
                shutil.copy(
                    gain_path, agg_root / f"{day.isoformat()}_gain.json",
                )
            successful_days.append(day)
            print(
                f"[step4]   anchors -> "
                f"{agg_root / (day.isoformat() + '.jsonl')}",
                flush=True,
            )

    if not successful_days:
        print("[step4] FAIL no panel days produced resolved anchors", flush=True)
        return 1

    print(
        f"[step4] running attribution snapshot on {len(successful_days)} "
        f"live-feedback days (forecasts_root={agg_root}) ...",
        flush=True,
    )
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ws3_h2_option1a_snapshot",
            "--days",
            *[d.isoformat() for d in successful_days],
            "--forecasts-root",
            str(agg_root),
            "--tag",
            "live-feedback",
        ],
        cwd=str(PROJECT_ROOT),
    )
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
