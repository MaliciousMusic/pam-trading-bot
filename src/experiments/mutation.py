"""Mutation engine — generate child variant configs from parents.

The unit of mutation is a *variant directory* containing a full set of
17 (or N) tribe YAMLs. A mutation produces a sibling directory with the
same set of files, each one read, jittered in-place on selected numeric
fields, and re-written. Categorical fields (strategy.family, ids,
timezone labels, jurisdiction lists) never mutate.

Default mutation magnitude is **±5% Gaussian** per field — the roadmap's
"small drift, not reinvention" rule. Bigger jumps would force the
selection cut to do too much work; smaller jumps don't cover enough
search space to matter inside a week.

Mutable fields (numeric, per-tribe):
    flow.weight                            # pool weight overlay
    flow.activation_rate                   # how often the tribe speaks
    flow.default_conviction                # how loudly when it does
    flow.cooldown_seconds                  # post-vote silence
    strategy.params.<numeric>              # everything not in BLACKLIST
    personality.risk_tolerance             # in [0,1]
    personality.fomo                       # in [0,1]
    personality.panic                      # in [0,1]
    personality.patience                   # in [0,1]
    personality.herding                    # in [0,1]
    personality.reaction_mu_seconds        # >= 0
    personality.reaction_sigma_seconds     # >= 0
    indicators.<name>.weight               # in [0, ~1.5]
    indicator_weight                       # in [0, 1]
    indicator_regime_range                 # in [0, ~0.5]

Bounded fields are clamped after the jitter so a long-tail draw can't
push `risk_tolerance=0.99` to 1.04 → invalid spec.
"""
from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger

from .lineage import Lineage, write_lineage

DEFAULT_MUTATION_SIGMA: float = 0.05  # ±5% Gaussian
SEED_BANK_VARIANT_ID: str = "variant_0"

# Field paths that are categorical or otherwise off-limits even when their
# raw value happens to be numeric. The runtime would either crash or silently
# misbehave if these were jittered.
_CATEGORICAL_PARAMS: set[str] = {
    "id",
    "name",
    "description",
    "strategy.family",
    "strategy.style_notes",
    "circadian.timezone_label",
    "demographics.jurisdiction",
    "demographics.experience_years",   # list, mutating breaks shape
    "demographics.capital_band_usd",   # list, ditto
    "attention.kinds",
    "attention.tags",
    "attention.ignored_tags",
    "software.platforms",
    "software.tools",
    "software.charting",
    "software.order_types_used",
    "software.data_feeds",
    "hardware.tier",
    "hardware.uptime",
    "circadian.presence_by_hour_utc",  # 24-element vector, leave alone
}

# Fields where a jittered value must stay inside a known interval. Drawn
# from each spec's documented contract.
_BOUNDED_FIELDS: dict[str, tuple[float, float]] = {
    "personality.risk_tolerance": (0.0, 1.0),
    "personality.fomo": (0.0, 1.0),
    "personality.panic": (0.0, 1.0),
    "personality.patience": (0.0, 1.0),
    "personality.herding": (0.0, 1.0),
    "personality.reaction_mu_seconds": (0.0, 60.0),
    "personality.reaction_sigma_seconds": (0.0, 60.0),
    "flow.weight": (0.0, 1.0),
    "flow.activation_rate": (0.0, 1.0),
    "flow.default_conviction": (0.0, 1.0),
    "flow.cooldown_seconds": (0.0, 3600.0),
    "indicator_weight": (0.0, 1.0),
    "indicator_regime_range": (0.0, 1.0),
    "disposition.idle_bias": (-1.0, 1.0),
    "disposition.idle_conviction": (0.0, 1.0),
}


@dataclass(slots=True)
class MutationResult:
    variant_id: str
    variant_dir: Path
    parent_variant_id: str
    mutated_fields: dict[str, dict[str, float]] = field(default_factory=dict)
    rng_seed: int = 0


def _is_numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _walk(prefix: str, node, out: list[tuple[str, list, str | int]]) -> None:
    """Collect (dotted_path, container, key) for every numeric leaf.

    Container + key are kept so mutation can write back in place without
    re-walking the structure. Lists are skipped (they're either categorical
    enumerations or fixed-shape vectors like presence_by_hour_utc).
    """
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            if path in _CATEGORICAL_PARAMS:
                continue
            if isinstance(v, dict):
                _walk(path, v, out)
            elif _is_numeric(v):
                out.append((path, node, k))
            # lists, strings, bools, None → skip
    # top-level shouldn't be a list


def _clamp(path: str, value: float) -> float:
    if path in _BOUNDED_FIELDS:
        lo, hi = _BOUNDED_FIELDS[path]
        if value < lo:
            return lo
        if value > hi:
            return hi
    return value


