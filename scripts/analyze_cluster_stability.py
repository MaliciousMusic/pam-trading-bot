"""Phase G — cluster-stability check across N day ledgers.

The union analysis compresses across days; it can hide drift (a pair that
is 0.8 correlated every day but swaps sign day-to-day appears uncorrelated
in the concatenated view, and vice versa). This script runs the
cos-sim + PCA analysis once per day and reports:

  * per-tribe activity stability (chronic-mute = 0% every single day vs
    regime-mute = 0% some days but not others)
  * lone-wolf stability (max |cos-sim| per tribe, per day — a tribe that
    is lone on 4/4 days is structurally independent; one that is lone
    on 1/4 is opportunistic)
  * top-pair stability (a pair flagged redundant on every day is the
    real passenger; a pair that flips is regime-dependent)
  * PCA rank stability (effective rank @ {50, 80, 90, 95}% per day — if
    rank is stable, council compression is structural)

Usage:
    python -m scripts.analyze_cluster_stability 2026-04-18 2026-04-19 2026-04-20 2026-04-21
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from src.core.config import PROJECT_ROOT
from scripts.analyze_council_orthogonality import (
    build_vote_matrices,
    cos_sim_matrix,
    pca_spectrum,
)

LEDGER_DIR = PROJECT_ROOT / "data" / "ledger"


def run_day(path: Path) -> dict:
    """Compute stability-relevant metrics for one day's ledger."""
    V_conv, V_dir, tribes, n_ticks = build_vote_matrices([path])
    if n_ticks == 0:
        return {"tribes": [], "n_ticks": 0}
    S_conv = cos_sim_matrix(V_conv)
    S_dir = cos_sim_matrix(V_dir)
    activity = (V_dir != 0).mean(axis=0)
    s, ratio, cum = pca_spectrum(V_conv)

    K = len(tribes)
    lw_score = {}
    for i in range(K):
        off = np.delete(S_conv[i], i)
        lw_score[tribes[i]] = float(np.max(np.abs(off))) if off.size else 0.0

    cum_list = cum.tolist()
    rank = {
        "50%": int(np.searchsorted(cum_list, 0.50)) + 1,
        "80%": int(np.searchsorted(cum_list, 0.80)) + 1,
        "90%": int(np.searchsorted(cum_list, 0.90)) + 1,
        "95%": int(np.searchsorted(cum_list, 0.95)) + 1,
    }

    # Pair conviction-cos-sim dict (symmetric)
    pair_cos_conv = {}
    pair_cos_dir = {}
    for i in range(K):
        for j in range(i + 1, K):
            key = (tribes[i], tribes[j])
            pair_cos_conv[key] = float(S_conv[i, j])
            pair_cos_dir[key] = float(S_dir[i, j])

    return {
        "tribes": tribes,
        "n_ticks": n_ticks,
        "activity": dict(zip(tribes, (a.item() for a in activity))),
        "lone_wolf_score": lw_score,
        "rank": rank,
        "pair_cos_conv": pair_cos_conv,
        "pair_cos_dir": pair_cos_dir,
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: analyze_cluster_stability.py <day1> <day2> ...", file=sys.stderr)
        sys.exit(2)

    days = sys.argv[1:]
    paths = [LEDGER_DIR / f"{d}.jsonl" for d in days]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)
        sys.exit(1)

    print(f"[stab] computing per-day metrics for {len(days)} day(s): {days}")
    per_day = {d: run_day(p) for d, p in zip(days, paths)}

    # Unified tribe roster = union across days.
    all_tribes = sorted({t for d in per_day.values() for t in d["tribes"]})

    # ------------------------------------------------------------------
    # 1. Activity stability
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  ACTIVITY RATE PER DAY (0% = completely silent)")
    print("=" * 78)
    header = f"  {'tribe':<36}" + "".join(f"{d[-5:]:>8}" for d in days) + "   verdict"
    print(header)
    chronic_mute = []
    regime_mute = []
    for t in all_tribes:
        rates = [per_day[d]["activity"].get(t, 0.0) for d in days]
        flag = ""
        if all(r < 0.01 for r in rates):
            flag = "CHRONIC_MUTE"
            chronic_mute.append(t)
        elif all(r < 0.05 for r in rates):
            flag = "CHRONIC_LOW"
            chronic_mute.append(t)
        elif any(r < 0.05 for r in rates) and any(r > 0.20 for r in rates):
            flag = "REGIME_SWING"
            regime_mute.append(t)
        row = f"  {t:<36}" + "".join(f"{r*100:>7.1f}%" for r in rates) + f"   {flag}"
        print(row)

    # ------------------------------------------------------------------
    # 2. Lone-wolf stability
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  LONE-WOLF SCORE PER DAY (max |cos-sim| to any other tribe)")
    print("  Structural lone-wolf = lone on ALL days (score < 0.30 each)")
    print("=" * 78)
    print(f"  {'tribe':<36}" + "".join(f"{d[-5:]:>8}" for d in days) + "   days_lone / verdict")
    structural_lone = []
    for t in all_tribes:
        scores = [per_day[d]["lone_wolf_score"].get(t, float("nan")) for d in days]
        n_lone = sum(1 for s in scores if s < 0.30)
        flag = ""
        if n_lone == len(days):
            flag = f"{n_lone}/{len(days)}  STRUCTURAL"
            structural_lone.append(t)
        elif n_lone >= len(days) - 1:
            flag = f"{n_lone}/{len(days)}  mostly_lone"
        elif n_lone == 0:
            flag = f"{n_lone}/{len(days)}  always_clustered"
        else:
            flag = f"{n_lone}/{len(days)}  drifting"
        row = f"  {t:<36}" + "".join(f"{s:>8.2f}" for s in scores) + f"   {flag}"
        print(row)

    # ------------------------------------------------------------------
    # 3. Top-pair stability (directional)
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  PAIR STABILITY -- directional cos-sim (binarized vote)")
    print("  Pair is structural-redundant if dir_cos >= 0.60 on ALL days")
    print("=" * 78)
    # Union of pairs across all days
    all_pairs = set()
    for d in days:
        all_pairs.update(per_day[d]["pair_cos_dir"].keys())

    # Compute mean + std for pairs that exist on most days
    pair_stats = []
    for p in all_pairs:
        vals = [per_day[d]["pair_cos_dir"].get(p) for d in days]
        vals_clean = [v for v in vals if v is not None]
        if len(vals_clean) < len(days):
            continue
        mean = float(np.mean(vals_clean))
        std = float(np.std(vals_clean))
        mini = float(np.min(vals_clean))
        maxi = float(np.max(vals_clean))
        pair_stats.append((p, vals, mean, std, mini, maxi))

    # Rank by |mean|, print top 20
    pair_stats.sort(key=lambda x: -abs(x[2]))
    print(f"  {'tribe A':<28} {'tribe B':<28}" + "".join(f"{d[-5:]:>8}" for d in days) + "   mean±std    verdict")
    structural_pairs = []
    for p, vals, mean, std, mini, maxi in pair_stats[:20]:
        a, b = p
        # Verdict
        flag = ""
        if all(v >= 0.60 for v in vals):
            flag = "STRUCT_POS"
            structural_pairs.append((a, b, mean, "pro"))
        elif all(v <= -0.60 for v in vals):
            flag = "STRUCT_NEG"
            structural_pairs.append((a, b, mean, "anti"))
        elif std > 0.3:
            flag = "UNSTABLE"
        cells = "".join(f"{v:>+8.2f}" for v in vals)
        print(f"  {a:<28} {b:<28}{cells}   {mean:>+.2f}±{std:.2f}   {flag}")

    # ------------------------------------------------------------------
    # 4. PCA rank stability
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  EFFECTIVE RANK PER DAY (components to reach X% variance)")
    print("=" * 78)
    print(f"  {'day':<16}{'50%':>6}{'80%':>6}{'90%':>6}{'95%':>6}{'nominal':>10}")
    for d in days:
        r = per_day[d]["rank"]
        K = len(per_day[d]["tribes"])
        print(f"  {d:<16}{r['50%']:>6}{r['80%']:>6}{r['90%']:>6}{r['95%']:>6}{K:>10}")

    # ------------------------------------------------------------------
    # 5. Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  STABILITY SUMMARY")
    print("=" * 78)
    print(f"  chronic mute (silent every day): {chronic_mute}")
    print(f"  structural lone-wolves (max |cos|<0.30 every day): {structural_lone}")
    print(f"  structural redundant pairs (dir cos>=0.60 every day, pro or anti):")
    for a, b, mean, kind in structural_pairs:
        print(f"    [{kind.upper():<4}] {a} <-> {b}  (mean dir_cos = {mean:+.2f})")
    print()


if __name__ == "__main__":
    main()
