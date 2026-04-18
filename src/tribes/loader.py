"""Load tribe specs from config/tribes/*.yaml into runtime Tribes."""
from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger

from ..core.config import CONFIG_DIR
from .derive import apply_and_log
from .spec import TribeSpec
from .strategies import available as available_strategies
from .tribe import Tribe


TRIBES_DIR = CONFIG_DIR / "tribes"


def load_all() -> list[Tribe]:
    if not TRIBES_DIR.exists():
        logger.warning("no tribes dir at {}", TRIBES_DIR)
        return []
    registered = set(available_strategies())
    specs: list[TribeSpec] = []
    for path in sorted(TRIBES_DIR.glob("*.yaml")):
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
    return [Tribe(s) for s in specs]
