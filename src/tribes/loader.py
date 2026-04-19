"""Load tribe specs from config/tribes/*.yaml into runtime Tribes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from loguru import logger

from ..core.config import CONFIG_DIR
from .calibration import log_calibration
from .derive import apply_and_log
from .spec import TribeSpec
from .strategies import available as available_strategies
from .tribe import Tribe


TRIBES_DIR = CONFIG_DIR / "tribes"


def _derive_tribe_seed(tribe_id: str, master: int) -> int:
    """Stable per-tribe seed derived from (tribe_id, master_seed).
    Uses sha256 so it's reproducible across processes (unlike Python's
    built-in hash() which is randomized per-interpreter). Enabling or
    disabling a tribe never reshuffles any other tribe's stream."""
    digest = hashlib.sha256(tribe_id.encode("utf-8")).digest()
    tribe_hash = int.from_bytes(digest[:4], "big")
    return (tribe_hash ^ (int(master) & 0xFFFFFFFF)) & 0xFFFFFFFF


def load_all(
    *, rng_seed: int | None = None, tribes_dir: Path | None = None,
) -> list[Tribe]:
    """Load every tribe spec. When `rng_seed` is provided, each tribe
    gets a deterministic per-id seed derived from it — mandatory for
    replay to produce byte-identical decision logs across runs. Live
    behavior (rng_seed=None) keeps each Tribe on a fresh Random().

    `tribes_dir` defaults to the project's `config/tribes/`. Phase 13's
    evolutionary loop points it at a per-variant directory under
    `config/tribes_variants/<gen>/variant_<id>/` so each variant runs its
    own mutated council."""
    src_dir = Path(tribes_dir) if tribes_dir is not None else TRIBES_DIR
    if not src_dir.exists():
        logger.warning("no tribes dir at {}", src_dir)
        return []
    registered = set(available_strategies())
    specs: list[TribeSpec] = []
    for path in sorted(src_dir.glob("*.yaml")):
        # `lineage.yaml` lives in variant dirs and isn't a tribe spec.
        if path.name == "lineage.yaml":
            continue
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        spec = TribeSpec.model_validate(raw)
        if spec.strategy.family not in registered:
            logger.error(
                "tribe {} references unknown strategy {!r}; known={}",
                spec.id, spec.strategy.family, sorted(registered),
            )
            continue
        specs.append(spec)
    # Bottom-up weight derivation: replaces the hand-tuned flow.weight on
    # every spec with a normalized value computed from population × capital
    # × per-tick activation. Logs a before/after table so the override is
    # auditable.
    apply_and_log(specs)
    if rng_seed is None:
        tribes = [Tribe(s) for s in specs]
    else:
        tribes = [Tribe(s, rng_seed=_derive_tribe_seed(s.id, rng_seed)) for s in specs]
    # Phase F companion: log the honest tick-level contribution share
    # (weight × activation × presence). Warns if any cohort dominates
    # or is effectively silent — catches mis-calibrated populations.
    log_calibration(tribes)
    return tribes
