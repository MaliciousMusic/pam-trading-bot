"""Render a RunMetrics into a Markdown summary."""
from __future__ import annotations

from .report import RunMetrics, VariantMetrics


def _drift_block(metrics: RunMetrics) -> list[str]:
    """Phase 13 drift alert. Compares variant_0 (seed-bank) to the median
    of the evolved cohort by Δequity. Emits a banner when the seed bank
    beat the cohort median this run.

    The "two consecutive weeks" rule lives in the orchestrator/scheduler
    that holds historical state — this module only flags the *current*
    week. The CLI reads last week's flag from the previous report's
    manifest and combines them."""
    sb = next((v for v in metrics.variants
               if v.variant_id == "variant_0"
               and v.lineage_generation > 0), None)
    cohort = [v for v in metrics.variants
              if v.variant_id != "variant_0"
              and v.lineage_generation > 0]
    if sb is None or not cohort:
        return []
    sorted_cohort = sorted(v.equity_delta for v in cohort)
    n = len(sorted_cohort)
    median = (sorted_cohort[n // 2] if n % 2
              else 0.5 * (sorted_cohort[n // 2 - 1] + sorted_cohort[n // 2]))
    if sb.equity_delta > median:
        gap = sb.equity_delta - median
        return [
            "> **⚠️ DRIFT WATCH** — seed-bank `variant_0` outperformed the "
            f"evolved cohort median by ${gap:+,.2f}. If this repeats next "
            "generation, mutation cycle is destroying alpha — flag GENETIC_DRIFT.",
            "",
        ]
    return []


def _variant_block(vm: VariantMetrics) -> str:
    delta_sign = "+" if vm.equity_delta >= 0 else ""
    fdelta_sign = "+" if vm.frictionless_delta >= 0 else ""
    title_extra = ""
    if vm.lineage_generation > 0:
        parent = vm.lineage_parent_id or "—"
        title_extra = f"  · gen {vm.lineage_generation}, parent {parent}"
    lines = [
        f"### {vm.variant_id}  (synth_rate={vm.synth_rate}, seed={vm.seed}){title_extra}",
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
        f"| Δ equity (frictionless) | {fdelta_sign}${vm.frictionless_delta:,.2f} |",
        f"| friction cost | ${vm.friction_cost_usd:,.2f}  "
        f"(slip ${vm.slippage_usd_total:,.2f} + fee ${vm.fee_usd_total:,.2f}) |",
        f"| avg intended bps | {vm.intended_bps_avg:+.1f} |",
        f"| avg realized bps | {vm.realized_bps_avg:+.1f} |",
        f"| avg bps drag | {vm.bps_drag_avg:+.1f} |",
        f"| capacity clipped | {vm.capacity_clipped_count} |",
        f"| capacity rejected | {vm.capacity_rejected_count} |",
        "",
    ]
    if vm.lineage_generation > 0:
        lines += [
            f"_lineage:_ generation **{vm.lineage_generation}** · "
            f"parent **{vm.lineage_parent_id or '—'}** · "
            f"{vm.lineage_mutated_count} mutated fields"
            + (f" · _{vm.lineage_notes}_" if vm.lineage_notes else ""),
            "",
        ]
    if vm.trades_table:
        lines += [
            "**Per-trade intended vs realized (last 20):**",
            "",
            "| ts | side | size | int_bps | real_bps | drag_bps | "
            "slip$ | fee$ | net$ | reason |",
            "|----|------|------|---------|----------|----------|"
            "-------|------|------|--------|",
        ]
        for t in vm.trades_table[-20:]:
            ib = float(t["intended_bps"])
            rb = float(t["realized_bps"])
            drag = ib - rb
            ts = (t.get("ts") or "")[-8:]  # HH:MM:SS suffix
            reason = (t.get("reason", "") or "").replace("|", "/")[:32]
            lines.append(
                f"| {ts} | {t['side']} | ${float(t['size_usd']):,.0f} "
                f"| {ib:+.1f} | {rb:+.1f} | {drag:+.1f} "
                f"| ${float(t['total_slip_usd']):.2f} "
                f"| ${float(t['total_fee_usd']):.2f} "
                f"| ${float(t['net_usd']):+.2f} | {reason} |"
            )
        lines.append("")

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
        any_evolved = any(v.lineage_generation > 0 for v in metrics.variants)
        if any_evolved:
            lines += [
                "## Variant comparison",
                "",
                "| variant | gen | parent | synth_rate | seed | ticks | trades | "
                "win% | Δeq | Δeq frictionless | friction $ | drag bps |",
                "|---------|-----|--------|------------|------|-------|--------|"
                "------|-----|------------------|-----------|----------|",
            ]
            for vm in metrics.variants:
                delta_sign = "+" if vm.equity_delta >= 0 else ""
                fdelta_sign = "+" if vm.frictionless_delta >= 0 else ""
                gen = vm.lineage_generation if vm.lineage_generation else "—"
                par = vm.lineage_parent_id or "—"
                lines.append(
                    f"| {vm.variant_id} | {gen} | {par} | {vm.synth_rate} | "
                    f"{vm.seed} | {vm.ticks:,} | {vm.trades} | "
                    f"{vm.win_rate*100:.1f}% "
                    f"| {delta_sign}${vm.equity_delta:,.2f} "
                    f"| {fdelta_sign}${vm.frictionless_delta:,.2f} "
                    f"| ${vm.friction_cost_usd:,.2f} "
                    f"| {vm.bps_drag_avg:+.1f} |"
                )
            lines.append("")
            lines += _drift_block(metrics)
        else:
            lines += [
                "## Variant comparison",
                "",
                "| variant | synth_rate | seed | ticks | trades | win% | "
                "Δeq | Δeq frictionless | friction $ | drag bps |",
                "|---------|------------|------|-------|--------|------|"
                "-----|------------------|-----------|----------|",
            ]
            for vm in metrics.variants:
                delta_sign = "+" if vm.equity_delta >= 0 else ""
                fdelta_sign = "+" if vm.frictionless_delta >= 0 else ""
                lines.append(
                    f"| {vm.variant_id} | {vm.synth_rate} | {vm.seed} "
                    f"| {vm.ticks:,} | {vm.trades} | {vm.win_rate*100:.1f}% "
                    f"| {delta_sign}${vm.equity_delta:,.2f} "
                    f"| {fdelta_sign}${vm.frictionless_delta:,.2f} "
                    f"| ${vm.friction_cost_usd:,.2f} "
                    f"| {vm.bps_drag_avg:+.1f} |"
                )
            lines.append("")

    lines.append("## Per-variant detail")
    lines.append("")
    for vm in metrics.variants:
        lines.append(_variant_block(vm))

    return "\n".join(lines)
