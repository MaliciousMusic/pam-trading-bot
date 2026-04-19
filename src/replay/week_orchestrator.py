"""Week-unit experiment orchestrator.

Runs M consecutive days as a single experiment. Equity carries over between
days (end equity of variant V on day N → starting equity of V on day N+1).
All variants run in parallel within each day; days are processed sequentially
so carry-over is exact.

Outputs under `outputs_root / week_<start>_<ts>/`:
  day_<YYYY-MM-DD>/              — one MultiVariantResult per day
    multi_manifest.json
    variants/<variant_id>/
      decisions.jsonl
      manifest.json
  week_manifest.json             — rollup across all days + variants
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import orjson
from loguru import logger

from ..core.config import PROJECT_ROOT, settings
from .orchestrator import MultiVariantResult, VariantSpec, run_multi_variant


@dataclass(slots=True)
class DayResult:
    day: str
    multi: MultiVariantResult
    # per-variant end equity after this day
    end_equities: dict[str, float]


@dataclass(slots=True)
class WeekResult:
    week_id: str
    start_day: str
    days: list[DayResult]
    variants: list[VariantSpec]
    symbol: str
    week_manifest_path: str
    zip_path: str | None = None

    @property
    def all_ok(self) -> bool:
        return all(d.multi.all_ok for d in self.days)

    @property
    def n_days(self) -> int:
        return len(self.days)


async def run_week(
    *,
    start_day: date,
    n_days: int,
    variants: Sequence[VariantSpec],
    week_id: str | None = None,
    outputs_root: Path | None = None,
    cache_root: Path | None = None,
    symbol: str = "BTCUSDT",
    max_concurrent: int | None = None,
    bars_fixture_paths: dict[str, Path] | None = None,
    funding_fixture_paths: dict[str, Path] | None = None,
    emit_report: bool = True,
) -> WeekResult:
    """Run `n_days` consecutive days, carrying equity between them per variant.

    `bars_fixture_paths` / `funding_fixture_paths` are keyed by ISO date string
    for fixture-mode (tests) or None for live cache mode.
    """
    if n_days < 1:
        raise ValueError("n_days must be >= 1")
    if not variants:
        raise ValueError("variants is empty")

    cfg = settings()
    repl = cfg.get("replay", {}) or {}
    if outputs_root is None:
        outputs_root = PROJECT_ROOT / repl.get("outputs_dir", "data/replays")
    if cache_root is None:
        cache_root = PROJECT_ROOT / repl.get("cache_dir", "data")
    outputs_root = Path(outputs_root)
    cache_root = Path(cache_root)

    if week_id is None:
        week_id = (
            f"week_{start_day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"
        )
    week_root = outputs_root / week_id
    week_root.mkdir(parents=True, exist_ok=True)

    logger.info(
        "[week] id={}  start={}  n_days={}  n_variants={}",
        week_id, start_day.isoformat(), n_days, len(variants),
    )

    # Carry-over equity: {variant_id: float}  (None = use config default)
    carry: dict[str, float | None] = {v.variant_id: None for v in variants}
    day_results: list[DayResult] = []

    for i in range(n_days):
        day = start_day + timedelta(days=i)
        day_str = day.isoformat()
        run_id = f"{week_id}/day_{day_str}"

        # Build starting_equities from carry, skipping None (first day)
        starting_equities = {
            vid: eq for vid, eq in carry.items() if eq is not None
        } or None

        bfp = (bars_fixture_paths or {}).get(day_str)
        ffp = (funding_fixture_paths or {}).get(day_str)

        logger.info(
            "[week] day {}/{} = {}  carry={}",
            i + 1, n_days, day_str,
            {k: f"${v:,.0f}" for k, v in (starting_equities or {}).items()},
        )

        multi = await run_multi_variant(
            day=day,
            variants=variants,
            run_id=run_id,
            outputs_root=week_root,
            cache_root=cache_root,
            symbol=symbol,
            max_concurrent=max_concurrent,
            bars_fixture_path=bfp,
            funding_fixture_path=ffp,
            emit_report=False,
            starting_equities=starting_equities,
        )

        # Collect end equities for the next day's carry
        end_equities: dict[str, float] = {}
        for outcome in multi.variants:
            if outcome.result is not None:
                end_equities[outcome.spec.variant_id] = outcome.result.end_equity_usd
            elif outcome.spec.variant_id in carry and carry[outcome.spec.variant_id] is not None:
                end_equities[outcome.spec.variant_id] = carry[outcome.spec.variant_id]  # type: ignore[assignment]
        carry = {v.variant_id: end_equities.get(v.variant_id) for v in variants}

        day_results.append(DayResult(day=day_str, multi=multi, end_equities=end_equities))

    # Build week manifest
    week_manifest = {
        "week_id": week_id,
        "start_day": start_day.isoformat(),
        "n_days": n_days,
        "symbol": symbol,
        "variants": [{"variant_id": v.variant_id, "synth_rate": v.synth_rate, "seed": v.seed}
                     for v in variants],
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
        "all_ok": all(d.multi.all_ok for d in day_results),
        "days": [
            {
                "day": dr.day,
                "run_id": dr.multi.run_id,
                "all_ok": dr.multi.all_ok,
                "variants": [
                    {
                        "variant_id": o.spec.variant_id,
                        "exit_code": o.exit_code,
                        "start_equity_usd": o.result.start_equity_usd if o.result else None,
                        "end_equity_usd": o.result.end_equity_usd if o.result else None,
                        "equity_delta_usd": o.result.equity_delta_usd if o.result else None,
                        "trades": o.result.trades if o.result else None,
                        "wins": o.result.wins if o.result else None,
                    }
                    for o in dr.multi.variants
                ],
            }
            for dr in day_results
        ],
    }
    week_manifest_path = week_root / "week_manifest.json"
    week_manifest_path.write_bytes(orjson.dumps(week_manifest, option=orjson.OPT_INDENT_2))
    logger.info("[week] wrote {}", week_manifest_path)

    zip_path: str | None = None
    if emit_report and all(d.multi.all_ok for d in day_results):
        try:
            from .report_week import build_week_zip
            zp = build_week_zip(week_root, week_manifest)
            zip_path = str(zp)
            logger.info("[week] report zip -> {}", zp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[week] report build failed: {}", exc)

    return WeekResult(
        week_id=week_id,
        start_day=start_day.isoformat(),
        days=day_results,
        variants=list(variants),
        symbol=symbol,
        week_manifest_path=str(week_manifest_path),
        zip_path=zip_path,
    )
