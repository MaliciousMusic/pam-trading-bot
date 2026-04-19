"""Package a completed run directory into a downloadable .zip."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .report import load_run_metrics
from .report_html import render_html
from .report_md import render_md

_SKIP_SUFFIXES = {".tmp", ".pyc"}
_SKIP_DIRS = {"__pycache__"}


def build_zip(run_dir: Path, out_path: Path | None = None) -> Path:
    """Build {run_id}.zip from a completed run directory.

    Contents:
      report.html           — self-contained HTML with SVG charts
      report.md             — Markdown summary for copy-paste
      multi_manifest.json   — orchestrator rollup (if present)
      variants/
        {variant_id}/
          manifest.json
          decisions.jsonl
          ledger/...
          worker.stdout.log
          worker.stderr.log

    Returns the path to the written zip."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    if out_path is None:
        out_path = run_dir.parent / f"{run_id}.zip"

    metrics = load_run_metrics(run_dir)
    html_bytes = render_html(metrics).encode("utf-8")
    md_bytes = render_md(metrics).encode("utf-8")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(f"{run_id}/report.html", html_bytes)
        zf.writestr(f"{run_id}/report.md", md_bytes)

        for src in sorted(run_dir.rglob("*")):
            if not src.is_file():
                continue
            if src.suffix in _SKIP_SUFFIXES:
                continue
            if any(p in _SKIP_DIRS for p in src.parts):
                continue
            rel = src.relative_to(run_dir.parent)
            zf.write(src, str(rel).replace("\\", "/"))

    return out_path
