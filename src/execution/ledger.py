"""Append-only JSONL ledger — durable audit trail for the day.

One file per UTC day under data/ledger/YYYY-MM-DD.jsonl. Every
execution event AND every voting tick is persisted so the audit layer
can reconstruct what each tribe said and what happened next.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from ..core.config import PROJECT_ROOT


LEDGER_DIR = PROJECT_ROOT / "data" / "ledger"


class Ledger:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or LEDGER_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, ts: datetime) -> Path:
        return self.dir / f"{ts.astimezone(timezone.utc).strftime('%Y-%m-%d')}.jsonl"

    def write(self, record: dict[str, Any]) -> None:
        ts = record.get("ts")
        if isinstance(ts, datetime):
            record = {**record, "ts": ts.astimezone(timezone.utc).isoformat()}
            path = self._path_for(ts)
        else:
            path = self._path_for(datetime.now(timezone.utc))
        line = orjson.dumps(record, option=orjson.OPT_SERIALIZE_NUMPY) + b"\n"
        with path.open("ab") as f:
            f.write(line)
