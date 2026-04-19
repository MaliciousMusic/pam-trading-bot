"""Metric extraction from a completed replay run directory.

Reads multi_manifest.json (orchestrator runs) or a bare variants/ tree
(single-variant CLI runs) and returns a RunMetrics object ready for
rendering. Decisions are streamed so even large full-day logs don't
blow up memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import orjson

N_PLOT = 600  # max time-series points per variant in charts


@dataclass
class VariantMetrics:
    variant_id: str
    synth_rate: int
    seed: int
    ticks: int
    trades: int
    wins: int
    start_equity: float
    end_equity: float
    # Downsampled time series (len <= N_PLOT)
    equity_curve: list[float]
    price_curve: list[float]
    pressure_curve: list[float]
    uncertainty_curve: list[float]
    # Distributions computed over full tick stream
    action_counts: dict[str, int]
    regime_counts: dict[str, int]
    tribe_net: dict[str, float]  # tribe_id -> avg signed conviction

    @property
    def equity_delta(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass
class RunMetrics:
    run_id: str
    day: str
    symbol: str
    wall_seconds: float
    variants: list[VariantMetrics]

    @property
    def is_multi(self) -> bool:
        return len(self.variants) > 1


def _load_variant(manifest_path: Path, decisions_path: Path) -> VariantMetrics:
    m = orjson.loads(manifest_path.read_bytes())

    rows: list[dict] = []
    if decisions_path.exists():
        with decisions_path.open("rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(orjson.loads(line))

    action_counts: dict[str, int] = {}
    regime_counts: dict[str, int] = {}
    tribe_sums: dict[str, float] = {}
    tribe_cnts: dict[str, int] = {}

    for r in rows:
        a = r.get("action", "hold")
        action_counts[a] = action_counts.get(a, 0) + 1
        reg = r.get("regime") or "unknown"
        regime_counts[reg] = regime_counts.get(reg, 0) + 1
        for v in r.get("votes", []):
            tid = v["tribe_id"]
            side = v.get("side", "hold")
            conv = float(v.get("conviction", 0.0))
            signed = conv if side == "buy" else (-conv if side == "sell" else 0.0)
            tribe_sums[tid] = tribe_sums.get(tid, 0.0) + signed
            tribe_cnts[tid] = tribe_cnts.get(tid, 0) + 1

    tribe_net = {tid: tribe_sums[tid] / tribe_cnts[tid]
                 for tid in tribe_sums if tribe_cnts[tid] > 0}

    step = max(1, len(rows) // N_PLOT)
    sampled = rows[::step]

    return VariantMetrics(
        variant_id=m["variant_id"],
        synth_rate=m["synth_rate"],
        seed=m["seed"],
        ticks=m["ticks"],
        trades=m["trades"],
        wins=m["wins"],
        start_equity=m["start_equity_usd"],
        end_equity=m["end_equity_usd"],
        equity_curve=[r["equity_usd"] for r in sampled],
        price_curve=[r["price"] for r in sampled],
        pressure_curve=[r["net_pressure"] for r in sampled],
        uncertainty_curve=[r["uncertainty"] for r in sampled],
        action_counts=action_counts,
        regime_counts=regime_counts,
        tribe_net=tribe_net,
    )


def load_run_metrics(run_dir: Path) -> RunMetrics:
    """Load a completed run from its output directory.

    Accepts both orchestrator runs (have multi_manifest.json) and bare
    single-variant runs (variants/ dir only). Returns a RunMetrics instance
    ready for rendering."""
    run_dir = Path(run_dir)
    rollup_path = run_dir / "multi_manifest.json"

    if rollup_path.exists():
        rollup = orjson.loads(rollup_path.read_bytes())
        run_id = rollup["run_id"]
        day = rollup["day"]
        symbol = rollup["symbol"]
        wall = float(rollup.get("wall_seconds", 0.0))
    else:
        run_id = run_dir.name
        day = "unknown"
        symbol = "BTCUSDT"
        wall = 0.0

    variants_dir = run_dir / "variants"
    variant_metrics: list[VariantMetrics] = []

    if variants_dir.exists():
        for vd in sorted(variants_dir.iterdir()):
            if not vd.is_dir():
                continue
            manifest = vd / "manifest.json"
            decisions = vd / "decisions.jsonl"
            if manifest.exists():
                if not rollup_path.exists():
                    m = orjson.loads(manifest.read_bytes())
                    day = m.get("day", day)
                    symbol = m.get("symbol", symbol)
                variant_metrics.append(_load_variant(manifest, decisions))

    return RunMetrics(
        run_id=run_id,
        day=day,
        symbol=symbol,
        wall_seconds=wall,
        variants=variant_metrics,
    )
