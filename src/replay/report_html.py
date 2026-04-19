"""Render a RunMetrics into a single-file, self-contained HTML report.

All CSS, data, and SVG charts are inlined — the .html file works offline
and opens without a server. Charts are SVG so they scale at any DPI.
"""
from __future__ import annotations

import html
import math

from .report import RunMetrics, VariantMetrics

VARIANT_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
]

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; font-size: 14px; color: #222;
       background: #f8f8f8; padding: 24px; }
header { margin-bottom: 20px; }
h1 { font-size: 22px; font-weight: 700; color: #111; }
.meta { color: #666; margin-top: 4px; font-size: 13px; }
section { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
          padding: 16px; margin-bottom: 16px; }
h2 { font-size: 15px; font-weight: 600; margin-bottom: 10px; color: #333; }
h3 { font-size: 13px; font-weight: 600; margin: 12px 0 8px; color: #555; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { background: #f4f4f4; text-align: left; padding: 6px 10px;
     border-bottom: 2px solid #ddd; white-space: nowrap; }
td { padding: 5px 10px; border-bottom: 1px solid #eee; }
tr:last-child td { border-bottom: none; }
.up { color: #2a9d2a; } .down { color: #cc2222; }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
              margin-top: 8px; }
.chart-full { margin-top: 8px; }
.chart-box { background: #fafafa; border: 1px solid #e8e8e8; border-radius: 4px;
             padding: 10px; }
.chart-box h3 { margin-top: 0; }
svg text { font-family: system-ui, sans-serif; }
.tribe-bar-wrap { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.tribe-label { font-size: 11px; color: #555; width: 160px; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; text-align: right; }
.tribe-bar-track { flex: 1; height: 12px; background: #eee; border-radius: 2px;
                   position: relative; }
.tribe-bar-fill { height: 100%; border-radius: 2px; position: absolute; }
.tribe-val { font-size: 11px; color: #666; width: 50px; }
"""


def _svg_line(
    series: list[tuple[str, list[float]]],
    colors: list[str],
    width: int = 760,
    height: int = 180,
    pad: int = 40,
) -> str:
    if not series or not series[0][1]:
        return f'<svg width="{width}" height="{height}"><text x="10" y="20" font-size="12" fill="#999">no data</text></svg>'

    n = max(len(vals) for _, vals in series)
    all_vals = [v for _, vals in series for v in vals if math.isfinite(v)]
    y_min = min(all_vals)
    y_max = max(all_vals)
    y_span = y_max - y_min or 1.0

    W = width - pad * 2
    H = height - pad * 2

    def sx(i: int, total: int) -> float:
        return pad + (i / max(total - 1, 1)) * W

    def sy(v: float) -> float:
        return pad + (1 - (v - y_min) / y_span) * H

    lines: list[str] = []

    for pct in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad + (1 - pct) * H
        gv = y_min + pct * y_span
        label = f"{gv:,.0f}" if abs(gv) >= 100 else f"{gv:.3f}"
        lines.append(f'<line x1="{pad}" y1="{gy:.1f}" x2="{pad+W}" y2="{gy:.1f}" '
                     f'stroke="#eeeeee" stroke-width="1"/>')
        lines.append(f'<text x="{pad-4}" y="{gy+4:.1f}" text-anchor="end" '
                     f'font-size="9" fill="#aaa">{html.escape(label)}</text>')

    for (label, vals), color in zip(series, colors):
        if not vals:
            continue
        pts = " ".join(f"{sx(i, len(vals)):.1f},{sy(v):.1f}" for i, v in enumerate(vals))
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="2" stroke-linejoin="round"/>')

    legend_x = pad
    legend_y = height - 8
    for (label, _), color in zip(series, colors):
        lines.append(f'<rect x="{legend_x}" y="{legend_y-10}" width="10" height="10" fill="{color}"/>')
        lines.append(f'<text x="{legend_x+14}" y="{legend_y}" font-size="10" fill="#444">'
                     f'{html.escape(label)}</text>')
        legend_x += len(label) * 7 + 24

    body = "\n".join(lines)
    return (f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block;">\n{body}\n</svg>')


def _svg_hbar(
    data: dict[str, int | float],
    color: str = "#4e79a7",
    width: int = 340,
    height: int | None = None,
    pad_left: int = 90,
    pad_right: int = 50,
) -> str:
    if not data:
        return f'<svg width="{width}" height="40"><text x="10" y="20" font-size="12" fill="#999">no data</text></svg>'
    items = sorted(data.items(), key=lambda x: -x[1])
    row_h = 22
    h = height or (len(items) * row_h + 16)
    max_val = max(v for _, v in items) or 1
    bar_max_w = width - pad_left - pad_right

    lines: list[str] = []
    for i, (label, val) in enumerate(items):
        y = 8 + i * row_h
        bw = (val / max_val) * bar_max_w
        lines.append(f'<rect x="{pad_left}" y="{y}" width="{bw:.1f}" height="{row_h-4}" '
                     f'rx="2" fill="{color}"/>')
        pct = f"{100*val/sum(v for _,v in items):.0f}%" if sum(v for _,v in items) else ""
        lines.append(f'<text x="{pad_left+bw+4:.1f}" y="{y+row_h-8}" font-size="10" fill="#555">'
                     f'{val} {html.escape(pct)}</text>')
        lines.append(f'<text x="{pad_left-4}" y="{y+row_h-8}" text-anchor="end" '
                     f'font-size="10" fill="#666">{html.escape(str(label))}</text>')

    return (f'<svg viewBox="0 0 {width} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block;">\n'
            + "\n".join(lines) + "\n</svg>")


def _tribe_bars_html(tribe_net: dict[str, float]) -> str:
    if not tribe_net:
        return "<p><em>no tribe data</em></p>"
    items = sorted(tribe_net.items(), key=lambda x: -abs(x[1]))
    max_abs = max(abs(v) for _, v in items) or 1.0
    rows: list[str] = []
    for tid, net in items:
        pct = net / max_abs  # -1 … +1
        color = "#2a9d2a" if net >= 0 else "#cc2222"
        # Bar centred at 50%: positive goes right, negative goes left
        left = 50.0 if net >= 0 else 50.0 + pct * 50
        bar_w = abs(pct) * 50
        rows.append(
            f'<div class="tribe-bar-wrap">'
            f'<span class="tribe-label">{html.escape(tid)}</span>'
            f'<div class="tribe-bar-track">'
            f'<div class="tribe-bar-fill" style="left:{left:.1f}%;width:{bar_w:.1f}%;background:{color};"></div>'
            f'</div>'
            f'<span class="tribe-val">{"+" if net>0 else ""}{net:.3f}</span>'
            f'</div>'
        )
    return "\n".join(rows)


def _variant_section(vm: VariantMetrics, color: str, show_all_charts: bool) -> str:
    delta_cls = "up" if vm.equity_delta >= 0 else "down"
    delta_str = f'{vm.equity_delta:+,.2f}'
    wr_pct = f"{vm.win_rate*100:.1f}%"

    stats = (
        f'<table><tr><th>Ticks</th><th>Trades</th><th>Win&nbsp;rate</th>'
        f'<th>Start&nbsp;equity</th><th>End&nbsp;equity</th><th>&#916;Equity</th></tr>'
        f'<tr><td>{vm.ticks:,}</td><td>{vm.trades}</td><td>{wr_pct}</td>'
        f'<td>${vm.start_equity:,.2f}</td><td>${vm.end_equity:,.2f}</td>'
        f'<td class="{delta_cls}">${delta_str}</td></tr></table>'
    )

    charts = ""
    if show_all_charts:
        price_svg = _svg_line(
            [("price", vm.price_curve)], [color]
        )
        pressure_svg = _svg_line(
            [("net pressure", vm.pressure_curve)], [color],
            height=120,
        )
        uncertainty_svg = _svg_line(
            [("uncertainty", vm.uncertainty_curve)], ["#e15759"],
            height=120,
        )
        charts = (
            f'<div class="chart-box chart-full"><h3>Price</h3>{price_svg}</div>'
            f'<div class="chart-grid">'
            f'<div class="chart-box"><h3>Net Pressure</h3>{pressure_svg}</div>'
            f'<div class="chart-box"><h3>Uncertainty</h3>{uncertainty_svg}</div>'
            f'</div>'
        )

    action_svg = _svg_hbar(vm.action_counts, color)
    regime_svg = _svg_hbar(vm.regime_counts, "#76b7b2")
    tribe_html = _tribe_bars_html(vm.tribe_net)

    return (
        f'<section>'
        f'<h2>{html.escape(vm.variant_id)} '
        f'<small style="font-weight:normal;color:#888;">'
        f'synth_rate={vm.synth_rate} &bull; seed={vm.seed}</small></h2>'
        f'{stats}{charts}'
        f'<div class="chart-grid">'
        f'<div class="chart-box"><h3>Actions</h3>{action_svg}</div>'
        f'<div class="chart-box"><h3>Regime</h3>{regime_svg}</div>'
        f'</div>'
        f'<div class="chart-box" style="margin-top:8px;">'
        f'<h3>Tribe net pressure (avg signed conviction)</h3>'
        f'{tribe_html}</div>'
        f'</section>'
    )


def render_html(metrics: RunMetrics) -> str:
    colors = VARIANT_COLORS[: len(metrics.variants)]

    # Comparison table for multi-variant
    comparison = ""
    if metrics.is_multi:
        rows = ""
        for vm, color in zip(metrics.variants, colors):
            delta_cls = "up" if vm.equity_delta >= 0 else "down"
            rows += (
                f"<tr>"
                f'<td><span style="color:{color}">&#9632;</span> {html.escape(vm.variant_id)}</td>'
                f"<td>{vm.synth_rate}</td><td>{vm.seed}</td>"
                f"<td>{vm.ticks:,}</td><td>{vm.trades}</td>"
                f'<td>{vm.win_rate*100:.1f}%</td>'
                f'<td class="{delta_cls}">${vm.equity_delta:+,.2f}</td>'
                f"</tr>\n"
            )
        comparison = (
            f"<section><h2>Variant comparison</h2>"
            f"<table><tr>"
            f"<th>variant</th><th>synth_rate</th><th>seed</th>"
            f"<th>ticks</th><th>trades</th><th>win&nbsp;%</th><th>&#916;equity</th>"
            f"</tr>{rows}</table></section>"
        )

    # Combined equity chart (all variants)
    equity_series = [(vm.variant_id, vm.equity_curve) for vm in metrics.variants]
    equity_svg = _svg_line(equity_series, colors, height=220)
    equity_section = (
        f"<section><h2>Equity curves</h2>"
        f'<div class="chart-box">{equity_svg}</div></section>'
    )

    # Per-variant detail sections
    variant_sections = ""
    for vm, color in zip(metrics.variants, colors):
        variant_sections += _variant_section(vm, color, show_all_charts=True)

    wall_str = f"{metrics.wall_seconds:.1f}s" if metrics.wall_seconds else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAM Replay — {html.escape(metrics.run_id)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>PAM Replay Report</h1>
  <div class="meta">
    run_id: <strong>{html.escape(metrics.run_id)}</strong> &bull;
    day: <strong>{html.escape(metrics.day)}</strong> &bull;
    symbol: <strong>{html.escape(metrics.symbol)}</strong> &bull;
    wall: <strong>{wall_str}</strong> &bull;
    variants: <strong>{len(metrics.variants)}</strong>
  </div>
</header>
{comparison}
{equity_section}
{variant_sections}
</body>
</html>"""
