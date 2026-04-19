"""Seed bank — variant_0 immutability + drift detection.

The seed bank is the hard reference point the evolutionary loop is
measured against. It's nothing more than a designated directory holding
the unmutated baseline tribe set, copied verbatim into `variant_0` of
every generation. Two invariants this module enforces:

  1. **Immutability**: `ensure_seed_bank()` re-copies the baseline files
     before each generation runs. If a previous run accidentally edited
     `variant_0` (e.g. a mutation engine bug), the next generation
     overwrites with the canonical baseline rather than propagating drift.

  2. **Drift detection**: `check_drift()` compares `variant_0`'s fitness
     against the median of the evolved cohort. Two consecutive weeks of
     evolved-cohort underperformance → `GENETIC_DRIFT` flag, surfaced
     in the report. The "two weeks" rule is conservative: one bad week
     can be regime noise; two in a row is a pattern.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .mutation import SEED_BANK_VARIANT_ID
from .selection import VariantFitness


@dataclass(slots=True)
class DriftStatus:
    drifted_this_week: bool
    drifted_last_week: bool
    seed_bank_fitness: float
    cohort_median_fitness: float
    flag: str  # "OK" | "WATCH" | "GENETIC_DRIFT"

    @property
    def is_drifting(self) -> bool:
        return self.flag == "GENETIC_DRIFT"


def is_seed_bank_variant(variant_id: str) -> bool:
    return variant_id == SEED_BANK_VARIANT_ID


def ensure_seed_bank(
    *, generation_root: Path, baseline_dir: Path,
) -> Path:
    """Re-copy `baseline_dir` into `generation_root/variant_0`.

    Idempotent — call before every generation runs to guarantee the
    baseline is exactly what the user committed in `config/tribes/`.
    Returns the path to the now-pristine `variant_0` directory.
    """
    dest = generation_root / SEED_BANK_VARIANT_ID
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(baseline_dir, dest)
    return dest


def check_drift(
    *,
    this_week: list[VariantFitness],
    last_week_drifted: bool = False,
) -> DriftStatus:
    """Compare seed-bank to evolved-cohort fitness for the current week.

    `last_week_drifted` should be the result of running this same check on
    the previous week (typically read from the previous report's manifest).
    The two-week rule: WATCH on first underperformance, GENETIC_DRIFT on
    the second consecutive one.
    """
    sb = next((s for s in this_week if is_seed_bank_variant(s.variant_id)), None)
    cohort = [s for s in this_week if not is_seed_bank_variant(s.variant_id)]
    if sb is None or not cohort:
        return DriftStatus(
            drifted_this_week=False,
            drifted_last_week=last_week_drifted,
            seed_bank_fitness=sb.fitness if sb else 0.0,
            cohort_median_fitness=0.0,
            flag="OK",
        )

    sorted_cohort = sorted(s.fitness for s in cohort)
    n = len(sorted_cohort)
    median = (
        sorted_cohort[n // 2] if n % 2
        else 0.5 * (sorted_cohort[n // 2 - 1] + sorted_cohort[n // 2])
    )
    drifted_now = sb.fitness > median  # baseline beats the cohort median

    if drifted_now and last_week_drifted:
        flag = "GENETIC_DRIFT"
    elif drifted_now:
        flag = "WATCH"
    else:
        flag = "OK"

    return DriftStatus(
        drifted_this_week=drifted_now,
        drifted_last_week=last_week_drifted,
        seed_bank_fitness=sb.fitness,
        cohort_median_fitness=median,
        flag=flag,
    )
