"""Metric extraction from a completed replay run directory.

Reads multi_manifest.json (orchestrator runs) or a bare variants/ tree
(single-variant CLI runs) and returns a RunMetrics object ready for
rendering. Decisions are streamed so even large full-day logs don't
blow up memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import serde
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
    # Phase 11 — realistic execution accounting. Surfaces the cost of
    # friction so the user can compare frictionless vs realized PnL and
    # see when capacity caps clipped a position. All zero when the run
    # didn't produce any closed trades.
    slippage_usd_total: float = 0.0
    fee_usd_total: float = 0.0
    intended_bps_avg: float = 0.0
    realized_bps_avg: float = 0.0
    bps_drag_avg: float = 0.0  # intended - realized (cost of friction in bps)
    capacity_clipped_count: int = 0
    capacity_rejected_count: int = 0
    frictionless_end_equity: float = 0.0
    frictionless_curve: list[float] = field(default_factory=list)
    # Per-trade table for the report — newest last. Each row carries the
    # raw fields the close ledger event already produces.
    trades_table: list[dict] = field(default_factory=list)
    # Phase 13 — lineage. Populated when the variant directory contains a
    # `lineage.yaml` (i.e. it was spawned by the evolutionary loop). Plain
    # variants from Phase 9 leave these at default.
    lineage_generation: int = 0
    lineage_parent_id: str | None = None
    lineage_mutated_count: int = 0
    lineage_notes: str = ""

    @property
    def equity_delta(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def frictionless_delta(self) -> float:
        if self.frictionless_end_equity == 0.0:
            return 0.0
        return self.frictionless_end_equity - self.start_equity

    @property
    def friction_cost_usd(self) -> float:
        """Dollars left on the table to the venue — fees + slippage."""
        return self.slippage_usd_total + self.fee_usd_total


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


def _load_variant(manifest_path: Path, decisions_path: Path,
                  ledger_dir: Path | None = None,
                  variant_config_dir: Path | None = None) -> VariantMetrics:
    m = serde.loads(manifest_path.read_bytes())

    rows: list[dict] = []
    if decisions_path.exists():
        with decisions_path.open("rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(serde.loads(line))

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

    # Phase 11 — drain the ledger for per-trade friction accounting.
    slip_total = 0.0
    fee_total = 0.0
    intended_bps_sum = 0.0
    realized_bps_sum = 0.0
    n_closed = 0
    capacity_clipped = 0
    capacity_rejected = 0
    frictionless_end = 0.0
    frictionless_curve: list[float] = []
    trades_table: list[dict] = []
    if ledger_dir is not None and ledger_dir.exists():
        for jp in sorted(ledger_dir.glob("*.jsonl")):
            with jp.open("rb") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = serde.loads(line)
                    except Exception:
                        continue
                    kind = ev.get("event")
                    if kind == "close":
                        slip_total += float(ev.get("total_slip_usd", 0.0))
                        fee_total += float(ev.get("total_fee_usd",
                                                  ev.get("fee_usd", 0.0)))
                        intended_bps_sum += float(ev.get("intended_bps", 0.0))
                        realized_bps_sum += float(ev.get("realized_bps", 0.0))
                        frictionless_end = float(ev.get(
                            "frictionless_equity_usd", frictionless_end))
                        frictionless_curve.append(frictionless_end)
                        n_closed += 1
                        trades_table.append({
                            "ts": ev.get("ts"),
                            "side": ev.get("side"),
                            "size_usd": ev.get("size_usd"),
                            "entry_price": ev.get("entry_price"),
                            "fill_price": ev.get("fill_price"),
                            "intended_bps": ev.get("intended_bps", 0.0),
                            "realized_bps": ev.get("realized_bps", 0.0),
                            "entry_slip_bps": ev.get("entry_slip_bps", 0.0),
                            "exit_slip_bps": ev.get("exit_slip_bps", 0.0),
                            "total_slip_usd": ev.get("total_slip_usd", 0.0),
                            "total_fee_usd": ev.get("total_fee_usd",
                                                    ev.get("fee_usd", 0.0)),
                            "net_usd": ev.get("net_usd", 0.0),
                            "reason": ev.get("reason", ""),
                        })
                    elif kind == "open" and bool(ev.get("capacity_clipped")):
                        capacity_clipped += 1
                    elif kind == "capacity_rejected":
                        capacity_rejected += 1
    intended_avg = intended_bps_sum / n_closed if n_closed else 0.0
    realized_avg = realized_bps_sum / n_closed if n_closed else 0.0
    drag_avg = intended_avg - realized_avg
    if frictionless_end == 0.0:
        frictionless_end = float(m.get("end_equity_usd", 0.0))

    # Phase 13 — pull lineage out of the variant's config dir if one was
    # supplied. Lazy-import keeps `experiments` an optional dependency.
    lineage_gen = 0
    lineage_parent: str | None = None
    lineage_mutated = 0
    lineage_notes = ""
    if variant_config_dir is not None and variant_config_dir.exists():
        try:
            from ..experiments.lineage import load_lineage
            lin = load_lineage(variant_config_dir)
            if lin is not None:
                lineage_gen = lin.generation
                lineage_parent = lin.parent_variant_id
                lineage_mutated = len(lin.mutated_fields)
                lineage_notes = lin.notes or ""
        except Exception:  # noqa: BLE001
            pass

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
        slippage_usd_total=slip_total,
        fee_usd_total=fee_total,
        intended_bps_avg=intended_avg,
        realized_bps_avg=realized_avg,
        bps_drag_avg=drag_avg,
        capacity_clipped_count=capacity_clipped,
        capacity_rejected_count=capacity_rejected,
        frictionless_end_equity=frictionless_end,
        frictionless_curve=frictionless_curve,
        trades_table=trades_table,
        lineage_generation=lineage_gen,
        lineage_parent_id=lineage_parent,
        lineage_mutated_count=lineage_mutated,
        lineage_notes=lineage_notes,
    )


def load_run_metrics(run_dir: Path) -> RunMetrics:
    """Load a completed run from its output directory.

    Accepts both orchestrator runs (have multi_manifest.json) and bare
    single-variant runs (variants/ dir only). Returns a RunMetrics instance
    ready for rendering."""
    run_dir = Path(run_dir)
    rollup_path = run_dir / "multi_manifest.json"

    if rollup_path.exists():
        rollup = serde.loads(rollup_path.read_bytes())
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
            # Per-variant ledger lives under <variant>/ledger/ in the
            # standard sprint+watch layout. Older runs may be missing it.
            ledger = vd / "ledger"
            if not ledger.exists():
                ledger = run_dir / "ledger" / vd.name
            # Phase 13 — find this variant's tribes_dir from its manifest;
            # the lineage.yaml lives in that config dir.
            variant_config_dir: Path | None = None
            if manifest.exists():
                m = serde.loads(manifest.read_bytes())
                if not rollup_path.exists():
                    day = m.get("day", day)
                    symbol = m.get("symbol", symbol)
                td = m.get("tribes_dir")
                if td:
                    variant_config_dir = Path(td)
                variant_metrics.append(_load_variant(
                    manifest, decisions,
                    ledger_dir=ledger if ledger.exists() else None,
                    variant_config_dir=variant_config_dir,
                ))

    return RunMetrics(
        run_id=run_id,
        day=day,
        symbol=symbol,
        wall_seconds=wall,
        variants=variant_metrics,
    )
