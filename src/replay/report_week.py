"""Week-unit report builder.

Generates:
  - week_report.html  — cross-day equity curve + per-day table (server-side SVG)
  - week_report.md    — Markdown summary with per-variant carry-over column
  - week_report.zip   — both reports + week_manifest.json

Intended to be called by `week_orchestrator.run_week()` or the standalone
`scripts/run_week.py` CLI.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_manifest(week_manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract structured series needed by both renderers."""
    variants: list[str] = [v["variant_id"] for v in week_manifest.get("variants", [])]
    days: list[str] = [d["day"] for d in week_manifest.get("days", [])]

    # equity_series[variant_id] = [(day_str, start_eq, end_eq), ...]
    equity_series: dict[str, list[tuple[str, float, float]]] = {v: [] for v in variants}
    trades_series: dict[str, list[int]] = {v: [] for v in variants}
    wins_series: dict[str, list[int]] = {v: [] for v in variants}

    for day_data in week_manifest.get("days", []):
        day = day_data["day"]
        for vd in day_data.get("variants", []):
            vid = vd["variant_id"]
            s = vd.get("start_equity_usd") or 0.0
            e = vd.get("end_equity_usd") or 0.0
            t = vd.get("trades") or 0
            w = vd.get("wins") or 0
            if vid in equity_series:
                equity_series[vid].append((day, s, e))
                trades_series[vid].append(t)
                wins_series[vid].append(w)

    return {
        "variants": variants,
        "days": days,
        "equity_series": equity_series,
        "trades_series": trades_series,
        "wins_series": wins_series,
    }


# ── SVG chart ─────────────────────────────────────────────────────────────────

_COLORS = ["#00d4ff", "#ff6b6b", "#ffd93d", "#6bcb77", "#c77dff", "#f4a261"]


def _equity_svg(series: dict[str, Any], width: int = 700, height: int = 260) -> str:
    equity_series = series["equity_series"]
    days = series["days"]
    if not days or not equity_series:
        return "<svg></svg>"

    all_vals: list[float] = []
    for pts in equity_series.values():
        for _, s, e in pts:
            all_vals += [s, e]
    if not all_vals:
        return "<svg></svg>"

    pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
    vmin, vmax = min(all_vals), max(all_vals)
    v_range = max(vmax - vmin, 1.0)
    n = len(days)

    def px(day_idx: int, val: float) -> tuple[float, float]:
        x = pad_l + (day_idx / max(n - 1, 1)) * (width - pad_l - pad_r)
        y = pad_t + (1 - (val - vmin) / v_range) * (height - pad_t - pad_b)
        return x, y

    lines: list[str] = []
    # grid lines
    for step in range(5):
        v = vmin + step * v_range / 4
        _, y = px(0, v)
        lines.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#333" stroke-dasharray="4,4"/>'
        )
        lines.append(
            f'<text x="{pad_l - 4}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#aaa">${v:,.0f}</text>'
        )

    # x-axis labels (trim to max 7)
    label_step = max(1, n // 7)
    for i, d in enumerate(days):
        if i % label_step == 0:
            x, _ = px(i, vmin)
            lines.append(
                f'<text x="{x:.1f}" y="{height - 6}" text-anchor="middle" '
                f'font-size="10" fill="#aaa">{d[5:]}</text>'
            )

    for vi, (vid, pts) in enumerate(equity_series.items()):
        if not pts:
            continue
        color = _COLORS[vi % len(_COLORS)]
        # line through end-of-day equity
        coords = [(days.index(day), e) for day, _, e in pts if day in days]
        if len(coords) < 2:
            continue
        path_d = " ".join(
            f"{'M' if j == 0 else 'L'}{px(di, v)[0]:.1f},{px(di, v)[1]:.1f}"
            for j, (di, v) in enumerate(coords)
        )
        lines.append(
            f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2"/>'
        )
        # dots
        for di, v in coords:
            cx, cy = px(di, v)
            lines.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}"/>')
        # legend
        lines.append(
            f'<text x="{pad_l + 8 + vi * 140}" y="{pad_t + 14}" '
            f'font-size="11" fill="{color}">{vid}</text>'
        )

    inner = "\n".join(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#1a1a2e">{inner}</svg>'
    )


# ── HTML report ───────────────────────────────────────────────────────────────

