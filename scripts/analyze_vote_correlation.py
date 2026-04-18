"""Tribe-vote correlation diagnostic.

Streams a day's tick ledger and prints a pairwise Pearson correlation
matrix of tribe vote signals. The signed vote vector per tick is:

    vote[tribe] = +conviction   if side == buy
                  -conviction   if side == sell
                   0.0          if abstain

High positive correlation (|r| > 0.7) between two tribes means they
are saying the same thing most of the time — the council is paying
its diversity budget twice. Either their weights need to shrink, or
one should be redesigned to key off a different signal.

High negative correlation (r < -0.5) means two tribes systematically
disagree — that's fine (it's the council's job to let views cancel)
but worth knowing so their weights can be set with awareness.

Output:
  - A NxN table with row/column tribe ids and Pearson r values.
  - A ranked list of the top 10 |r| pairs (anti or pro), with a
    flag on pairs above the redundancy threshold.
  - Per-tribe activity rate (fraction of ticks the tribe was not
    abstaining) — a tribe that abstains 99% of the time can't be
    meaningfully correlated with anything.

Usage:
    python -m scripts.analyze_vote_correlation                # today UTC
    python -m scripts.analyze_vote_correlation 2026-04-18     # explicit day
    python -m scripts.analyze_vote_correlation --threshold 0.6
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import orjson

from src.core.config import PROJECT_ROOT


LEDGER_DIR = PROJECT_ROOT / "data" / "ledger"
DEFAULT_REDUNDANCY = 0.70


def _iter_ticks(path: Path):
    """Stream tick events from a daily ledger. Non-tick rows skipped."""
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = orjson.loads(line)
            except Exception:
                continue
            if rec.get("event") != "tick":
                continue
            yield rec


def _signed_vote(v: dict) -> float:
    """Convert a recorded vote into a signed conviction value."""
    side = str(v.get("side", "abstain")).lower()
    conv = float(v.get("conviction") or 0.0)
    if side == "buy":
        return conv
    if side == "sell":
        return -conv
    return 0.0


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Plain Pearson. Returns 0.0 if either series is flat (zero variance)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return 0.0
    return num / (dx * dy)


def _format_matrix(tribes: list[str], corr: list[list[float]]) -> str:
    """Print-friendly NxN matrix with short-form tribe labels as headers."""
    short = {t: t[:10] for t in tribes}
    head = " " * 12 + "".join(f"{short[t]:>11}" for t in tribes)
    rows = [head]
    for i, ti in enumerate(tribes):
        cells = "".join(f"{corr[i][j]:>+11.2f}" for j in range(len(tribes)))
        rows.append(f"{short[ti]:<12}{cells}")
    return "\n".join(rows)


def analyze(path: Path, redundancy_threshold: float = DEFAULT_REDUNDANCY) -> None:
    ticks = list(_iter_ticks(path))
    if not ticks:
        print(f"[corr] no ticks in {path} — nothing to analyze")
        return
    # Discover the stable tribe order from the first record; assume fixed
    # across the session (it is; ledger writes every vote every tick).
    first_votes = ticks[0].get("votes") or []
    tribe_ids = [v["tribe_id"] for v in first_votes]
    n_tribes = len(tribe_ids)
    if n_tribes < 2:
        print("[corr] fewer than 2 tribes in ledger — nothing to compare")
        return

    # Per-tribe time series of signed vote.
    series: dict[str, list[float]] = {t: [] for t in tribe_ids}
    activity_count = {t: 0 for t in tribe_ids}
    for row in ticks:
        for v in row.get("votes") or ():
            tid = v.get("tribe_id")
            if tid not in series:
                continue
            x = _signed_vote(v)
            series[tid].append(x)
            if x != 0.0:
                activity_count[tid] += 1

    # Trim to the shortest series so column indices align even if a later
    # tick dropped a tribe (shouldn't happen, but be defensive).
    min_len = min(len(series[t]) for t in tribe_ids)
    for t in tribe_ids:
        series[t] = series[t][:min_len]

    # Correlation matrix.
    corr = [[0.0] * n_tribes for _ in range(n_tribes)]
    for i, ti in enumerate(tribe_ids):
        for j, tj in enumerate(tribe_ids):
            if i == j:
                corr[i][j] = 1.0
            elif j < i:
                corr[i][j] = corr[j][i]
            else:
                corr[i][j] = _pearson(series[ti], series[tj])

    # Top pairs by |r|.
    pairs: list[tuple[float, str, str]] = []
    for i in range(n_tribes):
        for j in range(i + 1, n_tribes):
            pairs.append((corr[i][j], tribe_ids[i], tribe_ids[j]))
    pairs.sort(key=lambda p: -abs(p[0]))
    top = pairs[:10]

    # Report.
    print(f"\n[corr] ledger={path.name}  ticks={min_len}  tribes={n_tribes}")
    print(f"[corr] redundancy threshold = |r| >= {redundancy_threshold:.2f}\n")

    print("-- activity rate (fraction of ticks with a non-abstain vote) --")
    for t in sorted(tribe_ids):
        rate = activity_count[t] / max(1, min_len)
        flag = "" if rate >= 0.05 else "  ← rarely votes, correlations may be noisy"
        print(f"  {t:<36}  {rate*100:>5.1f}%{flag}")

    print("\n-- pairwise Pearson correlation --")
    print(_format_matrix(tribe_ids, corr))

    print("\n-- top 10 |r| pairs --")
    for r, a, b in top:
        flag = "REDUNDANT" if abs(r) >= redundancy_threshold else ""
        print(f"  r={r:>+.2f}  {a:<30}  {b:<30}  {flag}")

    redundant = [(r, a, b) for r, a, b in pairs if r >= redundancy_threshold]
    anticorr = [(r, a, b) for r, a, b in pairs if r <= -redundancy_threshold]
    print()
    if redundant:
        print(f"[corr] {len(redundant)} pair(s) above redundancy threshold - "
              "consider shrinking the weight on one side of each pair or "
              "rewiring one tribe to a different input signal.")
    else:
        print("[corr] no redundant pairs - council diversity is holding up.")
    if anticorr:
        print(f"[corr] {len(anticorr)} strong anti-correlated pair(s) - that's "
              "normal (council is doing its job) but useful to know for weighting.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tribe vote correlation diagnostic")
    parser.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD (UTC). Default = today.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_REDUNDANCY,
                        help="|r| above which a pair is flagged REDUNDANT.")
    parser.add_argument("--path", default=None,
                        help="Override — explicit path to a ledger .jsonl file.")
    args = parser.parse_args()

    if args.path:
        path = Path(args.path)
    else:
        day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = LEDGER_DIR / f"{day}.jsonl"
    analyze(path, redundancy_threshold=args.threshold)


if __name__ == "__main__":
    main()
