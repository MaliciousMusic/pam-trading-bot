"""Evolutionary branching scaffold.

End-of-day hook that takes the current tribe set, clones it into N
variants, and mutates one parameter per tribe per variant within a
bounded range. Variants get their own YAML dir; over subsequent days
their scores are compared and winners survive.

Wired at the interface level only — the evolution LOOP runs once we
have multiple days of audit data (causality), not from day one. This
respects the "no MVP but layer in dependency order" rule.
"""
from __future__ import annotations

import random
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from ..core.config import CONFIG_DIR
from ..tribes.spec import TribeSpec


VARIANTS_DIR = CONFIG_DIR / "tribes_variants"


@dataclass(slots=True)
class MutationRule:
    """How to perturb a single strategy.params field."""
    path: str                 # e.g. "strategy.params.min_net_flow_usd"
    lo_multiplier: float = 0.7
    hi_multiplier: float = 1.3


# Rules are intentionally modest — we want small drift, not reinvention.
DEFAULT_RULES: dict[str, list[MutationRule]] = {
    "app_fomo":           [MutationRule("strategy.params.min_move_bps"),
                           MutationRule("strategy.params.price_window_seconds", 0.5, 1.5)],
    "momentum_chaser":    [MutationRule("strategy.params.min_net_flow_usd"),
                           MutationRule("strategy.params.liq_amplifier", 0.5, 2.0)],
    "breakout_trader":    [MutationRule("strategy.params.break_buffer_bps", 0.5, 2.0),
                           MutationRule("strategy.params.range_window_seconds", 0.5, 1.5)],
    "trend_follower":     [MutationRule("strategy.params.min_persistent_move_bps"),
                           MutationRule("strategy.params.funding_guard", 0.3, 2.0)],
    "mean_reverter_mm":   [MutationRule("strategy.params.max_imbalance_usd")],
    "liquidation_hunter": [MutationRule("strategy.params.min_liq_usd")],
    "funding_fader":      [MutationRule("strategy.params.extreme_funding_rate")],
    "news_reactor":       [],
}


def _get(obj: dict, path: str):
    for k in path.split("."):
        obj = obj[k]
    return obj


def _set(obj: dict, path: str, value) -> None:
    parts = path.split(".")
    for k in parts[:-1]:
        obj = obj[k]
    obj[parts[-1]] = value


def spawn_variants(tribes: list[TribeSpec], n_variants: int = 3,
                   seed: int | None = None) -> list[list[TribeSpec]]:
    """Return n_variants independent mutated copies of the tribe set."""
    rng = random.Random(seed)
    variants: list[list[TribeSpec]] = []
    for _ in range(n_variants):
        variant: list[TribeSpec] = []
        for spec in tribes:
            rules = DEFAULT_RULES.get(spec.strategy.family, [])
            if not rules:
                variant.append(spec.model_copy(deep=True))
                continue
            data = spec.model_dump()
            rule = rng.choice(rules)
            try:
                cur = _get(data, rule.path)
                if isinstance(cur, (int, float)):
                    mul = rng.uniform(rule.lo_multiplier, rule.hi_multiplier)
                    new = type(cur)(cur * mul) if not isinstance(cur, bool) else cur
                    _set(data, rule.path, new)
            except (KeyError, TypeError):
                pass
            variant.append(TribeSpec.model_validate(data))
        variants.append(variant)
    return variants


def write_variants(variants: list[list[TribeSpec]], generation_tag: str) -> list[Path]:
    """Persist each variant to config/tribes_variants/<tag>/variant_K/*.yaml."""
    base = VARIANTS_DIR / generation_tag
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, variant in enumerate(variants):
        vdir = base / f"variant_{i}"
        vdir.mkdir(exist_ok=True)
        for spec in variant:
            path = vdir / f"{spec.id}.yaml"
            with path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(spec.model_dump(), f, sort_keys=False)
            written.append(path)
    logger.info("wrote {} variant files under {}", len(written), base)
    return written