def render_week_html(week_manifest: dict[str, Any]) -> str:
    series = _parse_manifest(week_manifest)
    svg = _equity_svg(series)

    week_id = week_manifest.get("week_id", "")
    start_day = week_manifest.get("start_day", "")
    symbol = week_manifest.get("symbol", "")
    created = week_manifest.get("created_at_utc", "")
    variants = series["variants"]
    days = series["days"]
    equity_series = series["equity_series"]
    trades_series = series["trades_series"]
    wins_series = series["wins_series"]

    # Summary table rows
    trows: list[str] = []
    for vid in variants:
        pts = equity_series.get(vid, [])
        if pts:
            start_eq = pts[0][1]
            end_eq = pts[-1][2]
            delta = end_eq - start_eq
            total_t = sum(trades_series.get(vid, []))
            total_w = sum(wins_series.get(vid, []))
            wr = f"{total_w / total_t * 100:.1f}%" if total_t else "—"
            sign = "+" if delta >= 0 else ""
            trows.append(
                f"<tr><td>{vid}</td><td>${start_eq:,.2f}</td>"
                f"<td>${end_eq:,.2f}</td>"
                f"<td class='{'pos' if delta >= 0 else 'neg'}'>"
                f"{sign}${delta:,.2f}</td>"
                f"<td>{total_t}</td><td>{wr}</td></tr>"
            )

    # Per-day table
    day_rows: list[str] = []
    for di, day in enumerate(days):
        for vid in variants:
            pts = equity_series.get(vid, [])
            if di < len(pts):
                _, s, e = pts[di]
                delta = e - s
                sign = "+" if delta >= 0 else ""
                t = trades_series.get(vid, [])[di] if di < len(trades_series.get(vid, [])) else 0
                w = wins_series.get(vid, [])[di] if di < len(wins_series.get(vid, [])) else 0
                wr = f"{w / t * 100:.1f}%" if t else "—"
                day_rows.append(
                    f"<tr><td>{day}</td><td>{vid}</td>"
                    f"<td>${s:,.2f}</td><td>${e:,.2f}</td>"
                    f"<td class='{'pos' if delta >= 0 else 'neg'}'>{sign}${delta:,.2f}</td>"
                    f"<td>{t}</td><td>{wr}</td></tr>"
                )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Week Report — {week_id}</title>
<style>
body{{background:#0d0d1a;color:#e0e0e0;font-family:monospace;padding:24px;}}
h1{{color:#00d4ff;}}
h2{{color:#aaa;font-size:1em;margin-top:2em;}}
table{{border-collapse:collapse;width:100%;margin-top:.5em;}}
th{{background:#1a1a2e;color:#888;padding:6px 10px;text-align:left;border-bottom:1px solid #333;}}
td{{padding:5px 10px;border-bottom:1px solid #222;}}
.pos{{color:#6bcb77;}}.neg{{color:#ff6b6b;}}
</style>
</head>
<body>
<h1>Week Report — {week_id}</h1>
<p style="color:#666">{symbol} · start {start_day} · {len(days)} day(s) · generated {created}</p>

<h2>Cross-Day Equity Curve</h2>
{svg}

<h2>Summary (per variant, full week)</h2>
<table>
<tr><th>Variant</th><th>Start Equity</th><th>End Equity</th>
<th>Δ Equity</th><th>Trades</th><th>Win Rate</th></tr>
{''.join(trows)}
</table>

<h2>Per-Day Breakdown</h2>
<table>
<tr><th>Day</th><th>Variant</th><th>Start Equity</th><th>End Equity</th>
<th>Δ Equity</th><th>Trades</th><th>Win Rate</th></tr>
{''.join(day_rows)}
</table>
</body>
</html>"""


# ── Markdown report ───────────────────────────────────────────────────────────

def render_week_md(week_manifest: dict[str, Any]) -> str:
    series = _parse_manifest(week_manifest)
    week_id = week_manifest.get("week_id", "")
    start_day = week_manifest.get("start_day", "")
    symbol = week_manifest.get("symbol", "")
    n_days = week_manifest.get("n_days", 0)
    variants = series["variants"]
    equity_series = series["equity_series"]
    trades_series = series["trades_series"]
    wins_series = series["wins_series"]

    lines = [
        f"# Week Report — {week_id}",
        f"",
        f"**Symbol:** {symbol}  **Start:** {start_day}  **Days:** {n_days}",
        f"",
        f"## Summary",
        f"",
        f"| Variant | Start $ | End $ | Δ $ | Trades | Win% |",
        f"|---|---|---|---|---|---|",
    ]
    for vid in variants:
        pts = equity_series.get(vid, [])
        if pts:
            s = pts[0][1]
            e = pts[-1][2]
            delta = e - s
            t = sum(trades_series.get(vid, []))
            w = sum(wins_series.get(vid, []))
            wr = f"{w / t * 100:.1f}" if t else "—"
            sign = "+" if delta >= 0 else ""
            lines.append(f"| {vid} | ${s:,.2f} | ${e:,.2f} | {sign}${delta:,.2f} | {t} | {wr}% |")

    lines += ["", "## Per-Day Carry", "", "| Day | Variant | Start $ | End $ | Δ $ | Trades |", "|---|---|---|---|---|---|"]
    for di, day_data in enumerate(week_manifest.get("days", [])):
        day = day_data["day"]
        for vd in day_data.get("variants", []):
            vid = vd["variant_id"]
            s = vd.get("start_equity_usd") or 0.0
            e = vd.get("end_equity_usd") or 0.0
            delta = e - s
            t = vd.get("trades") or 0
            sign = "+" if delta >= 0 else ""
            lines.append(f"| {day} | {vid} | ${s:,.2f} | ${e:,.2f} | {sign}${delta:,.2f} | {t} |")

    return "\n".join(lines) + "\n"


# ── Zip builder ───────────────────────────────────────────────────────────────

def build_week_zip(week_root: Path, week_manifest: dict[str, Any]) -> Path:
    """Bundle week_manifest.json + both reports into a zip."""
    zip_path = week_root / "week_report.zip"
    html = render_week_html(week_manifest)
    md = render_week_md(week_manifest)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("week_report.html", html.encode())
        zf.writestr("week_report.md", md.encode())
        manifest_path = week_root / "week_manifest.json"
        if manifest_path.exists():
            zf.write(manifest_path, "week_manifest.json")

    return zip_path
