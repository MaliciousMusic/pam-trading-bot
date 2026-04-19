"""Spoof event injector for adversarial replay (Phase 14).

Loads a fixture from `tests/fixtures/spoofs/*.json`, anchors it to a
moment in the base event stream, and yields a merged stream where the
spoof events are interleaved at their `(anchor_ts + offset_s)` timestamps.

A few payload shortcuts let fixtures stay portable across days/prices:

  - For `kind == "trade"`, `payload.price_pct_from_anchor` resolves to
    `payload.price = anchor_price * (1 + pct/100)`.
  - For `kind == "book"`, `payload.best_bid_pct_from_anchor` /
    `payload.best_ask_pct_from_anchor` resolve to absolute bid/ask
    around the anchor price; `mid` and `spread_bps` are derived.

The anchor price is the most recent `kind == "trade"` event in the base
stream whose ts ≤ anchor_ts (or the first one after, if the spoof fires
before any base trade — rare).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from ..core.types import Event


@dataclass
class SpoofFixture:
    """Loaded JSON fixture describing a single adversarial scenario."""
    id: str
    description: str
    bait_side: str            # "buy" | "sell"
    real_outcome_bps: float
    scoring_window_s: int
    events: list[dict] = field(default_factory=list)


def load_fixture(path: Path) -> SpoofFixture:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return SpoofFixture(
        id=str(raw["id"]),
        description=str(raw.get("description", "")),
        bait_side=str(raw["bait_side"]).lower(),
        real_outcome_bps=float(raw.get("real_outcome_bps", 0.0)),
        scoring_window_s=int(raw.get("scoring_window_s", 300)),
        events=list(raw.get("events", [])),
    )


def list_fixtures(directory: Path) -> list[SpoofFixture]:
    return [load_fixture(p) for p in sorted(Path(directory).glob("*.json"))]


def _resolve_payload(payload: dict, anchor_price: float | None) -> dict:
    """Resolve `*_pct_from_anchor` shortcuts into absolute prices."""
    out = dict(payload)
    if anchor_price is None:
        # Strip the shortcuts so downstream consumers don't see partial state.
        out.pop("price_pct_from_anchor", None)
        out.pop("best_bid_pct_from_anchor", None)
        out.pop("best_ask_pct_from_anchor", None)
        return out
    if "price_pct_from_anchor" in out:
        pct = float(out.pop("price_pct_from_anchor"))
        out["price"] = anchor_price * (1.0 + pct / 100.0)
    if "best_bid_pct_from_anchor" in out:
        pct = float(out.pop("best_bid_pct_from_anchor"))
        out["best_bid"] = anchor_price * (1.0 + pct / 100.0)
    if "best_ask_pct_from_anchor" in out:
        pct = float(out.pop("best_ask_pct_from_anchor"))
        out["best_ask"] = anchor_price * (1.0 + pct / 100.0)
    if "best_bid" in out and "best_ask" in out and "mid" not in out:
        bb = float(out["best_bid"]); ba = float(out["best_ask"])
        if ba > bb > 0:
            mid = 0.5 * (bb + ba)
            out["mid"] = mid
            out.setdefault("spread_bps", (ba - bb) / mid * 10_000.0)
    return out


def _build_event(spec: dict, anchor_ts: datetime, anchor_price: float | None,
                 *, symbol: str) -> Event:
    ts = anchor_ts + timedelta(seconds=float(spec.get("offset_s", 0.0)))
    payload = _resolve_payload(dict(spec.get("payload", {})), anchor_price)
    return Event(
        ts=ts,
        source=str(spec.get("source", f"spoof.{spec.get('kind', 'event')}")),
        kind=str(spec.get("kind", "trade")),
        symbol=symbol,
        payload=payload,
        tags=set(spec.get("tags", [])),
        severity=float(spec.get("severity", 0.0)),
    )


def inject_spoof(
    base: Iterable[Event],
    fixture: SpoofFixture,
    anchor_ts: datetime,
    *,
    symbol: str = "BTCUSDT",
) -> Iterator[Event]:
    """Yield base events with spoof events interleaved at anchor_ts + offset.

    Anchor price resolution: tracks `last_price` from base trade events
    whose `ts <= anchor_ts`. Once we cross the anchor, the spoof events
    are materialized with that price and merged in chronological order
    against the rest of the base stream.
    """
    anchor_price: float | None = None
    pending: list[Event] = []
    materialized = False

    def _materialize() -> None:
        nonlocal pending
        pending = [
            _build_event(s, anchor_ts, anchor_price, symbol=symbol)
            for s in fixture.events
        ]
        pending.sort(key=lambda e: e.ts)

    for ev in base:
        if not materialized and ev.kind == "trade" and ev.ts <= anchor_ts:
            p = ev.payload.get("price")
            if p:
                anchor_price = float(p)

        if not materialized and ev.ts >= anchor_ts:
            _materialize()
            materialized = True

        while pending and pending[0].ts <= ev.ts:
            yield pending.pop(0)

        yield ev

    if not materialized:
        _materialize()
        materialized = True
    while pending:
        yield pending.pop(0)
