"""Config loader. Reads YAML from the config/ directory at project root."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def settings() -> dict:
    return load("settings")


def data_sources() -> dict:
    return load("data_sources")
