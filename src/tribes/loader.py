"""Load tribe specs from config/tribes/*.yaml into runtime Tribes."""
from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger

from ..core.config import CONFIG_DIR
from .spec import TribeSpec
from .strategies import available as available_strategies
from .tribe import Tribe


TRIBES_DIR = CONFIG_DIR / "tribes"


def load_all() -> list[Tribe]:
    if not TRIBES_DIR.exists():
        logger.warning("no tribes dir at {}", TRIBES_DIR)
        return []
    registered = set(available_strategies())
    tribes: list[Tribe] = []
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
        tribes.append(Tribe(spec))
        logger.info("loaded tribe {:<32} weight={:.3f} strategy={}",
                    spec.id, spec.flow.weight, spec.strategy.family)
    return tribes
