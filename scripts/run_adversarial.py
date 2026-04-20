"""Run adversarial replay fixtures end-to-end.

  python -m scripts.run_adversarial --day 2026-04-15
  python -m scripts.run_adversarial --day 2026-04-15 --apply-prior

The base day must already be cached by `scripts.prefetch_klines` (or any
prior replay run that touched the day). The script anchors every spoof
fixture at `--anchor-offset-s` seconds into the day, runs each fixture
through a fresh SprintEngine, and writes:

  data/replays/<run_id>/adversarial_report.json
  data/replays/<run_id>/adversarial_report.md
  data/replays/<run_id>/fixtures/<fixture_id>/decisions.jsonl
  data/replays/<run_id>/fixtures/<fixture_id>/ledger/

Pass `--apply-prior` to fold the resulting per-tribe skepticism scores
into the persistent calibrator state at
`data/calibration/tribe_skill.json` — chronically-fooled tribes get a
starting EWMA below 0.5 (a malus), consistently-skeptical ones above
(a boost). Use `--apply-prior-overwrite` to also reset tribes that
already have live samples.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date as Date
from pathlib import Path

from src.audit.calibrator import TribeCalibrator
from src.replay.adversarial import run_adversarial


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=Date.fromisoformat,
                    help="UTC day to use as the carrier replay")
    ap.add_argument("--seed", default=42, type=int)
    ap.add_argument("--synth-rate", default=60, type=int)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--anchor-offset-s", default=6 * 3600, type=int,
                    help="seconds from start-of-day at which to inject each spoof "
                         "(default 21600 = 6h)")
    ap.add_argument("--tribes-dir", default=None, type=Path,
                    help="optional config/tribes_variants/<gen>/<variant> dir")
    ap.add_argument("--fixtures-dir", default=None, type=Path,
                    help="alternate fixtures dir (default: tests/fixtures/spoofs)")
    ap.add_argument("--warmup-min", default=30, type=int,
                    help="minutes of bars before the anchor to feed for warmup")
    ap.add_argument("--cooldown-min", default=5, type=int,
                    help="minutes of bars past the scoring window to allow exits")
    ap.add_argument("--apply-prior", action="store_true",
                    help="write the resulting skepticism scores into the calibrator")
    ap.add_argument("--apply-prior-overwrite", action="store_true",
                    help="also reset tribes that already have live samples")
    args = ap.parse_args()

    report = asyncio.run(run_adversarial(
        day=args.day, seed=args.seed,
        synth_rate=args.synth_rate, symbol=args.symbol,
        anchor_offset_s=args.anchor_offset_s,
        tribes_dir=args.tribes_dir,
        fixtures_dir=args.fixtures_dir,
        warmup_min=args.warmup_min,
        cooldown_min=args.cooldown_min,
    ))

    print()
    print(f"  Adversarial run: {report.run_id}")
    print(f"  Day: {report.day}  -  Symbol: {report.symbol}")
    print(f"  Anchor offset: {report.anchor_offset_s}s into day")
    print(f"  Fixtures: {len(report.fixtures)}")
    print()
    print(f"  {'fixture':<40} {'bait':<5} {'council':>10} {'tribes':>10}")
    print(f"  {'-'*40} {'-'*5} {'-'*10} {'-'*10}")
    for fr in report.fixtures:
        n_skep = sum(1 for t in fr.tribes if t.skeptical)
        n_total = len(fr.tribes)
        marker = "OK " if fr.council_skeptical else "ERR"
        print(f"  {fr.fixture_id:<40} {fr.bait_side:<5} "
              f"{marker} {fr.council_signed:+8.2f}  {n_skep:>3}/{n_total:<3}")
    print()
    print("  Per-tribe skepticism (fraction of fixtures where the tribe")
    print("  voted AGAINST the bait - 1.0 = always skeptical):")
    print()
    for tid, s in sorted(report.skepticism_by_tribe.items(),
                         key=lambda x: -x[1]):
        bar = "#" * int(round(s * 20))
        print(f"    {tid:<28} {s:5.2f}  {bar}")

    if args.apply_prior:
        cal = TribeCalibrator()
        n = cal.apply_skepticism_prior(
            report.skepticism_by_tribe,
            overwrite_existing=args.apply_prior_overwrite,
        )
        print()
        print(f"  Wrote skepticism prior for {n} tribes into the calibrator.")


if __name__ == "__main__":
    main()
