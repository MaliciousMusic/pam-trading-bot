"""Phase G3 validator -- does `synth_book` wake the replay-mute tribes?

Runs two short sprints against the same day/seed: one with the legacy
trade-only pipeline, one with the proxy book stream on. Reports per-tribe
activity rate from each decisions.jsonl and highlights which tribes
cross from 0% into voting territory. This is the G3 exit criterion:
microstructure_prober must go from 0% activity to >= some non-trivial
rate when depth proxy events are emitted.

Usage:
    python -m scripts.verify_replay_fidelity --day 2026-04-20
    python -m scripts.verify_replay_fidelity --day 2026-04-20 --max-ticks 500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from loguru import logger

from src.core import serde
from src.core.config import PROJECT_ROOT, settings
from src.replay.harness import run_sprint
from src.replay.kline_client import CachedHistoricalClient


FOCUS_TRIBES = ("microstructure_prober", "put_call_fader", "options_dealer_gamma")


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _activity_rate_per_tribe(decisions_path: Path) -> tuple[dict[str, float], int]:
    """Stream decisions.jsonl and count non-abstain votes per tribe."""
    votes_nonzero: Counter[str] = Counter()
    ticks = 0
    with decisions_path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = serde.loads(line)
            if rec.get("tick") is None or rec.get("votes") is None:
                # DecisionLogWriter also emits a run manifest / summary row;
                # those lack `tick`. Skip silently.
                continue
            ticks += 1
            for v in rec["votes"]:
                side = str(v.get("side", "abstain")).lower()
                if side in ("buy", "sell"):
                    votes_nonzero[v["tribe_id"]] += 1
    if ticks == 0:
        return {}, 0
    return {tid: n / ticks for tid, n in votes_nonzero.items()}, ticks


async def _fetch_inputs(day: date, symbol: str) -> tuple[list[dict], list[dict]]:
    fdir = PROJECT_ROOT / "data" / "fixtures" / f"{symbol}_{day.isoformat()}"
    if (fdir / "bars.json").exists() and (fdir / "funding.json").exists():
        bars = serde.loads((fdir / "bars.json").read_bytes())
        funding = serde.loads((fdir / "funding.json").read_bytes())
        return bars, funding
    client = CachedHistoricalClient(cache_root=PROJECT_ROOT / "data", symbol=symbol)
    bars = await client.get_day_klines(day)
    funding = await client.get_day_funding(day)
    return bars, funding


async def _run_once(
    *, day: date, seed: int, synth_rate: int, synth_book: bool, max_ticks: int | None,
    outputs: Path, tag: str, bars: list[dict], funding: list[dict], symbol: str,
) -> Path:
    result = await run_sprint(
        day=day, synth_rate=synth_rate, seed=seed,
        run_id=tag, variant_id="fidelity_check",
        outputs_root=outputs, symbol=symbol,
        bars=bars, funding=funding,
        max_ticks=max_ticks,
        synth_book=synth_book,
    )
    return Path(result.decisions_path)


def _diff_table(
    baseline: dict[str, float], fidelity: dict[str, float], ticks_b: int, ticks_f: int,
) -> None:
    print(f"\n  {'tribe':<36}{'baseline':>12}{'fidelity':>12}   verdict")
    print("  " + "-" * 74)
    all_tribes = sorted(set(baseline) | set(fidelity))
    for t in all_tribes:
        b = baseline.get(t, 0.0)
        f = fidelity.get(t, 0.0)
        flag = ""
        if t in FOCUS_TRIBES:
            if b < 0.01 and f >= 0.10:
                flag = "  <- WOKE UP"
            elif b < 0.01 and f < 0.01:
                flag = "  (still mute)"
            elif b >= 0.01 and f > b * 1.5:
                flag = "  (activity up)"
            elif b >= 0.01 and f < b * 0.5:
                flag = "  (activity down)"
            else:
                flag = "  (focus tribe)"
        print(f"  {t:<36}{b*100:>11.1f}%{f*100:>11.1f}%{flag}")
    print(f"\n  ticks: baseline={ticks_b}  fidelity={ticks_f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day)
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-ticks", type=int, default=500,
                    help="Cap each run (fast lane for a sanity check; "
                         "full-day is what final calibration uses).")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    symbol = (settings().get("symbol") or "BTCUSDT")
    bars, funding = asyncio.run(_fetch_inputs(args.day, symbol))
    print(f"[fid] loaded {len(bars)} bars, {len(funding)} funding events")

    with tempfile.TemporaryDirectory(prefix="fid_") as tmp:
        outputs = Path(tmp)

        print("[fid] run BASELINE (synth_book=False) ...")
        path_b = asyncio.run(_run_once(
            day=args.day, seed=args.seed, synth_rate=args.synth_rate,
            synth_book=False, max_ticks=args.max_ticks,
            outputs=outputs, tag="baseline",
            bars=bars, funding=funding, symbol=symbol,
        ))
        rate_b, ticks_b = _activity_rate_per_tribe(path_b)
        print(f"[fid]   baseline: {ticks_b} ticks logged")

        print("[fid] run FIDELITY (synth_book=True) ...")
        path_f = asyncio.run(_run_once(
            day=args.day, seed=args.seed, synth_rate=args.synth_rate,
            synth_book=True, max_ticks=args.max_ticks,
            outputs=outputs, tag="fidelity",
            bars=bars, funding=funding, symbol=symbol,
        ))
        rate_f, ticks_f = _activity_rate_per_tribe(path_f)
        print(f"[fid]   fidelity: {ticks_f} ticks logged")

        _diff_table(rate_b, rate_f, ticks_b, ticks_f)

        # G3 exit criterion: microstructure_prober moves from <1% to >=10%.
        micro_b = rate_b.get("microstructure_prober", 0.0)
        micro_f = rate_f.get("microstructure_prober", 0.0)
        print()
        if micro_b < 0.01 and micro_f >= 0.10:
            print(f"[fid] G3 EXIT MET: microstructure_prober "
                  f"{micro_b*100:.1f}% -> {micro_f*100:.1f}%")
            return 0
        else:
            print(f"[fid] G3 EXIT NOT MET: microstructure_prober "
                  f"{micro_b*100:.1f}% -> {micro_f*100:.1f}%")
            print("       (threshold: baseline <1% AND fidelity >=10%)")
            return 1


if __name__ == "__main__":
    sys.exit(main())
