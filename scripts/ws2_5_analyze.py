"""WS2.5 — analyze variant replay outputs and emit metrics table.

For each variant under `data/replays/ws2_5_<variant_id>_seed<seed>/`:
  - parse ledger `close` events → realized_bps list, equity curve
  - parse decisions.jsonl → tick count, hold-reason distribution,
    regime-time distribution
  - compute: trade count, win-rate, mean/median realized_bps, Sharpe,
    max drawdown, final equity delta

Writes a markdown table + per-variant JSON summary to
`research/phase11_diagnostic/_ws2_5_tables/`.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median, pstdev

from src.core.config import PROJECT_ROOT


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _load_closes(ledger_dir: Path) -> list[dict]:
    closes: list[dict] = []
    if not ledger_dir.exists():
        return closes
    for ledger_file in sorted(ledger_dir.glob("*.jsonl")):
        for rec in _iter_jsonl(ledger_file):
            if rec.get("event") == "close":
                closes.append(rec)
    return closes


def _sharpe_per_trade(xs: list[float]) -> float:
    """Per-trade Sharpe: mean(x) / std(x) * sqrt(N). Uses realized_bps
    as the underlying return. 0 when fewer than 2 trades or zero-variance."""
    if len(xs) < 2:
        return 0.0
    mu = mean(xs)
    sd = pstdev(xs)
    if sd == 0.0:
        return 0.0
    return (mu / sd) * math.sqrt(len(xs))


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    """Max drawdown as a positive percentage from the running peak."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _regime_time_dist(decisions_path: Path) -> dict[str, int]:
    counts: Counter = Counter()
    for rec in _iter_jsonl(decisions_path):
        counts[rec.get("regime") or "none"] += 1
    return dict(counts)


def _hold_reason_top(decisions_path: Path, k: int = 5) -> list[tuple[str, int]]:
    """Top-k action_reason values for rows where action == 'hold'."""
    counts: Counter = Counter()
    for rec in _iter_jsonl(decisions_path):
        if rec.get("action") == "hold":
            reason = rec.get("action_reason", "")
            bucket = reason.split("(")[0].strip() if "(" in reason else reason.split(" ")[0]
            counts[bucket] += 1
    return counts.most_common(k)


def analyze_variant(variant_dir: Path) -> dict:
    """Return a dict of metrics for one variant run directory."""
    decisions_path = variant_dir / "decisions.jsonl"
    ledger_dir = variant_dir / "ledger"
    manifest_path = variant_dir / "manifest.json"
    closes = _load_closes(ledger_dir)
    realized_bps = [float(c.get("realized_bps", 0.0))
                    if "realized_bps" in c
                    else ((float(c.get("net_usd", 0.0)) / float(c.get("size_usd", 1.0))) * 10_000.0
                          if float(c.get("size_usd", 0.0)) > 0 else 0.0)
                    for c in closes]
    wins = sum(1 for r in realized_bps if r > 0)
    equity_curve = [float(c.get("equity_usd", 0.0)) for c in closes]
    # Prefer manifest's start/end equity when available — it reflects the
    # full-run starting capital and the force-closed book at tape end,
    # including any realized-only cases where the first ledger row is a
    # post-entry-fee snapshot.
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            manifest = {}
    start_equity = float(manifest.get(
        "start_equity_usd",
        closes[0].get("equity_usd", 0.0) - closes[0].get("net_usd", 0.0)
        if closes else 5000.0,
    ))
    end_equity = float(manifest.get(
        "end_equity_usd",
        equity_curve[-1] if equity_curve else start_equity,
    ))

    ticks = 0
    for _ in _iter_jsonl(decisions_path):
        ticks += 1

    return {
        "variant_id": variant_dir.name,
        "ticks": ticks,
        "trades": len(closes),
        "wins": wins,
        "win_rate": (wins / len(closes)) if closes else 0.0,
        "mean_realized_bps": mean(realized_bps) if realized_bps else 0.0,
        "median_realized_bps": median(realized_bps) if realized_bps else 0.0,
        "stdev_realized_bps": pstdev(realized_bps) if len(realized_bps) >= 2 else 0.0,
        "sharpe_per_trade": _sharpe_per_trade(realized_bps),
        "max_drawdown_pct": _max_drawdown_pct([start_equity] + equity_curve),
        "start_equity_usd": start_equity,
        "end_equity_usd": end_equity,
        "equity_delta_usd": end_equity - start_equity,
        "equity_delta_pct": (
            0.0 if start_equity == 0
            else (end_equity - start_equity) / start_equity * 100.0
        ),
        "regime_time_ticks": _regime_time_dist(decisions_path),
        "hold_reason_top": _hold_reason_top(decisions_path, k=6),
    }


def _fmt_row(m: dict) -> str:
    return (
        f"| {m['variant_id']:<22} "
        f"| {m['trades']:>5} "
        f"| {m['win_rate']*100:>5.1f}% "
        f"| {m['mean_realized_bps']:+7.2f} "
        f"| {m['median_realized_bps']:+7.2f} "
        f"| {m['sharpe_per_trade']:+6.2f} "
        f"| {m['max_drawdown_pct']:>5.2f}% "
        f"| ${m['equity_delta_usd']:+9.2f} "
        f"| {m['equity_delta_pct']:+6.3f}% |"
    )


def emit_table(metrics: list[dict]) -> str:
    lines = [
        "| variant                | trades | win%  | mean bps| med bps | sharpe | maxDD  | d_equity USD| d_eq %  |",
        "|------------------------|-------:|------:|--------:|--------:|-------:|-------:|------------:|--------:|",
    ]
    lines.extend(_fmt_row(m) for m in metrics)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--variants", nargs="*", default=[
        "v1_baseline", "v2_trend_only", "v3_trend_cascade", "v4_trend_high_conf",
    ])
    ap.add_argument("--outputs-root", type=Path,
                    default=PROJECT_ROOT / "data" / "replays")
    ap.add_argument("--report-dir", type=Path,
                    default=PROJECT_ROOT / "research" / "phase11_diagnostic" / "_ws2_5_tables")
    args = ap.parse_args()

    args.report_dir.mkdir(parents=True, exist_ok=True)

    metrics: list[dict] = []
    for vid in args.variants:
        var_dir = args.outputs_root / f"ws2_5_{vid}_seed{args.seed}" / "variants" / vid
        if not var_dir.exists():
            print(f"[warn] missing variant dir: {var_dir}")
            continue
        m = analyze_variant(var_dir)
        metrics.append(m)
        summary_path = args.report_dir / f"{vid}_seed{args.seed}.json"
        summary_path.write_text(json.dumps(m, indent=2))

    table = emit_table(metrics)
    print(f"\nseed={args.seed}")
    print(table)
    table_path = args.report_dir / f"metrics_seed{args.seed}.md"
    table_path.write_text(f"seed={args.seed}\n\n{table}\n")
    print(f"\n-> {table_path}")


if __name__ == "__main__":
    main()
