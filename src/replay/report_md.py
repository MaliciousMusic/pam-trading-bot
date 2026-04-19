"""Render a RunMetrics into a Markdown summary."""
from __future__ import annotations

from .report import RunMetrics, VariantMetrics


def _variant_block(vm: VariantMetrics) -> str:
    delta_sign = "+" if vm.equity_delta >= 0 else ""
    lines = [
        f"### {vm.variant_id}  (synth_rate={vm.synth_rate}, seed={vm.seed})",
        "",
        f"| metric | value |",
        f"|--------|-------|",
        f"| ticks | {vm.ticks:,} |",
        f"| trades | {vm.trades} |",
        f"| wins | {vm.wins} |",
        f"| win rate | {vm.win_rate*100:.1f}% |",
        f"| start equity | ${vm.start_equity:,.2f} |",
        f"| end equity | ${vm.end_equity:,.2f} |",
        f"| Δ equity | {delta_sign}${vm.equity_delta:,.2f} |",
        "",
    ]

    if vm.action_counts:
        total = sum(vm.action_counts.values())
        lines.append("**Actions:**")
        for action, cnt in sorted(vm.action_counts.items(), key=lambda x: -x[1]):
            pct = cnt / total * 100
            lines.append(f"- {action}: {cnt} ({pct:.0f}%)")
        lines.append("")

    if vm.regime_counts:
        total = sum(vm.regime_counts.values())
        lines.append("**Regimes:**")
        for reg, cnt in sorted(vm.regime_counts.items(), key=lambda x: -x[1]):
            pct = cnt / total * 100
            lines.append(f"- {reg}: {cnt} ({pct:.0f}%)")
        lines.append("")

    if vm.tribe_net:
        top = sorted(vm.tribe_net.items(), key=lambda x: -abs(x[1]))[:5]
        lines.append("**Top 5 tribes by |avg net pressure|:**")
        for tid, net in top:
            sign = "+" if net >= 0 else ""
            lines.append(f"- {tid}: {sign}{net:.4f}")
        lines.append("")

    return "\n".join(lines)


def render_md(metrics: RunMetrics) -> str:
    wall_str = f"{metrics.wall_seconds:.1f}s" if metrics.wall_seconds else "—"
    lines = [
        f"# PAM Replay Report",
        "",
        f"**run_id:** {metrics.run_id}  ",
        f"**day:** {metrics.day}  ",
        f"**symbol:** {metrics.symbol}  ",
        f"**wall time:** {wall_str}  ",
        f"**variants:** {len(metrics.variants)}",
        "",
    ]

    if metrics.is_multi:
        lines += [
            "## Variant comparison",
            "",
            "| variant | synth_rate | seed | ticks | trades | win% | Δeq |",
            "|---------|------------|------|-------|--------|------|-----|",
        ]
        for vm in metrics.variants:
            delta_sign = "+" if vm.equity_delta >= 0 else ""
            lines.append(
                f"| {vm.variant_id} | {vm.synth_rate} | {vm.seed} "
                f"| {vm.ticks:,} | {vm.trades} | {vm.win_rate*100:.1f}% "
                f"| {delta_sign}${vm.equity_delta:,.2f} |"
            )
        lines.append("")

    lines.append("## Per-variant detail")
    lines.append("")
    for vm in metrics.variants:
        lines.append(_variant_block(vm))

    return "\n".join(lines)