def mutate_variant_dir(
    *,
    parent_dir: Path,
    out_dir: Path,
    rng: random.Random,
    sigma: float = DEFAULT_MUTATION_SIGMA,
    parent_variant_id: str = "",
    fields_per_tribe: int = 2,
) -> MutationResult:
    """Clone `parent_dir`'s tribe YAMLs into `out_dir` with Gaussian jitter.

    For each tribe file, picks `fields_per_tribe` random numeric leaves
    and multiplies each by `1 + N(0, sigma)`, clamped to documented
    bounds. Categorical fields are passed through verbatim.

    Returns a `MutationResult` enumerating every (path, from, to) so the
    lineage record can pinpoint exactly which knobs moved.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    mutated: dict[str, dict[str, float]] = {}

    yaml_files = sorted(parent_dir.glob("*.yaml"))
    if not yaml_files:
        raise FileNotFoundError(f"no tribe YAMLs in {parent_dir}")

    for src in yaml_files:
        if src.name == "lineage.yaml":
            continue  # never carry parent's lineage forward
        with src.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            shutil.copy2(src, out_dir / src.name)
            continue

        tribe_id = data.get("id", src.stem)
        leaves: list[tuple[str, list, str | int]] = []
        _walk("", data, leaves)

        # Bypass tribes with no mutable fields (shouldn't happen with our
        # current spec set but the safety check is cheap).
        if not leaves:
            with (out_dir / src.name).open("w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            continue

        n_pick = min(fields_per_tribe, len(leaves))
        chosen = rng.sample(leaves, n_pick)
        for path, container, key in chosen:
            old = container[key]
            jitter = 1.0 + rng.gauss(0.0, sigma)
            new = old * jitter
            new = _clamp(path, new)
            # Preserve int type for ints; floats stay float.
            if isinstance(old, int) and not isinstance(old, bool):
                new = int(round(new))
            else:
                new = float(new)
            container[key] = new
            mutated[f"{tribe_id}.{path}"] = {"from": old, "to": new}

        with (out_dir / src.name).open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    return MutationResult(
        variant_id=out_dir.name,
        variant_dir=out_dir,
        parent_variant_id=parent_variant_id,
        mutated_fields=mutated,
        rng_seed=int(rng.random() * 2**32),  # capture rng position for reproducibility
    )


def spawn_generation(
    *,
    generation_root: Path,
    seed_bank_dir: Path,
    parent_dirs: list[Path],
    n_children: int = 6,
    sigma: float = DEFAULT_MUTATION_SIGMA,
    rng_seed: int | None = None,
    generation_index: int = 1,
    parent_run_id: str | None = None,
    fields_per_tribe: int = 2,
) -> list[MutationResult]:
    """Build the next generation under `generation_root/`.

    Layout produced:

        <generation_root>/
            variant_0/                   ← seed-bank baseline (copied verbatim)
                <tribe>.yaml ...
                lineage.yaml             ← marks generation, no parent
            variant_1/                   ← child of parent_dirs[0]
                <tribe>.yaml ...
                lineage.yaml             ← parent + mutated_fields
            variant_2/                   ← child of parent_dirs[1]
                ...
            variant_<n_children>/        ← last child
                ...

    The seed bank ALWAYS occupies `variant_0` and ALWAYS contains an
    unmutated copy of `seed_bank_dir`. Phase 13's drift detector (see
    `seed_bank.check_drift`) compares the evolved cohort's median Sharpe
    against `variant_0`'s — when the cohort underperforms baseline two
    weeks running, the report flags `GENETIC_DRIFT`.
    """
    if not parent_dirs:
        raise ValueError("spawn_generation: parent_dirs is empty")
    if n_children < 1:
        raise ValueError("spawn_generation: n_children must be >= 1")
    generation_root.mkdir(parents=True, exist_ok=True)

    # 1) Seed-bank slot (variant_0) — always a verbatim copy of the baseline.
    sb_out = generation_root / SEED_BANK_VARIANT_ID
    if sb_out.exists():
        shutil.rmtree(sb_out)
    shutil.copytree(seed_bank_dir, sb_out)
    write_lineage(sb_out, Lineage(
        variant_id=SEED_BANK_VARIANT_ID,
        generation=generation_index,
        parent_variant_id=None,
        parent_generation=None,
        parent_run_id=None,
        mutated_fields={},
        rng_seed=None,
        notes="seed-bank baseline (immutable)",
    ))

    # 2) Children — round-robin through parents so the gene pool stays diverse.
    rng = random.Random(rng_seed)
    results: list[MutationResult] = []
    for i in range(1, n_children + 1):
        parent = parent_dirs[(i - 1) % len(parent_dirs)]
        out_dir = generation_root / f"variant_{i}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        # Each child gets its own derived RNG so the same parent twice
        # isn't byte-identical.
        child_rng = random.Random(rng.randrange(2**32))
        res = mutate_variant_dir(
            parent_dir=parent,
            out_dir=out_dir,
            rng=child_rng,
            sigma=sigma,
            parent_variant_id=parent.name,
            fields_per_tribe=fields_per_tribe,
        )
        write_lineage(out_dir, Lineage(
            variant_id=res.variant_id,
            generation=generation_index,
            parent_variant_id=res.parent_variant_id,
            parent_generation=generation_index - 1,
            parent_run_id=parent_run_id,
            mutated_fields=res.mutated_fields,
            rng_seed=res.rng_seed,
            notes=f"σ={sigma:+.2%} Gaussian on {fields_per_tribe} fields/tribe",
        ))
        results.append(res)

    logger.info(
        "[evo] generation {} → {} children + seed bank under {}",
        generation_index, n_children, generation_root,
    )
    return results
