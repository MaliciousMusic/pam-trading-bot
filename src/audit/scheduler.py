"""Daily audit scheduler — runs once per UTC day at 00:00.

Workflow:
  1. Sleep until next UTC midnight.
  2. Audit yesterday's ledger → DailyReport.
  3. Persist the scorecard (human-readable + machine-readable JSON).
  4. Derive audit-driven hypotheses, each an atomic single-parameter
     experiment. Count depends on the audit — may be 0..MAX_VARIANTS.
  5. Spawn a child variant per hypothesis (branching).
  6. Loop.

The scheduler is fire-and-forget; the main engine keeps trading the
parent council while the variants wait on disk. Tournament scoring
across variants happens when we wire the multi-council runner.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core import serde
from loguru import logger

from ..core.config import PROJECT_ROOT
from ..tribes.spec import TribeSpec
from .daily import DailyReport, audit_day, print_report
from .evolution import spawn_from_hypotheses, write_hypothesis_variants
from .hypothesis import council_fitness, generate


SCORECARD_DIR = PROJECT_ROOT / "data" / "audit"


def _yesterday_utc(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def _seconds_until_next_utc_midnight(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds())


def _persist_scorecard(report: DailyReport, fitness: dict, hypotheses: list) -> Path:
    SCORECARD_DIR.mkdir(parents=True, exist_ok=True)
    out = SCORECARD_DIR / f"{report.day}.json"
    payload = {
        "day": report.day,
        "fitness": fitness,    # equity delta is THE primary quality signal
        "trades": report.trades,
        "wins": report.wins,
        "realized_pnl_usd": report.realized_pnl_usd,
        "tribes": {
            tid: {
                "participation": s.participation,
                "hit_rate": s.hit_rate,
                "conviction_weighted_hit_rate": s.conviction_weighted_hit_rate,
                "directional_votes": s.directional_votes,
                "total_votes": s.total_votes,
            }
            for tid, s in report.tribes.items()
        },
        "hypotheses": [
            {
                "tribe_id": h.tribe_id, "field": h.field,
                "current": h.current, "proposed": h.proposed,
                "rationale": h.rationale, "priority": h.priority,
            }
            for h in hypotheses
        ],
    }
    with out.open("wb") as f:
        f.write(serde.dumps_indent(payload))
    return out


class DailyAuditScheduler:
    """Background task. Call `await run(tribes)` inside the main gather."""

    def __init__(self, horizon_seconds: int = 60) -> None:
        self.horizon_seconds = horizon_seconds

    async def run(self, tribes: list[TribeSpec]) -> None:
        logger.info("[audit] scheduler armed — next run at next UTC midnight")
        while True:
            try:
                delay = _seconds_until_next_utc_midnight()
                logger.info("[audit] sleeping {:.0f}s until next cycle", delay)
                await asyncio.sleep(delay)
                await self._cycle(tribes)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[audit] cycle failed: {}", e)
                await asyncio.sleep(300)   # don't hot-loop

    async def _cycle(self, tribes: list[TribeSpec]) -> None:
        day = _yesterday_utc()
        logger.info("[audit] ── running daily audit for {} ──", day)
        report = audit_day(day, horizon_seconds=self.horizon_seconds)
        fitness = council_fitness(report)

        logger.info(
            "[audit] COUNCIL FITNESS  Δequity ${:+.2f} ({:+.2%})  "
            "trades={} win_rate={:.1%}",
            fitness["equity_delta_usd"], fitness["equity_delta_pct"],
            report.trades, report.win_rate,
        )
        print_report(report)

        hypotheses = generate(report, tribes)
        if not hypotheses:
            logger.info("[audit] no actionable hypotheses — generation skipped")
            _persist_scorecard(report, fitness, [])
            return

        logger.info("[audit] generated {} hypothesis/es (atomic single-param changes)",
                    len(hypotheses))
        for i, h in enumerate(hypotheses):
            logger.info("  #{} [{}] {}: {:.4g} → {:.4g}   ({})",
                        i, h.tribe_id, h.field, h.current, h.proposed, h.rationale)

        variants = spawn_from_hypotheses(tribes, hypotheses)
        base = write_hypothesis_variants(variants, generation_tag=day)
        _persist_scorecard(report, fitness, hypotheses)
        logger.info("[audit] variants written under {}", base)
