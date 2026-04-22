"""WS3 H2 Option 1a — attribution snapshot.

Reads `data/forecasts/YYYY-MM-DD.jsonl` resolved-anchor records and
simulates KernelCalibrator's fit under both the pre-fix (signed-ratio
median) and post-fix (correct-direction median) behaviors, using the
same trailing-window, min-samples, and clamps as the production class.

Output answers three attribution questions:

  1.  What does the on-disk `last_correction=0.25` decompose into?
      (signed-median under WS1's 44.5 %-directional tape vs
      correct-direction median)
  2.  Does the new `last_dropped_fraction` cross-check match the
      ForecastAuditor's `directional_hit` flag within `MIN_PRED_BPS`?
  3.  Where does gain(t) settle across the 3-day tape panel, pre vs
      post, and does the post-fix value land inside the working band
      `[MIN_GAIN, MAX_GAIN]` or still hit a clamp?

Usage:
    PYTHONPATH=. python -m scripts.ws3_h2_option1a_snapshot \
        --days 2026-04-18 2026-04-19 2026-04-20 2026-04-21

Markdown snapshot written to
`research/phase11_diagnostic/_ws3_h2_snapshots/option1a_<timestamp>.md`.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from src.core.config import PROJECT_ROOT
from src.projector.kernel_calibrator import (
    CORRECTION_CLAMP_HI,
    CORRECTION_CLAMP_LO,
    MAX_GAIN,
    MIN_GAIN,
    MIN_PRED_BPS,
)


# Production defaults not yet exposed as module constants in
# kernel_calibrator.py. Kept in sync by convention — promote if/when
# they become configurable.
WINDOW = 2000
MIN_SAMPLES = 50
HALFLIFE = 300  # count-based fallback; time-based path not exercised here


def _signed_median_correction(pairs: list[tuple[float, float]]) -> float:
    """Pre-fix: signed median of realized / predicted."""
    ratios = sorted(r / p for p, r in pairs if p != 0)
    if not ratios:
        return 1.0
    m = len(ratios) // 2
    raw = ratios[m] if len(ratios) % 2 else 0.5 * (ratios[m - 1] + ratios[m])
    return max(CORRECTION_CLAMP_LO, min(CORRECTION_CLAMP_HI, raw))


def _correct_direction_correction(
    pairs: list[tuple[float, float]],
) -> tuple[float, float, tuple[float, float, float]]:
    """Post-fix: median over the correct-direction subset, dropped
    fraction, (p25, p50, p75) of the kept subset."""
    nonzero = [(p, r) for p, r in pairs if p != 0]
    if not nonzero:
        return 1.0, 0.0, (1.0, 1.0, 1.0)
    kept = sorted(r / p for p, r in nonzero if (p > 0) == (r > 0))
    dropped = 1.0 - len(kept) / len(nonzero)
    if not kept:
        return 1.0, dropped, (0.0, 0.0, 0.0)
    m = len(kept) // 2
    raw = kept[m] if len(kept) % 2 else 0.5 * (kept[m - 1] + kept[m])
    n = len(kept)
    quartiles = (
        kept[max(0, n // 4)],
        raw,
        kept[min(n - 1, (3 * n) // 4)],
    )
    correction = max(CORRECTION_CLAMP_LO, min(CORRECTION_CLAMP_HI, raw))
    return correction, dropped, quartiles


def _ewma_step(gain: float, correction: float, halflife: int) -> float:
    """Identical to `_refit`'s count-based branch: target = gain *
    correction clamped to [MIN, MAX]; EWMA pulls gain by alpha."""
    alpha = 1.0 - 0.5 ** (1.0 / float(halflife))
    target = max(MIN_GAIN, min(MAX_GAIN, gain * correction))
    gain = gain + alpha * (target - gain)
    return max(MIN_GAIN, min(MAX_GAIN, gain))


def _run(
    records: list[dict],
    *,
    initial_gain: float,
    signed: bool,
) -> dict:
    """Stream through records, refitting every time a new sample is
    appended (mirrors `apply_resolved`'s refit cadence for a single-
    record feed). Returns final-state diagnostics + trajectory samples."""
    pairs: deque[tuple[float, float]] = deque(maxlen=WINDOW)
    gain = initial_gain
    n_seen = 0
    last_correction = 1.0
    last_dropped = 0.0
    last_quartiles = (1.0, 1.0, 1.0)
    trajectory: list[tuple[int, float, float, float]] = []  # (n, gain, corr, dropped)
    hit_count = 0
    for rec in records:
        exp = rec.get("expected_move_bps")
        real = rec.get("realized_move_bps")
        if not isinstance(exp, (int, float)) or not isinstance(real, (int, float)):
            continue
        if abs(exp) < MIN_PRED_BPS:
            continue
        pairs.append((float(exp), float(real)))
        n_seen += 1
        if rec.get("directional_hit"):
            hit_count += 1
        if n_seen < MIN_SAMPLES:
            continue
        if signed:
            last_correction = _signed_median_correction(list(pairs))
            dropped = None
            quartiles = (None, None, None)
        else:
            last_correction, last_dropped, last_quartiles = (
                _correct_direction_correction(list(pairs))
            )
            dropped = last_dropped
            quartiles = last_quartiles
        gain = _ewma_step(gain, last_correction, HALFLIFE)
        if n_seen % 500 == 0 or n_seen == MIN_SAMPLES:
            trajectory.append((n_seen, gain, last_correction, dropped or 0.0))
    return {
        "n_seen": n_seen,
        "final_gain": gain,
        "last_correction": last_correction,
        "last_dropped_fraction": last_dropped,
        "last_quartiles": last_quartiles,
        "trajectory": trajectory,
        "directional_hit_count": hit_count,
    }


def _iter_records(path: Path):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        nargs="+",
        default=["2026-04-18", "2026-04-19", "2026-04-20", "2026-04-21"],
        help="Dates whose forecasts to replay (matching data/forecasts/<day>.jsonl).",
    )
    ap.add_argument(
        "--initial-gain",
        type=float,
        default=8.0,
        help="Starting gain for the simulated EWMA. Default matches "
        "`settings.projector.bps_per_unit_pressure: 8.0` — the value that "
        "was in play before the live calibrator walked gain down to 1.57 "
        "via repeated signed-median corrections clamped at 0.25.",
    )
    ap.add_argument(
        "--label-filter",
        default=None,
        help="Optional label filter (e.g. '15s'). Default: all labels, "
        "matching production calibrator behaviour.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT
        / "research"
        / "phase11_diagnostic"
        / "_ws3_h2_snapshots",
    )
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out_dir / f"option1a_{ts}.md"

    lines: list[str] = []
    lines.append(f"# WS3 H2 Option 1a — attribution snapshot ({ts})\n")
    lines.append(
        f"- initial_gain={args.initial_gain}, window={WINDOW}, "
        f"min_samples={MIN_SAMPLES}, halflife={HALFLIFE}\n"
        f"- min_pred_bps={MIN_PRED_BPS}, correction clamp "
        f"[{CORRECTION_CLAMP_LO}, {CORRECTION_CLAMP_HI}], "
        f"gain clamp [{MIN_GAIN}, {MAX_GAIN}]\n"
        f"- label_filter={args.label_filter or 'ALL'}\n\n"
    )

    lines.append("## Per-day end-state\n\n")
    lines.append(
        "`signed corr` / `gain(signed)` replays the pre-fix signed-median "
        "path; `correct-dir corr` quartiles and `gain(opt1a)` use the new "
        "Option 1a filter. Gains are offline-replay trajectories — the "
        "real production feedback (new gain changes subsequent forecasts, "
        "so corrections drift toward 1.0) does **not** apply here.\n\n"
    )
    lines.append(
        "| day | n_used | dir-hit% | dropped% | signed corr | "
        "correct-dir corr (p25/p50/p75) | gain(signed) | gain(opt1a) |\n"
        "|:--|--:|--:|--:|--:|:--|--:|--:|\n"
    )

    all_records: list[dict] = []
    for day in args.days:
        path = PROJECT_ROOT / "data" / "forecasts" / f"{day}.jsonl"
        recs = list(_iter_records(path))
        if args.label_filter:
            recs = [r for r in recs if r.get("label") == args.label_filter]
        # Chronological order matches the calibrator's live feed.
        recs.sort(key=lambda r: r.get("realized_ts", 0.0))
        all_records.extend(recs)

        pre = _run(recs, initial_gain=args.initial_gain, signed=True)
        post = _run(recs, initial_gain=args.initial_gain, signed=False)
        n = pre["n_seen"]
        dir_hit = (pre["directional_hit_count"] / n * 100.0) if n else 0.0
        dropped = (post["last_dropped_fraction"] or 0.0) * 100.0
        q25, q50, q75 = post["last_quartiles"]
        lines.append(
            f"| {day} | {n:,} | {dir_hit:.1f}% | {dropped:.1f}% | "
            f"{pre['last_correction']:.3f} | "
            f"{q25:.3f} / {q50:.3f} / {q75:.3f} | "
            f"{pre['final_gain']:.2f} | {post['final_gain']:.2f} |\n"
        )

    # All-days aggregate — runs one continuous stream in chronological
    # order so the window crosses day boundaries (production calibrator
    # doesn't wipe state at midnight).
    all_records.sort(key=lambda r: r.get("realized_ts", 0.0))
    pre_all = _run(all_records, initial_gain=args.initial_gain, signed=True)
    post_all = _run(all_records, initial_gain=args.initial_gain, signed=False)
    n = pre_all["n_seen"]
    dir_hit = (pre_all["directional_hit_count"] / n * 100.0) if n else 0.0
    dropped = (post_all["last_dropped_fraction"] or 0.0) * 100.0
    q25, q50, q75 = post_all["last_quartiles"]
    lines.append(
        f"| **all (cont.)** | {n:,} | {dir_hit:.1f}% | {dropped:.1f}% | "
        f"{pre_all['last_correction']:.3f} | "
        f"{q25:.3f} / {q50:.3f} / {q75:.3f} | "
        f"{pre_all['final_gain']:.2f} | {post_all['final_gain']:.2f} |\n"
    )
    lines.append("\n")

    lines.append("## Cross-check: dropped-fraction vs directional-hit\n\n")
    lines.append(
        "Option 1a's `last_dropped_fraction` is the wrong-direction share "
        "of non-zero pairs in the window, computed at the **last** refit. "
        "The `directional_hit` column is the per-anchor hit rate from "
        "`ForecastAuditor` over the full day (not a trailing window). They "
        "only need to match in distribution, not value; a large divergence "
        "would imply a dataflow problem. Under `MIN_PRED_BPS={:.1f}` the "
        "two are expected within a few %.\n\n".format(MIN_PRED_BPS)
    )

    lines.append("## Aggregate trajectory (continuous stream)\n\n")
    lines.append("| n_seen | gain(signed) | corr(signed) | gain(opt1a) | corr(opt1a) | dropped% |\n")
    lines.append("|--:|--:|--:|--:|--:|--:|\n")
    pre_traj = {n: (g, c) for n, g, c, _ in pre_all["trajectory"]}
    post_traj = {n: (g, c, d) for n, g, c, d in post_all["trajectory"]}
    for n_seen in sorted(pre_traj.keys() | post_traj.keys()):
        g_pre, c_pre = pre_traj.get(n_seen, (float("nan"), float("nan")))
        g_post, c_post, d_post = post_traj.get(
            n_seen, (float("nan"), float("nan"), 0.0)
        )
        lines.append(
            f"| {n_seen:,} | {g_pre:.2f} | {c_pre:.3f} | "
            f"{g_post:.2f} | {c_post:.3f} | {d_post*100:.1f}% |\n"
        )
    lines.append("\n")

    # Magnitude of the realized-vs-predicted gap. Independent of the
    # calibrator — useful context when reading the correction number.
    def _num(x):
        return x if isinstance(x, (int, float)) else None

    abs_exp = [abs(_num(r.get("expected_move_bps")) or 0.0) for r in all_records
               if _num(r.get("expected_move_bps")) is not None]
    abs_real = [abs(_num(r.get("realized_move_bps")) or 0.0) for r in all_records
                if _num(r.get("realized_move_bps")) is not None]
    kept_pred = [x for x in abs_exp if x >= MIN_PRED_BPS]
    label_counter = Counter(r.get("label") for r in all_records)
    lines.append("## Raw-data context\n\n")
    lines.append(
        f"- total anchors: {len(all_records):,} "
        f"(kept ≥ MIN_PRED_BPS: {len(kept_pred):,})\n"
        f"- |expected_move_bps| median/p90/max: "
        f"{median(abs_exp):.3f} / "
        f"{sorted(abs_exp)[int(len(abs_exp)*0.9)]:.3f} / "
        f"{max(abs_exp):.3f}\n"
        f"- |realized_move_bps| median/p90/max: "
        f"{median(abs_real):.3f} / "
        f"{sorted(abs_real)[int(len(abs_real)*0.9)]:.3f} / "
        f"{max(abs_real):.3f}\n"
        f"- labels: "
        f"{', '.join(f'{k}={v:,}' for k, v in sorted(label_counter.items()))}\n"
    )

    out_path.write_text("".join(lines), encoding="utf-8")
    # Writing in UTF-8; stdout on Windows is cp1252 by default and chokes
    # on the ≥ / μ characters present in the markdown — so just flag where
    # the report landed and let the operator open the file.
    print(f"[snapshot] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
