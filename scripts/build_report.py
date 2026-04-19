"""Build a rich .zip report from a completed replay run directory.

Usage:
    python -m scripts.build_report --run-dir data/replays/multi_2026-04-19_123456
    python -m scripts.build_report --run-dir data/replays/multi_2026-04-19_123456 \\
        --out my_report.zip

Produces {run_id}.zip (next to the run dir by default) containing:
  report.html  — self-contained HTML with SVG equity curves + distributions
  report.md    — Markdown summary for copy-paste
  variants/    — raw artifacts (manifest.json, decisions.jsonl, ledger/)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path,
                    help="path to the run output directory "
                         "(contains multi_manifest.json or variants/)")
    ap.add_argument("--out", default=None, type=Path,
                    help="output .zip path (default: <run_dir>/../<run_id>.zip)")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    from src.replay.report_zip import build_zip

    print(f"  loading metrics from {run_dir} ...")
    out = build_zip(run_dir, out_path=args.out)
    size_kb = out.stat().st_size / 1024
    print(f"  wrote {out}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
