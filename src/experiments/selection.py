"""Selection cut — rank variants and keep the top half.

Fitness formula (Phase 13 roadmap):

    fitness = sharpe · sign(equity_growth) · (1 − MDD)

where:
    sharpe   = mean_per_trade_pnl / stdev_per_trade_pnl × √n_trades
    growth   = end_equity − start_equity
    MDD      = max_drawdown_frac of cumulative-PnL curve, in [0, 1]

The `sign(growth)` factor makes a negative-edge variant with high Sharpe
(consistent losses) score *negative* — survival means making money, not
just being predictable.

Inputs are read from the per-variant ledger JSONL (close events) and the
manifest (start/end equity). When the ledger is empty (zero trades), the
variant is given a fitness of 0 — it didn't earn its way out of either bin.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import orjson


@dataclass(slots=True)
class VariantFitness:
    variant_id: str
    fitness: float
    sharpe: float
    equity_delta_usd: float
    max_drawdown_frac: float
    trades: int


def _iter_close_events(ledger_dir: Path):
    """Yield close-event dicts from every JSONL under `ledger_dir`."""
    if not ledger_dir.exists():
        return
    for path in sorted(ledger_dir.glob("*.jsonl")):
        with path.open("rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if rec.get("event") == "close":
                    yield rec


def _max_drawdown_frac(cum_pnls: list[float], start_equity: float) -> float:
    """Peak-to-trough max-drawdown as a fraction of equity at peak.

    Walks the equity curve (start_equity + cumulative PnL) tracking the
    running peak; the deepest peak-to-trough drop divided by the peak
    equity is the answer. Returns 0.0 when the curve never declined.
    """
    if start_equity <= 0:
        return 0.0
    peak = start_equity
    max_dd = 0.0
    for pnl in cum_pnls:
        eq = start_equity + pnl
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def fitness_from_ledger(
    *,
    variant_id: str,
    ledger_dir: Path,
    start_equity_usd: float,
    end_equity_usd: float,
) -> VariantFitness:
    """Compute Sharpe + MDD + composite fitness from a variant's ledger.

    The ledger has one JSONL per session/run; every "close" event has a
    `net_usd` (post-friction PnL on that trade). We accumulate them into
    a curve, compute the per-trade Sharpe-like ratio, and combine with
    drawdown + the directional sign to get the survival score.

    Variants with <2 trades get sharpe=0 and fitness=0 — too few data
    points to score honestly.
    """
    pnls: list[float] = []
    cum: list[float] = []
    running = 0.0
    for rec in _iter_close_events(ledger_dir):
        try:
            net = float(rec.get("net_usd", 0.0))
        except (TypeError, ValueError):
            net = 0.0
        pnls.append(net)
        running += net
        cum.append(running)

    n = len(pnls)
    delta = end_equity_usd - start_equity_usd

    if n < 2:
        return VariantFitness(
            variant_id=variant_id,
            fitness=0.0,
            sharpe=0.0,
            equity_delta_usd=delta,
            max_drawdown_frac=0.0,
            trades=n,
        )

    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    sd = math.sqrt(var)
    sharpe = (mean / sd) * math.sqrt(n) if sd > 0 else 0.0

    mdd = _max_drawdown_frac(cum, start_equity_usd)
    sign = 1.0 if delta >= 0 else -1.0
    fitness = sharpe * sign * (1.0 - mdd)

    return VariantFitness(
        variant_id=variant_id,
        fitness=fitness,
        sharpe=sharpe,
        equity_delta_usd=delta,
        max_drawdown_frac=mdd,
        trades=n,
    )


def rank_variants(scores: list[VariantFitness]) -> list[VariantFitness]:
    """Highest fitness first. Stable on equality (preserves input order)."""
    return sorted(scores, key=lambda s: s.fitness, reverse=True)


def cull_bottom_half(
    scores: list[VariantFitness], *, keep_fraction: float = 0.5,
    always_keep_seed_bank: bool = True,
) -> list[VariantFitness]:
    """Keep the top `keep_fraction` of variants by fitness.

    `always_keep_seed_bank` (default true) forces `variant_0` to survive
    even if its fitness lands in the bottom half — that's the whole point
    of the seed bank (immutable baseline; can't be culled).
    """
    if not scores:
        return []
    ranked = rank_variants(scores)
    n_keep = max(1, int(round(len(ranked) * keep_fraction)))
    survivors = list(ranked[:n_keep])
    if always_keep_seed_bank:
        from .mutation import SEED_BANK_VARIANT_ID
        if not any(s.variant_id == SEED_BANK_VARIANT_ID for s in survivors):
            sb = next((s for s in ranked if s.variant_id == SEED_BANK_VARIANT_ID), None)
            if sb is not None:
                survivors.append(sb)
    return survivors
