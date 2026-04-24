"""WS3 H1 Phase 1 — confidence-formula decomposition analysis.

Reads post-I1 ``data/forecasts/<day>.jsonl`` archives and reports the
per-field distribution summary required by ``WS3_H1_SCOPE.md`` §3.2 to
identify which of the five confidence factors is the binding
constraint on ``final_conf``.

Output dimensions per scope §3.2 (all required, not optional):

  * Per-field distribution: mean + p10/p25/p50/p75/p90/p95/p99.
  * Pairwise sanity cross-checks:
      ``signal_quality * directional_score`` vs ``base_conf``
      ``base_conf * diversity_mult`` vs ``forecast_conf``
    A non-zero mean absolute diff on either line flags a schema drift
    between projector and auditor, not a measurement finding.
  * Horizon stratification (15s / 1m / 5m / 10m). If a horizon is
    missing from the post-I1 archive it is simply absent from the
    report — do not synthesise empty buckets.
  * Trade-firing-subset proxy: distributions on the top-Q subset by
    ``forecast_conf`` (default Q=0.9). The canonical trade-firing
    subset requires a decisions.jsonl join not yet available; this
    proxy answers "which factor binds on the highest-conf ticks?"
    while the join is being built.

Pre-I1 records (any of the five I1 fields is ``None``) are skipped;
their count is reported in the ``scan`` block of the JSON artifact.

Usage::

    PYTHONPATH=. python -m scripts.ws3_h1_decomp_analyze \\
        --days 2026-04-24 2026-04-25 2026-04-26

    # Skip the JSON artifact (stdout-only)
    PYTHONPATH=. python -m scripts.ws3_h1_decomp_analyze \\
        --days 2026-04-24 --out ''

Exit 0 on success, 2 when no post-I1 records exist across all days.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.core.config import PROJECT_ROOT


DEFAULT_FORECASTS_ROOT = PROJECT_ROOT / "data" / "forecasts"
DEFAULT_OUT_DIR = PROJECT_ROOT / "research" / "phase11_diagnostic" / "_ws3_h1_decomp"


DECOMP_FIELDS: tuple[str, ...] = (
    "signal_quality",
    "directional_score",
    "base_conf",
    "diversity_score",
    "diversity_mult",
    "forecast_conf",
)


I1_REQUIRED: tuple[str, ...] = (
    "signal_quality",
    "directional_score",
    "base_conf",
    "diversity_score",
    "diversity_mult",
)


PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90, 95, 99)


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list.

    Matches numpy's default (``method='linear'``). Callers pass ``p`` in
    the 0-100 range. Returns ``nan`` for an empty input.
    """
    n = len(sorted_values)
    if n == 0:
        return float("nan")
    if n == 1:
        return float(sorted_values[0])
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def _distribution(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None,
                "percentiles": {p: None for p in PERCENTILES}}
    sv = sorted(values)
    return {
        "n": n,
        "mean": sum(sv) / n,
        "percentiles": {p: _percentile(sv, p) for p in PERCENTILES},
    }


def _iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _is_post_i1(rec: dict) -> bool:
    """True iff all five I1 decomposition fields are populated."""
    return all(rec.get(k) is not None for k in I1_REQUIRED)


