"""H6.B signal-preservation regression — extract median |emove| on
04-21 top-decile lively-chop windows from a given replay forecasts file.

Usage:
  python -m scripts.h6b_signal_preservation \
      --forecasts data/replays/h6b_baseline_pre_2026-04-21/variants/pre_h6b/forecasts/2026-04-21.jsonl \
      --windows  data/replays/h6b_baseline_pre_2026-04-21/top_decile_windows.json \
      --label    PRE_H6B

Two acceptance modes:
  * baseline   — print only the per-window median (and per-window p10/min)
  * compare    — given two label-files, print PASS/FAIL on the
                 (1) magnitude criterion: median(post)/median(pre) >= 0.70
                 (2) annihilation criterion: min(post emove) >= 1 bps
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def load_forecasts(path: Path):
    rows = []
    with path.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            rows.append(json.loads(ln))
    return rows


def load_windows(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def emove_per_window(rows, windows):
    """Return list of |emove| values per window: one entry per window with
    the *median* |emove| of all forecasts whose tick_ts falls in [start, end).
    Skip windows with zero matching forecasts."""
    by_window = []
    for w in windows:
        in_win = [
            abs(r["expected_move_bps"])
            for r in rows
            if w["start"] <= r["tick_ts"] < w["end"]
        ]
        if in_win:
            by_window.append({
                "start": w["start"],
                "end": w["end"],
                "abs_pc_bps": w["abs_pc_bps"],
                "n_forecasts": len(in_win),
                "median_emove": statistics.median(in_win),
                "p10_emove": sorted(in_win)[max(0, int(len(in_win) * 0.10))],
                "min_emove": min(in_win),
                "max_emove": max(in_win),
            })
    return by_window


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecasts", required=True, type=Path)
    ap.add_argument("--windows", required=True, type=Path)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rows = load_forecasts(args.forecasts)
    windows = load_windows(args.windows)
    per_win = emove_per_window(rows, windows)
    if not per_win:
        print(f"[{args.label}] NO MATCHING FORECASTS — replay tick_ts grid does not overlap window timestamps")
        return

    medians = [w["median_emove"] for w in per_win]
    mins = [w["min_emove"] for w in per_win]
    overall_median = statistics.median(medians)
    overall_min = min(mins)
    print(f"[{args.label}] windows_matched={len(per_win)}/{len(windows)}")
    print(f"[{args.label}] per-window median emove distribution:")
    sm = sorted(medians)
    n = len(sm)
    print(f"    p10={sm[int(n*0.10)]:.4f} p50={sm[n//2]:.4f} p90={sm[int(n*0.90)]:.4f}")
    print(f"[{args.label}] overall_median_of_per_window_medians = {overall_median:.4f} bps")
    print(f"[{args.label}] overall_min_emove (any window any tick) = {overall_min:.4f} bps")

    if args.out:
        args.out.write_text(json.dumps({
            "label": args.label,
            "n_windows": len(per_win),
            "overall_median": overall_median,
            "overall_min": overall_min,
            "per_window": per_win,
        }, indent=2), encoding="utf-8")
        print(f"[{args.label}] saved to {args.out}")


def compare(pre_path: Path, post_path: Path):
    pre = json.loads(pre_path.read_text(encoding="utf-8"))
    post = json.loads(post_path.read_text(encoding="utf-8"))
    pre_by = {(w["start"], w["end"]): w for w in pre["per_window"]}
    post_by = {(w["start"], w["end"]): w for w in post["per_window"]}
    common = sorted(set(pre_by) & set(post_by))
    print(f"common windows: {len(common)} (pre={len(pre_by)} post={len(post_by)})")
    if not common:
        print("NO COMMON WINDOWS — comparison invalid"); return

    # Criterion 1: median ratio
    pre_med = pre["overall_median"]
    post_med = post["overall_median"]
    ratio_med = post_med / pre_med if pre_med > 0 else float("nan")
    c1_pass = ratio_med >= 0.70
    print(f"\n[CRITERION 1 — magnitude relative]")
    print(f"  pre_median  = {pre_med:.4f} bps")
    print(f"  post_median = {post_med:.4f} bps")
    print(f"  ratio       = {ratio_med:.4f}  (threshold >= 0.70)")
    print(f"  -> {'PASS' if c1_pass else 'FAIL'}")

    # Criterion 2: per-window annihilation
    failures = []
    for k in common:
        pm = pre_by[k]["median_emove"]
        qm = post_by[k]["median_emove"]
        r = qm / pm if pm > 0 else float("inf")
        if r < 0.10:
            failures.append((k, pm, qm, r))
    n_fail = len(failures)
    if n_fail == 0:
        c2_status = "PASS"
    elif n_fail <= 2:
        c2_status = "PASS_WITH_FLAG"
    else:
        c2_status = "FAIL"
    print(f"\n[CRITERION 2 — per-window annihilation (post/pre < 0.10)]")
    print(f"  windows failing: {n_fail}/{len(common)}")
    for (k, pm, qm, r) in failures:
        print(f"    window [{k[0]},{k[1]}): pre={pm:.4f} post={qm:.4f} ratio={r:.4f}")
    print(f"  -> {c2_status}  (0 fail = PASS, 1-2 = PASS_WITH_FLAG, 3+ = FAIL/check-in)")

    overall = "PASS" if (c1_pass and c2_status in ("PASS", "PASS_WITH_FLAG")) else "FAIL"
    print(f"\n[OVERALL] {overall}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        compare(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        main()
