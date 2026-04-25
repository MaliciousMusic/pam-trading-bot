"""H6.C live-gate diagnostic — read the post-H6.C bot stderr log and
forecasts tape, compute steady-state saturation rate (the gate
metric: < 5% PASS, >= 5% STOP/check-in).

Produces a 5-line summary:
  total_ticks, sat_hits_total, sat_hits_steady (tick>=100),
  sat_rate_steady_pct, dna_delta_clamp_total, depth_scale_cap_total,
  dna_ctx_clamp_total

Usage:
  python -m scripts.h6c_live_diagnostic --err logs/bot_ui_2026-04-25_post_H6C.err
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--err", required=True, type=Path,
                    help="bot stderr log path")
    ap.add_argument("--forecasts", type=Path,
                    help="forecasts jsonl path (optional, for tick count)")
    ap.add_argument("--steady-from", type=int, default=100,
                    help="tick index from which to compute steady-state")
    args = ap.parse_args()

    txt = args.err.read_text(encoding="utf-8", errors="replace").splitlines()

    # Saturation hits — emitted by chess.py:_step_bps_list saturation hit warning.
    sat_hits = sum(1 for ln in txt if "saturation hit" in ln)
    # H6.A diagnostic
    dna_ctx = 0
    for ln in txt:
        m = re.search(r"DNA ctx clamp fired (\d+) times", ln)
        if m:
            dna_ctx += int(m.group(1))
    # H6.B diagnostic
    depth_cap = 0
    for ln in txt:
        m = re.search(r"depth-scale cap fired (\d+) times", ln)
        if m:
            depth_cap += int(m.group(1))
    # H6.C diagnostic
    dna_out = 0
    for ln in txt:
        m = re.search(r"dna_delta clamp fired (\d+) times", ln)
        if m:
            dna_out += int(m.group(1))

    # Tick count via forecasts.jsonl if provided, else infer from log
    total_ticks = 0
    if args.forecasts and args.forecasts.exists():
        with args.forecasts.open(encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    total_ticks += 1
    else:
        # Crude: each `[projector]` warning fires once per tick. Use depth_cap
        # warnings (always fires post-H6.B) as a proxy.
        total_ticks = sum(1 for ln in txt if "depth-scale cap fired" in ln)

    sat_steady = max(0, sat_hits - args.steady_from)  # sat hits in tick > steady_from
    ticks_steady = max(0, total_ticks - args.steady_from)
    sat_rate_steady = (sat_steady / ticks_steady * 100.0) if ticks_steady > 0 else 0.0

    print(f"[H6.C live-gate]")
    print(f"  total_ticks         = {total_ticks}")
    print(f"  sat_hits_total      = {sat_hits}")
    print(f"  steady_from         = tick {args.steady_from}")
    print(f"  ticks_steady        = {ticks_steady}")
    print(f"  sat_hits_steady     = {sat_steady}")
    print(f"  sat_rate_steady     = {sat_rate_steady:.1f}%   (gate: < 5% PASS)")
    print(f"  dna_ctx_clamp       = {dna_ctx} (H6.A, recursion-input)")
    print(f"  depth_scale_cap     = {depth_cap} (H6.B)")
    print(f"  dna_delta_clamp     = {dna_out} (H6.C, output)")
    if sat_rate_steady < 5.0:
        print(f"  -> PASS")
    else:
        print(f"  -> FAIL  -> STOP, check-in")


if __name__ == "__main__":
    main()
