"""Phase 13 — Evolutionary variant search.

Promotes the existing hand-picked-variant week experiment (Phase 9) into a
mutation + selection + seed-bank loop. Within-tribe mutation only — no
cross-tribe mating. The seed bank guards against drift by keeping
`variant_0` as an immutable snapshot of the baseline council that always
runs alongside the evolved cohort.

Public API:

    from src.experiments import (
        spawn_generation,         # mutation: parents -> children
        rank_variants,            # selection: rank by fitness
        cull_bottom_half,         # selection: top-K survivors
        write_lineage,            # lineage.yaml writer
        load_lineage,             # lineage.yaml reader
        check_drift,              # variant_0 vs evolved cohort comparison
        SEED_BANK_VARIANT_ID,
    )

The evolution loop is *not* a single function — it's the composition of
`spawn_generation()` over time, with the seed bank kept constant. Wire it
into your scheduler of choice (week-end hook, manual CLI, cron) — there's
no "evolution daemon" baked in.
"""
from __future__ import annotations

from .lineage import Lineage, load_lineage, write_lineage
from .mutation import (
    DEFAULT_MUTATION_SIGMA,
    MutationResult,
    SEED_BANK_VARIANT_ID,
    mutate_variant_dir,
    spawn_generation,
)
from .seed_bank import (
    DriftStatus,
    check_drift,
    ensure_seed_bank,
    is_seed_bank_variant,
)
from .selection import (
    VariantFitness,
    cull_bottom_half,
    fitness_from_ledger,
    rank_variants,
)

__all__ = [
    "DEFAULT_MUTATION_SIGMA",
    "DriftStatus",
    "Lineage",
    "MutationResult",
    "SEED_BANK_VARIANT_ID",
    "VariantFitness",
    "check_drift",
    "cull_bottom_half",
    "ensure_seed_bank",
    "fitness_from_ledger",
    "is_seed_bank_variant",
    "load_lineage",
    "mutate_variant_dir",
    "rank_variants",
    "spawn_generation",
    "write_lineage",
]