def _bucket_by_label(records: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault(str(r.get("label", "?")), []).append(r)
    return out


def analyze(records: list[dict], *,
            top_conf_quantile: float | None = None) -> dict:
    """Compute the full Phase 1 report from a pre-filtered record list.

    Returns a dict with keys: ``n_total``, ``n_horizons``, ``overall``,
    ``by_horizon``, ``sanity``, ``top_subset``. ``records`` must already
    be post-I1 (use ``_is_post_i1`` to filter upstream).
    """
    out: dict[str, Any] = {
        "n_total": len(records),
        "by_horizon": {},
    }

    out["overall"] = {
        f: _distribution([float(r[f]) for r in records if r.get(f) is not None])
        for f in DECOMP_FIELDS
    }

    buckets = _bucket_by_label(records)
    for label, recs in buckets.items():
        out["by_horizon"][label] = {
            f: _distribution([float(r[f]) for r in recs if r.get(f) is not None])
            for f in DECOMP_FIELDS
        }
    out["n_horizons"] = len(buckets)

    diffs_base: list[float] = []
    diffs_final: list[float] = []
    for r in records:
        sq, dr = r.get("signal_quality"), r.get("directional_score")
        bc, dm = r.get("base_conf"), r.get("diversity_mult")
        fc = r.get("forecast_conf")
        if sq is not None and dr is not None and bc is not None:
            diffs_base.append(abs(float(sq) * float(dr) - float(bc)))
        if bc is not None and dm is not None and fc is not None:
            diffs_final.append(abs(float(bc) * float(dm) - float(fc)))
    out["sanity"] = {
        "n_base_pairs": len(diffs_base),
        "mean_abs_diff_base": (sum(diffs_base) / len(diffs_base)) if diffs_base else None,
        "max_abs_diff_base": max(diffs_base) if diffs_base else None,
        "n_final_pairs": len(diffs_final),
        "mean_abs_diff_final": (sum(diffs_final) / len(diffs_final)) if diffs_final else None,
        "max_abs_diff_final": max(diffs_final) if diffs_final else None,
    }

    if top_conf_quantile is not None and 0.0 < top_conf_quantile < 1.0:
        confs = sorted(float(r["forecast_conf"]) for r in records
                       if r.get("forecast_conf") is not None)
        if confs:
            cutoff = _percentile(confs, top_conf_quantile * 100.0)
            top = [r for r in records
                   if r.get("forecast_conf") is not None
                   and float(r["forecast_conf"]) >= cutoff]
            out["top_subset"] = {
                "quantile": top_conf_quantile,
                "cutoff": cutoff,
                "n": len(top),
                "distributions": {
                    f: _distribution([float(r[f]) for r in top
                                      if r.get(f) is not None])
                    for f in DECOMP_FIELDS
                },
            }
        else:
            out["top_subset"] = {"quantile": top_conf_quantile, "n": 0}
    else:
        out["top_subset"] = None

    return out


def _fmt_distribution(name: str, dist: dict) -> str:
    if dist["n"] == 0:
        return f"{name:<20}  n=0 (no data)"
    pcts = dist["percentiles"]
    return (
        f"{name:<20}  n={dist['n']:<6}  mean={dist['mean']:.4f}  "
        f"p10={pcts[10]:.3f}  p25={pcts[25]:.3f}  p50={pcts[50]:.3f}  "
        f"p75={pcts[75]:.3f}  p90={pcts[90]:.3f}  p95={pcts[95]:.3f}  "
        f"p99={pcts[99]:.3f}"
    )


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("# WS3 H1 Phase 1 — confidence-formula decomposition")
    lines.append("")
    scan = result.get("scan")
    if scan:
        lines.append(f"Days: **{', '.join(scan['days'])}**")
        lines.append(f"Scanned records: {scan['n_scanned']}; "
                     f"post-I1: {scan['n_post_i1']}; "
                     f"pre-I1 skipped: {scan['pre_i1_skipped']}")
        lines.append("")
    lines.append(f"Records analysed: **{result['n_total']}**, "
                 f"horizons present: **{result['n_horizons']}**")
    lines.append("")
    lines.append("## Overall (pooled across horizons)")
    lines.append("")
    lines.append("```")
    for f in DECOMP_FIELDS:
        lines.append(_fmt_distribution(f, result["overall"][f]))
    lines.append("```")
    lines.append("")

    lines.append("## Per horizon (stratified)")
    for label, dists in sorted(result["by_horizon"].items(),
                               key=lambda kv: kv[1][DECOMP_FIELDS[0]]["n"],
                               reverse=True):
        lines.append("")
        lines.append(f"### {label}")
        lines.append("")
        lines.append("```")
        for f in DECOMP_FIELDS:
            lines.append(_fmt_distribution(f, dists[f]))
        lines.append("```")
    lines.append("")

    s = result["sanity"]
    lines.append("## Sanity cross-check (multiplicative identity)")
    lines.append("")
    lines.append("```")
    if s["n_base_pairs"]:
        lines.append(
            f"signal_quality*directional_score vs base_conf   "
            f"n={s['n_base_pairs']:<6}  "
            f"mean|diff|={s['mean_abs_diff_base']:.6f}  "
            f"max={s['max_abs_diff_base']:.6f}"
        )
    if s["n_final_pairs"]:
        lines.append(
            f"base_conf*diversity_mult vs forecast_conf        "
            f"n={s['n_final_pairs']:<6}  "
            f"mean|diff|={s['mean_abs_diff_final']:.6f}  "
            f"max={s['max_abs_diff_final']:.6f}"
        )
    lines.append("```")

    top = result.get("top_subset")
    if top and top.get("n", 0) > 0:
        lines.append("")
        lines.append(f"## Top-{top['quantile']:.0%} subset by forecast_conf "
                     f"(cutoff={top['cutoff']:.4f}, n={top['n']})")
        lines.append("")
        lines.append("```")
        for f in DECOMP_FIELDS:
            lines.append(_fmt_distribution(f, top["distributions"][f]))
        lines.append("```")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("WS3 H1 Phase 1 confidence-formula decomposition "
                     "analysis (see WS3_H1_SCOPE.md §3.2)"))
    parser.add_argument("--days", nargs="+", required=True,
                        help="Day strings YYYY-MM-DD; one jsonl per day")
    parser.add_argument("--forecasts-root", type=Path,
                        default=DEFAULT_FORECASTS_ROOT)
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT_DIR),
                        help="Directory for the JSON artifact "
                             "(empty string disables JSON output)")
    parser.add_argument("--top-conf-quantile", type=float, default=0.9,
                        help="Top-Q subset by forecast_conf (0 to disable)")
    args = parser.parse_args(argv)

    paths = [args.forecasts_root / f"{d}.jsonl" for d in args.days]
    all_records = list(_iter_records(paths))
    post_i1 = [r for r in all_records if _is_post_i1(r)]

    if not post_i1:
        print(
            f"[h1-decomp] no post-I1 records in "
            f"{[str(p) for p in paths]} "
            f"(scanned {len(all_records)} total)",
            file=sys.stderr,
        )
        return 2

    top_q = args.top_conf_quantile if args.top_conf_quantile > 0 else None
    result = analyze(post_i1, top_conf_quantile=top_q)
    result["scan"] = {
        "days": args.days,
        "n_scanned": len(all_records),
        "n_post_i1": len(post_i1),
        "pre_i1_skipped": len(all_records) - len(post_i1),
    }

    print(format_report(result))

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"decomp_{'-'.join(args.days)}_{stamp}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n[h1-decomp] wrote JSON artifact: {out_path}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
