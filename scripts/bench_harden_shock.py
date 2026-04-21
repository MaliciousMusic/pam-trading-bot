"""Phase 10-Harden — Merciless shock report.

Runs the same sprint TWICE — once with `harden.enabled=False` (Gentle
baseline, bit-for-bit identical to pre-Harden), once with
`harden.enabled=True` (Merciless overlay armed) — and produces a side-
by-side report of how reality degrades theoretical performance.

The two runs share every other knob: same day, same seed, same synth
rate, same tick count, same tribes, same settings. The only difference
is the three adversarial layers (adversarial fill, fat-tail latency,
sqrt-slippage overlay).

Usage:
    python -m scripts.bench_harden_shock --day 2026-04-20 --max-ticks 500
    python -m scripts.bench_harden_shock --day 2026-04-20 --max-ticks 500 \
        --out docs/perf_baselines/harden_shock.json

Exit codes:
  0   report produced
  2   bad args / run failed
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

from loguru import logger

from src.core import serde
from src.execution.harden import HardenConfig
from src.replay.harness import run_sprint


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _parse_run(decisions_path: Path, ledger_dir: Path) -> dict:
    """Parse both log streams a sprint writes and collect the shock-report
    diagnostics.

    `decisions.jsonl` carries per-tick rows (votes, regime, action) — that's
    where the vote tallies come from.

    `<ledger_dir>/*.jsonl` carries the executor's event stream — `open`,
    `close`, `latency_spike`, `capacity_rejected`, `skip`. That's where
    the entry-slippage distribution and Merciless telemetry (deferred
    fills, drift bps, capacity clips) actually live.

    Parsing both keeps the shock report honest: the main equity/trade
    numbers come from `SprintResult`, but the friction breakdown needs
    the ledger."""
    buys: Counter[str] = Counter()
    sells: Counter[str] = Counter()
    holds: Counter[str] = Counter()
    ticks = 0
    # Stream 1 — decisions.jsonl (tick rows only).
    with decisions_path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = serde.loads(line)
            if rec.get("tick") is None:
                continue
            ticks += 1
            for v in rec.get("votes", []):
                side = str(v.get("side", "abstain")).lower()
                tid = v["tribe_id"]
                if side == "buy":
                    buys[tid] += 1
                elif side == "sell":
                    sells[tid] += 1
                else:
                    holds[tid] += 1

    # Stream 2 — executor ledger (open / close / latency_spike / ...).
    latency_events = 0
    deferred_opens = 0
    drifts_bps: list[float] = []        # realized drift on deferred fills
    capacity_clips = 0
    capacity_rejects = 0
    entry_slip_bps: list[float] = []    # open events, all sides
    exit_slip_bps: list[float] = []     # close events
    pareto_latencies = 0                # spikes drawn from the fat tail
    latency_draws_s: list[float] = []   # all deferred-fill latencies (incl. body)
    if ledger_dir.exists():
        for p in sorted(ledger_dir.glob("*.jsonl")):
            with p.open("rb") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = serde.loads(line)
                    ev = rec.get("event")
                    if ev == "open":
                        sb = rec.get("slippage_bps")
                        if isinstance(sb, (int, float)):
                            entry_slip_bps.append(float(sb))
                        if rec.get("harden_deferred"):
                            deferred_opens += 1
                            drift = rec.get("latency_drift_bps")
                            if isinstance(drift, (int, float)):
                                drifts_bps.append(float(drift))
                            ls = rec.get("latency_s")
                            if isinstance(ls, (int, float)):
                                latency_draws_s.append(float(ls))
                            if rec.get("latency_was_pareto"):
                                pareto_latencies += 1
                        if rec.get("capacity_clipped"):
                            capacity_clips += 1
                    elif ev == "close":
                        # close event writes slippage under a different key
                        # in some paths ("slippage_bps_exit" or similar); be
                        # liberal in what we accept.
                        for k in ("slippage_bps_exit", "slippage_bps"):
                            sb = rec.get(k)
                            if isinstance(sb, (int, float)):
                                exit_slip_bps.append(float(sb))
                                break
                    elif ev == "latency_spike":
                        latency_events += 1
                    elif ev == "capacity_rejected":
                        capacity_rejects += 1

    avg_entry_slip = (sum(entry_slip_bps) / len(entry_slip_bps)
                      if entry_slip_bps else 0.0)
    avg_exit_slip = (sum(exit_slip_bps) / len(exit_slip_bps)
                     if exit_slip_bps else 0.0)
    avg_drift = (sum(drifts_bps) / len(drifts_bps)) if drifts_bps else 0.0
    avg_latency_s = (sum(latency_draws_s) / len(latency_draws_s)
                     if latency_draws_s else 0.0)
    # The "drift" metric is signed against the trade direction —
    # separate the positive tail (market moved for us during the stall —
    # rare) from the negative tail (against us — the normal case).
    adverse_drifts = [d for d in drifts_bps if d < 0]
    favorable_drifts = [d for d in drifts_bps if d > 0]
    return {
        "ticks_logged": ticks,
        "buys_by_tribe": dict(buys),
        "sells_by_tribe": dict(sells),
        "holds_by_tribe": dict(holds),
        "latency_events_in_ledger": latency_events,
        "deferred_opens": deferred_opens,
        "pareto_latencies": pareto_latencies,
        "latency_draw_s_avg": round(avg_latency_s, 3),
        "latency_draw_s_max": round(max(latency_draws_s), 3) if latency_draws_s else 0.0,
        "drift_bps_avg": round(avg_drift, 3),
        "drift_bps_samples": len(drifts_bps),
        "drift_bps_adverse_n": len(adverse_drifts),
        "drift_bps_adverse_avg": (
            round(sum(adverse_drifts) / len(adverse_drifts), 3)
            if adverse_drifts else 0.0
        ),
        "drift_bps_favorable_n": len(favorable_drifts),
        "capacity_clips": capacity_clips,
        "capacity_rejects": capacity_rejects,
        "entry_slip_bps_samples": len(entry_slip_bps),
        "entry_slip_bps_avg": round(avg_entry_slip, 3),
        "entry_slip_bps_max": round(max(entry_slip_bps), 3) if entry_slip_bps else 0.0,
        "exit_slip_bps_samples": len(exit_slip_bps),
        "exit_slip_bps_avg": round(avg_exit_slip, 3),
    }


async def _run_one(
    day: date,
    synth_rate: int,
    seed: int,
    max_ticks: int,
    symbol: str,
    harden: HardenConfig,
    label: str,
) -> dict:
    """Run one sprint with a specific HardenConfig, parse its output."""
    # The sprint reads `settings()` — but `_build_stack` looks at
    # `cfg.get("harden")` to build HardenConfig. We need to override that
    # temporarily. Simplest: monkey-patch the settings() for the duration
    # of this run.
    from src.core import config as cfg_mod

    with tempfile.TemporaryDirectory(prefix=f"harden_{label}_") as tmp:
        outputs = Path(tmp)
        t0 = time.perf_counter()
        # Monkey-patch: wrap settings() so the cfg dict carries the
        # harden block we want. The wrapper copies the underlying dict
        # and overwrites "harden" with the serialized HardenConfig.
        _orig_settings = cfg_mod.settings

        def _patched_settings(*a, **kw):
            cfg = _orig_settings(*a, **kw)
            # Copy + inject; caller mutates the returned dict sometimes,
            # so we return a fresh shallow copy for isolation.
            out = dict(cfg)
            out["harden"] = {
                "enabled": harden.enabled,
                "adv_mult": harden.adv_mult,
                "adv_floor_bps": harden.adv_floor_bps,
                "adv_vol_window_s": harden.adv_vol_window_s,
                "adv_cap_bps": harden.adv_cap_bps,
                "p_spike": harden.p_spike,
                "normal_mean_s": harden.normal_mean_s,
                "spike_scale_s": harden.spike_scale_s,
                "spike_alpha": harden.spike_alpha,
                "spike_cap_s": harden.spike_cap_s,
                "sqrt_k": harden.sqrt_k,
                "sqrt_ref_usd": harden.sqrt_ref_usd,
                "seed": harden.seed,
            }
            return out

        cfg_mod.settings = _patched_settings
        # Also patch the transitive import site inside harness.py —
        # it did `from ..core.config import settings` at module load,
        # so we need to update the reference there too.
        import src.replay.harness as _harness_mod
        _orig_harness_settings = _harness_mod.settings
        _harness_mod.settings = _patched_settings
        try:
            result = await run_sprint(
                day=day, synth_rate=synth_rate, seed=seed,
                max_ticks=max_ticks, outputs_root=outputs, symbol=symbol,
                run_id=f"harden_{label}", variant_id="shock",
            )
        finally:
            cfg_mod.settings = _orig_settings
            _harness_mod.settings = _orig_harness_settings
        wall_s = time.perf_counter() - t0
        dec_path = Path(result.decisions_path)
        # The harness writes the executor's ledger as a sibling directory
        # of decisions.jsonl. That's where open/close/latency rows land.
        ledger_dir = dec_path.parent / "ledger"
        parsed = _parse_run(dec_path, ledger_dir)

    # The executor's harden snapshot is gone with the tempdir, but we
    # baked the ledger-parsed version into `parsed` above. That's enough
    # for the shock report.
    return {
        "label": label,
        "harden_enabled": harden.enabled,
        "harden_cfg": {
            "adv_mult": harden.adv_mult,
            "adv_floor_bps": harden.adv_floor_bps,
            "p_spike": harden.p_spike,
            "spike_scale_s": harden.spike_scale_s,
            "spike_alpha": harden.spike_alpha,
            "sqrt_k": harden.sqrt_k,
            "sqrt_ref_usd": harden.sqrt_ref_usd,
            "seed": harden.seed,
        } if harden.enabled else None,
        "params": {
            "day": str(day), "synth_rate": synth_rate, "seed": seed,
            "max_ticks": max_ticks, "symbol": symbol,
        },
        "ticks": result.ticks,
        "events_consumed": result.events_consumed,
        "trades": result.trades,
        "wins": result.wins,
        "win_rate_pct": (100.0 * result.wins / result.trades
                         if result.trades > 0 else 0.0),
        "start_equity_usd": result.start_equity_usd,
        "end_equity_usd": result.end_equity_usd,
        "equity_delta_usd": result.equity_delta_usd,
        "wall_seconds": wall_s,
        **parsed,
    }


def _fmt_bp(x: float) -> str:
    return f"{x:+.2f} bp" if abs(x) < 100 else f"{x:+.1f} bp"


def _markdown_report(gentle: dict, merc: dict) -> str:
    """Produce the human-readable shock report."""
    lines: list[str] = []
    lines.append("# Phase 10-Harden — Merciless Shock Report")
    lines.append("")
    lines.append(f"Day **{gentle['params']['day']}**  ·  "
                 f"seed **{gentle['params']['seed']}**  ·  "
                 f"ticks **{gentle['ticks']}**  ·  "
                 f"synth rate **{gentle['params']['synth_rate']}**")
    lines.append("")
    lines.append("## Executive summary — Reality Tax")
    lines.append("")
    d_eq = merc["end_equity_usd"] - gentle["end_equity_usd"]
    d_eq_pct = 100.0 * d_eq / gentle["start_equity_usd"]
    lines.append(f"- **Equity delta (Merciless − Gentle):** "
                 f"${d_eq:+,.2f}  ({d_eq_pct:+.3f} % of starting equity)")
    lines.append(f"- **Gentle end-equity:** ${gentle['end_equity_usd']:,.2f}  "
                 f"({gentle['equity_delta_usd']:+,.2f} from start)")
    lines.append(f"- **Merciless end-equity:** ${merc['end_equity_usd']:,.2f}  "
                 f"({merc['equity_delta_usd']:+,.2f} from start)")
    lines.append(f"- **Trade count:** Gentle {gentle['trades']} → "
                 f"Merciless {merc['trades']}  "
                 f"(Δ {merc['trades'] - gentle['trades']:+d})")
    lines.append(f"- **Win rate:** Gentle {gentle['win_rate_pct']:.1f} % → "
                 f"Merciless {merc['win_rate_pct']:.1f} %")
    lines.append("")

    lines.append("## Merciless-layer telemetry")
    lines.append("")
    lines.append(f"- **Latency events logged:** {merc['latency_events_in_ledger']}  "
                 f"(fills deferred >= 1 tick)")
    lines.append(f"- **Deferred fills that landed:** {merc['deferred_opens']}")
    if merc["deferred_opens"] > 0:
        pareto_pct = 100.0 * merc['pareto_latencies'] / merc['deferred_opens']
        lines.append(f"- **Pareto-tail draws:** {merc['pareto_latencies']} "
                     f"({pareto_pct:.1f} % of deferrals)")
        lines.append(f"- **Latency draw avg:** {merc['latency_draw_s_avg']:.2f} s  "
                     f"(max {merc['latency_draw_s_max']:.2f} s)")
    if merc["drift_bps_samples"] > 0:
        lines.append(f"- **Avg decision-to-fill drift** (signed vs. trade "
                     f"direction): {_fmt_bp(merc['drift_bps_avg'])}  "
                     f"over {merc['drift_bps_samples']} deferrals")
        lines.append(f"- **Adverse drifts:** "
                     f"{merc['drift_bps_adverse_n']} "
                     f"(avg {_fmt_bp(merc['drift_bps_adverse_avg'])})")
        lines.append(f"- **Favorable drifts (lucky stall):** "
                     f"{merc['drift_bps_favorable_n']}")
    lines.append(f"- **Entry slippage avg (Merciless):** "
                 f"{merc['entry_slip_bps_avg']:.2f} bp  "
                 f"(max {merc['entry_slip_bps_max']:.2f} bp, "
                 f"n={merc['entry_slip_bps_samples']})")
    lines.append(f"- **Entry slippage avg (Gentle baseline):** "
                 f"{gentle['entry_slip_bps_avg']:.2f} bp  "
                 f"(max {gentle['entry_slip_bps_max']:.2f} bp, "
                 f"n={gentle['entry_slip_bps_samples']})")
    slip_bump = merc['entry_slip_bps_avg'] - gentle['entry_slip_bps_avg']
    slip_pct = (100.0 * slip_bump / gentle['entry_slip_bps_avg']
                if gentle['entry_slip_bps_avg'] > 0 else 0.0)
    lines.append(f"- **Slippage inflation (Merciless - Gentle):** "
                 f"{slip_bump:+.2f} bp  ({slip_pct:+.1f} %)")
    lines.append(f"- **Exit slippage avg:** Gentle "
                 f"{gentle['exit_slip_bps_avg']:.2f} bp -> "
                 f"Merciless {merc['exit_slip_bps_avg']:.2f} bp")
    lines.append(f"- **Capacity clips:** Gentle {gentle['capacity_clips']} -> "
                 f"Merciless {merc['capacity_clips']}  "
                 f"(rejects: {gentle['capacity_rejects']} -> "
                 f"{merc['capacity_rejects']})")
    lines.append("")

    lines.append("## Side-by-side")
    lines.append("")
    lines.append("| Metric | Gentle | Merciless | Delta |")
    lines.append("|---|---:|---:|---:|")

    def _row(name: str, g, m, fmt: str = "{:,.2f}"):
        delta = m - g if isinstance(g, (int, float)) else ""
        gs = fmt.format(g) if isinstance(g, (int, float)) else str(g)
        ms = fmt.format(m) if isinstance(m, (int, float)) else str(m)
        ds = fmt.format(delta) if isinstance(delta, (int, float)) else delta
        lines.append(f"| {name} | {gs} | {ms} | {ds} |")

    _row("Ticks", gentle["ticks"], merc["ticks"], "{:,d}")
    _row("Trades", gentle["trades"], merc["trades"], "{:,d}")
    _row("Wins", gentle["wins"], merc["wins"], "{:,d}")
    _row("Win rate (%)", gentle["win_rate_pct"], merc["win_rate_pct"], "{:.1f}")
    _row("Start equity ($)", gentle["start_equity_usd"],
         merc["start_equity_usd"], "{:,.2f}")
    _row("End equity ($)", gentle["end_equity_usd"],
         merc["end_equity_usd"], "{:,.2f}")
    _row("Equity delta ($)", gentle["equity_delta_usd"],
         merc["equity_delta_usd"], "{:+,.2f}")
    _row("Entry slip avg (bp)", gentle["entry_slip_bps_avg"],
         merc["entry_slip_bps_avg"], "{:.2f}")
    _row("Entry slip max (bp)", gentle["entry_slip_bps_max"],
         merc["entry_slip_bps_max"], "{:.2f}")
    _row("Wall (s)", gentle["wall_seconds"], merc["wall_seconds"], "{:.2f}")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if d_eq_pct < -1.0:
        verdict = "**REGIME DEGRADATION** — the Merciless model bites hard. Strategy tuning needed under live frictions."
    elif d_eq_pct < -0.2:
        verdict = "**MEANINGFUL TAX** — adversarial conditions subtract real value but the strategy still books P&L."
    elif d_eq_pct < 0:
        verdict = "**MINOR TAX** — harden layer produces small degradation; strategy largely robust to simulated frictions."
    else:
        verdict = "**NO DEGRADATION DETECTED** — either the harden layer is too gentle, the strategy is adversarial-proof, or (more likely) we got lucky on this seed. Re-run with more seeds before trusting the signal."
    lines.append(verdict)
    lines.append("")
    lines.append("_Next steps: if the tax is > 1 %, Phase 10-Harden work_")
    lines.append("_continues until the council is robust under Merciless._")
    lines.append("_Target: 7 An-0 days with Merciless=on, Sharpe > 2.0,_")
    lines.append("_zero guard alerts._")
    lines.append("")
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    day = _parse_day(args.day)
    gentle_cfg = HardenConfig(enabled=False)
    merc_cfg = HardenConfig(
        enabled=True,
        adv_mult=args.adv_mult,
        adv_floor_bps=args.adv_floor_bps,
        p_spike=args.p_spike,
        spike_scale_s=args.spike_scale_s,
        spike_alpha=args.spike_alpha,
        sqrt_k=args.sqrt_k,
        seed=args.seed,
    )
    logger.info("[shock] run 1/2: Gentle (harden.enabled=False)")
    gentle = await _run_one(
        day=day, synth_rate=args.synth_rate, seed=args.seed,
        max_ticks=args.max_ticks, symbol=args.symbol,
        harden=gentle_cfg, label="gentle",
    )
    logger.info("[shock] gentle done  end_eq=${:.2f}  trades={}",
                gentle["end_equity_usd"], gentle["trades"])
    logger.info("[shock] run 2/2: Merciless (harden.enabled=True)")
    merc = await _run_one(
        day=day, synth_rate=args.synth_rate, seed=args.seed,
        max_ticks=args.max_ticks, symbol=args.symbol,
        harden=merc_cfg, label="merciless",
    )
    logger.info("[shock] merciless done  end_eq=${:.2f}  trades={}",
                merc["end_equity_usd"], merc["trades"])

    # --- report --------------------------------------------------------
    md = _markdown_report(gentle, merc)
    # Write outputs before printing so a stdout encoding hiccup (Windows
    # cp1252 can't render `−` / unicode dashes) never blocks persistence.
    payload = {
        "gentle": gentle,
        "merciless": merc,
        "delta": {
            "end_equity_usd": merc["end_equity_usd"] - gentle["end_equity_usd"],
            "end_equity_pct_of_start": (
                100.0 * (merc["end_equity_usd"] - gentle["end_equity_usd"])
                / gentle["start_equity_usd"]
            ),
            "trade_count": merc["trades"] - gentle["trades"],
            "win_rate_pct": merc["win_rate_pct"] - gentle["win_rate_pct"],
            "entry_slip_bps": (
                merc["entry_slip_bps_avg"] - gentle["entry_slip_bps_avg"]
            ),
        },
    }

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(serde.dumps_indent(payload))
        logger.info("[shock] wrote JSON snapshot → {}", out)
    if args.md_out:
        md_out = Path(args.md_out)
        md_out.parent.mkdir(parents=True, exist_ok=True)
        md_out.write_text(md, encoding="utf-8")
        logger.info("[shock] wrote markdown report -> {}", md_out)
    # Now attempt stdout — tolerate cp1252 on Windows by swallowing
    # encode errors rather than losing the JSON/MD that already landed.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        print(md)
    except UnicodeEncodeError:
        print(md.encode("ascii", errors="replace").decode("ascii"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--synth-rate", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-ticks", type=int, default=500)
    ap.add_argument("--symbol", default="BTCUSDT")
    # Merciless knob overrides (defaults match the Merciless preset in
    # settings.yaml). Leaving them at CLI defaults reproduces the canonical
    # shock number.
    ap.add_argument("--adv-mult", type=float, default=0.5)
    ap.add_argument("--adv-floor-bps", type=float, default=1.5)
    ap.add_argument("--p-spike", type=float, default=0.02)
    ap.add_argument("--spike-scale-s", type=float, default=1.5)
    ap.add_argument("--spike-alpha", type=float, default=1.3)
    ap.add_argument("--sqrt-k", type=float, default=0.8)
    ap.add_argument("--out", help="JSON snapshot output path (optional)")
    ap.add_argument("--md-out", help="Markdown report output path (optional)")
    args = ap.parse_args()
    try:
        return asyncio.run(_amain(args))
    except Exception as e:
        logger.exception("[shock] failed: {}", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
