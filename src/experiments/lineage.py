"""Per-variant lineage record.

A `lineage.yaml` lives in every variant directory under
`config/tribes_variants/<gen_id>/variant_<id>/lineage.yaml` and tracks who
spawned this variant, what was mutated, and which generation it belongs
to. This is the audit trail the evolutionary loop walks each week to
attribute Sharpe gains to specific param shifts.

Format:

    variant_id: variant_3
    generation: 4
    parent_variant_id: variant_1     # null for seed-bank/variant_0
    parent_generation: 3             # null for variant_0
    parent_run_id: week_2026-04-13_103022
    mutated_fields:                  # path -> (parent_value, child_value)
      mm_algo.strategy.params.flow_window_seconds:
        from: 30
        to: 31.42
      liquidation_hunter.flow.activation_rate:
        from: 0.12
        to: 0.118
    rng_seed: 838172
    created_at_utc: 2026-04-20T11:34:22Z
    notes: >-
      Free-text — used for "this variant tests hypothesis X" annotations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


@dataclass(slots=True)
class Lineage:
    variant_id: str
    generation: int
    parent_variant_id: str | None = None
    parent_generation: int | None = None
    parent_run_id: str | None = None
    mutated_fields: dict[str, dict[str, float | int | str]] = field(default_factory=dict)
    rng_seed: int | None = None
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def write_lineage(variant_dir: Path, lineage: Lineage) -> Path:
    """Persist `lineage.yaml` inside the variant dir.

    The file is the source of truth for "where did this variant come
    from"; everything else (mutation engine, selection cut, drift check)
    reads it back. Round-trips through dict for stable YAML key order.
    """
    variant_dir.mkdir(parents=True, exist_ok=True)
    path = variant_dir / "lineage.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(lineage.to_dict(), f, sort_keys=False)
    return path


def load_lineage(variant_dir: Path) -> Lineage | None:
    """Read `lineage.yaml` from a variant dir; returns None if missing.

    Pre-Phase-13 variants don't have one — the reader treats absence as
    "untracked legacy variant" rather than an error so old generations
    still load."""
    path = variant_dir / "lineage.yaml"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Lineage(
        variant_id=raw["variant_id"],
        generation=int(raw.get("generation", 0)),
        parent_variant_id=raw.get("parent_variant_id"),
        parent_generation=raw.get("parent_generation"),
        parent_run_id=raw.get("parent_run_id"),
        mutated_fields=raw.get("mutated_fields", {}) or {},
        rng_seed=raw.get("rng_seed"),
        created_at_utc=raw.get("created_at_utc", ""),
        notes=raw.get("notes", "") or "",
    )
