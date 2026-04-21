"""Phase 2 — semantic-invariant capture + diff.

For NumPy-vectorization commits we accept distribution-invariant changes
(a new baseline SHA is fine) but we reject semantic drift. This tool
defines and enforces the "semantic invariance" contract:

Semantic invariants, in order of strictness:
  1. tick count                         — must match exactly
  2. trade count (opens)                — must match exactly
  3. per-tribe BUY count                — within tolerance (default 0 — exact)
  4. per-tribe SELL count               — within tolerance (default 0 — exact)
  5. final equity USD                   — within tolerance (default 1 bps)

Rationale: Target 1 (RollingSeries NumPy backend) does the same float math
in a different order — but ordered reductions (sum, min, max) are order-
independent, so we expect bit-exact drift. Target 2 (realized_vol_bps via
numpy) uses numpy.std which can differ from a manual two-pass variance by
~1e-15 per tick; the tolerance absorbs that. If a commit violates any
invariant above, it needs an explicit waiver.

Usage:
    # Capture a baseline snapshot
    python -m scripts.bench_semantic_invariants --day 2026-04-20 \\
        --out docs/perf_baselines/snapshot_pre_numpy.json

    # Re-run after a change and diff against the baseline
    python -m scripts.bench_semantic_invariants --day 2026-04-20 \\
        --baseline docs/perf_baselines/snapshot_pre_numpy.json

    # Store a new snapshot and diff in one pass
    python -m scripts.bench_semantic_invariants --day 2026-04-20 \\
        --baseline docs/perf_baselines/snapshot_pre_numpy.json \\
        --out docs/perf_baselines/snapshot_post_numpy.json

Exit codes:
  0   invariants held (or baseline just captured, no diff)
  1   invariants violated
  2   bad args / run failed
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

from loguru import logger

from src.core import serde
from src.replay.harness import run_sprint


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_decisions(decisions_path: Path) -> dict:
    """Stream decisions.jsonl and compute the per-tribe vote distribution.

    Each non-manifest row has `tick` + `votes`. We count BUY/SELL per tribe;
    abstain/hold is everything else. Return shape is stable so the diff
    logic can treat missing keys as zero.
    """
    buys: Counter[str] = Counter()
    sells: Counter[str] = Counter()
    holds: Counter[str] = Counter()
    ticks = 0
    with decisions_path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = serde.loads(line)
            if rec.get("tick") is None or rec.get("votes") is None:
                continue  # manifest / summary row
            ticks += 1
            for v in rec["votes"]:
                side = str(v.get("side", "abstain")).lower()
                tid = v["tribe_id"]
                if side == "buy":
                    buys[tid] += 1
                elif side == "sell":
                    sells[tid] += 1
                else:
                    holds[tid] += 1
    return {
        "ticks_logged": ticks,
        "buys_by_tribe": dict(buys),
        "sells_by_tribe": dict(sells),
        "holds_by_tribe": dict(holds),
    }


def run_one_sprint(*, day: date, synth_rate: int, seed: int,
                    max_ticks: int | None, symbol: str) -> dict:
    """Run a capped sprint and return a semantic-invariant snapshot."""
    with tempfile.TemporaryDirectory(prefix="bench_") as tmp:
        outputs = Path(tmp)
        t0 = time.perf_counter()
        result = asyncio.run(run_sprint(
            day=day, synth_rate=synth_rate, seed=seed,
            max_ticks=max_ticks,
            outputs_root=outputs, symbol=symbol,
            run_id="bench", variant_id="invariants",
        ))
        wall_s = time.perf_counter() - t0

        dec_path = Path(result.decisions_path)
        parsed = _parse_decisions(dec_path)
        dec_sha = _sha256(dec_path)

    return {
        "params": {
            "day": str(day),
            "synth_rate": synth_rate,
            "seed": seed,
            "max_ticks": max_ticks,
            "symbol": symbol,
        },
        "ticks": result.ticks,
        "events_consumed": result.events_consumed,
        "trades": result.trades,
        "wins": result.wins,
        "start_equity_usd": result.start_equity_usd,
        "end_equity_usd": result.end_equity_usd,
        "equity_delta_usd": result.equity_delta_usd,
        "wall_seconds": wall_s,
        "ticks_logged": parsed["ticks_logged"],
        "buys_by_tribe": parsed["buys_by_tribe"],
        "sells_by_tribe": parsed["sells_by_tribe"],
        "holds_by_tribe": parsed["holds_by_tribe"],
        "decisions_sha256": dec_sha,
    }


def _within_bps(new: float, ref: float, tol_bps: float) -> bool:
    """Accept drift if |new-ref| <= tol_bps * |ref| / 10000 (or
    absolute <= 0.01 when ref is zero)."""
    if ref == 0.0:
        return abs(new) <= 0.01
    return abs(new - ref) / abs(ref) <= tol_bps / 10000.0


def diff(baseline: dict, new: dict, *, equity_tol_bps: float = 1.0,
         vote_tol_pct: float = 0.0) -> tuple[bool, list[str]]:
    """Compare two snapshots. Returns (invariants_held, report_lines)."""
    lines: list[str] = []
    violations: list[str] = []

    # --- top-line ---
    lines.append("")
    lines.append("=" * 78)
    lines.append("  SEMANTIC-INVARIANT DIFF")
    lines.append("=" * 78)
    lines.append(f"  params  : {baseline['params']} vs {new['params']}")
    lines.append("")
    lines.append(f"  {'metric':<28}{'baseline':>18}{'new':>18}{'delta':>14}")
    lines.append("  " + "-" * 76)

    def _row(name, b, n, fmt="{:>18.4f}"):
        delta = n - b if isinstance(b, (int, float)) else ""
        fb = fmt.format(b) if isinstance(b, (int, float)) else str(b)
        fn = fmt.format(n) if isinstance(n, (int, float)) else str(n)
        fd = f"{delta:+.4f}" if isinstance(delta, (int, float)) else delta
        lines.append(f"  {name:<28}{fb:>18}{fn:>18}{fd:>14}")

    _row("ticks", baseline["ticks"], new["ticks"], "{:>18d}")
    if baseline["ticks"] != new["ticks"]:
        violations.append(
            f"tick count changed: {baseline['ticks']} -> {new['ticks']}"
        )

    _row("trades", baseline["trades"], new["trades"], "{:>18d}")
    if baseline["trades"] != new["trades"]:
        violations.append(
            f"trade count changed: {baseline['trades']} -> {new['trades']}"
        )

    _row("wins", baseline["wins"], new["wins"], "{:>18d}")

    _row("events_consumed",
         baseline["events_consumed"], new["events_consumed"], "{:>18d}")

    _row("start_equity_usd",
         baseline["start_equity_usd"], new["start_equity_usd"])
    _row("end_equity_usd",
         baseline["end_equity_usd"], new["end_equity_usd"])
    _row("equity_delta_usd",
         baseline["equity_delta_usd"], new["equity_delta_usd"])

    if not _within_bps(new["end_equity_usd"], baseline["end_equity_usd"],
                       equity_tol_bps):
        violations.append(
            f"end_equity drift > {equity_tol_bps} bps: "
            f"{baseline['end_equity_usd']:.4f} -> {new['end_equity_usd']:.4f}"
        )

    _row("wall_seconds",
         baseline["wall_seconds"], new["wall_seconds"], "{:>18.2f}")
    speedup = baseline["wall_seconds"] / max(new["wall_seconds"], 1e-6)
    lines.append(f"  {'speedup (x)':<28}{'':>18}{speedup:>18.2f}{'':>14}")

    # --- SHA ---
    lines.append("")
    lines.append(f"  decisions.jsonl SHA256:")
    lines.append(f"    baseline: {baseline['decisions_sha256']}")
    lines.append(f"    new     : {new['decisions_sha256']}")
    if baseline["decisions_sha256"] == new["decisions_sha256"]:
        lines.append("    -> bit-identical")
    else:
        lines.append("    -> different (accepted if semantic invariants hold)")

    # --- per-tribe vote counts ---
    lines.append("")
    lines.append(f"  {'tribe':<32}{'BUY b/n':>16}{'SELL b/n':>16}"
                 f"{'drift':>10}")
    lines.append("  " + "-" * 74)
    all_tribes = sorted(
        set(baseline["buys_by_tribe"]) | set(new["buys_by_tribe"])
        | set(baseline["sells_by_tribe"]) | set(new["sells_by_tribe"])
    )
    worst_drift = 0
    for t in all_tribes:
        bb = baseline["buys_by_tribe"].get(t, 0)
        nb = new["buys_by_tribe"].get(t, 0)
        bs = baseline["sells_by_tribe"].get(t, 0)
        ns = new["sells_by_tribe"].get(t, 0)
        drift_b = nb - bb
        drift_s = ns - bs
        drift = max(abs(drift_b), abs(drift_s))
        worst_drift = max(worst_drift, drift)
        flag = ""
        if vote_tol_pct <= 0.0:
            if bb != nb or bs != ns:
                flag = "  <- DRIFT"
                violations.append(
                    f"{t}: BUY {bb}->{nb} SELL {bs}->{ns}"
                )
        else:
            max_ref = max(bb, bs, 1)
            if drift / max_ref > vote_tol_pct / 100.0:
                flag = "  <- DRIFT"
                violations.append(
                    f"{t}: BUY {bb}->{nb} SELL {bs}->{ns} "
                    f"(>{vote_tol_pct:.1f}%)"
                )
        lines.append(
            f"  {t:<32}{bb:>7} / {nb:<6}{bs:>7} / {ns:<6}"
            f"{drift:>10}{flag}"
        )
    lines.append(f"  worst per-tribe drift: {worst_drift} votes")

    # --- verdict ---
    lines.append("")
    if violations:
        lines.append(f"  VERDICT: INVARIANTS VIOLATED ({len(violations)} issues)")
        lines.append("=" * 78)
        for v in violations:
            lines.append(f"  - {v}")
    else:
        lines.append("  VERDICT: INVARIANTS HELD")
        lines.append("=" * 78)

    return (not violations), lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, type=_parse_day)
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-ticks", type=int, default=500)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--out", type=Path, default=None,
                    help="Write the snapshot JSON here.")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="Compare against a stored baseline snapshot.")
    ap.add_argument("--equity-tol-bps", type=float, default=1.0,
                    help="End-equity drift tolerance in basis points "
                         "(default 1.0 = 0.01 percent).")
    ap.add_argument("--vote-tol-pct", type=float, default=0.0,
                    help="Per-tribe vote-count drift tolerance in percent. "
                         "0 = exact match (default). Only raise this if "
                         "you have documented evidence float ordering "
                         "explains the drift.")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    print(f"[bench] running sprint: day={args.day} "
          f"max_ticks={args.max_ticks} synth_rate={args.synth_rate} "
          f"seed={args.seed}")
    snapshot = run_one_sprint(
        day=args.day, synth_rate=args.synth_rate, seed=args.seed,
        max_ticks=args.max_ticks, symbol=args.symbol,
    )
    print(f"[bench] done: {snapshot['ticks']} ticks, "
          f"{snapshot['trades']} trades, "
          f"end_equity=${snapshot['end_equity_usd']:,.2f}, "
          f"wall={snapshot['wall_seconds']:.2f}s, "
          f"sha={snapshot['decisions_sha256'][:12]}...")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(snapshot, indent=2, default=str))
        print(f"[bench] wrote snapshot to {args.out}")

    if args.baseline is None:
        return 0

    baseline = json.loads(args.baseline.read_text())
    held, lines = diff(baseline, snapshot,
                        equity_tol_bps=args.equity_tol_bps,
                        vote_tol_pct=args.vote_tol_pct)
    print("\n".join(lines))
    return 0 if held else 1


if __name__ == "__main__":
    sys.exit(main())
