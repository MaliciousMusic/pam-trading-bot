"""WS2.5 — cross-seed metrics rollup for one variant.

Reads per-seed replay outputs under `data/replays/ws2_5_<variant>_seed<N>/`
and prints a table with one row per seed plus a mean/std rollup.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, pstdev

from scripts.ws2_5_analyze import analyze_variant, emit_table
from src.core.config import PROJECT_ROOT


DEFAULT_SEEDS = [42, 123, 777, 2024]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--seeds", type=int, nargs="*", default=DEFAULT_SEEDS)
    ap.add_argument("--outputs-root", type=Path,
                    default=PROJECT_ROOT / "data" / "replays")
    ap.add_argument("--report-dir", type=Path,
                    default=PROJECT_ROOT / "research" / "phase11_diagnostic" / "_ws2_5_tables")
    args = ap.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for seed in args.seeds:
        var_dir = (
            args.outputs_root / f"ws2_5_{args.variant}_seed{seed}"
            / "variants" / args.variant
        )
        if not var_dir.exists():
            print(f"[warn] missing: {var_dir}")
            continue
        m = analyze_variant(var_dir)
        m["variant_id"] = f"{args.variant}@seed{seed}"
        m["seed"] = seed
        rows.append(m)

    agg_keys = [
        "trades", "win_rate", "mean_realized_bps", "sharpe_per_trade",
        "max_drawdown_pct", "equity_delta_usd", "equity_delta_pct",
    ]
    summary: dict[str, dict] = {}
    for k in agg_keys:
        vals = [float(r[k]) for r in rows]
        summary[k] = {
            "mean": mean(vals) if vals else 0.0,
            "std": pstdev(vals) if len(vals) >= 2 else 0.0,
            "min": min(vals) if vals else 0.0,
            "max": max(vals) if vals else 0.0,
        }

    table = emit_table(rows)
    print(f"\n{args.variant} across seeds {args.seeds}")
    print(table)
    print("\nrollup (mean +- std across seeds):")
    for k, v in summary.items():
        print(f"  {k:24s}  mean={v['mean']:+9.3f}  std={v['std']:7.3f}  "
              f"min={v['min']:+9.3f}  max={v['max']:+9.3f}")

    out_path = args.report_dir / f"cross_seed_{args.variant}.md"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"variant: {args.variant}  seeds: {args.seeds}\n\n")
        f.write(table + "\n\n")
        f.write("rollup (mean +- std across seeds):\n\n")
        f.write("| metric | mean | std | min | max |\n")
        f.write("|---|---:|---:|---:|---:|\n")
        for k, v in summary.items():
            f.write(f"| {k} | {v['mean']:+.3f} | {v['std']:.3f} | "
                    f"{v['min']:+.3f} | {v['max']:+.3f} |\n")
    json_path = args.report_dir / f"cross_seed_{args.variant}.json"
    json_path.write_text(json.dumps({"rows": rows, "rollup": summary}, indent=2))
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
