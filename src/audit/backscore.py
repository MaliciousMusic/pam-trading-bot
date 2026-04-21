"""Forecast backscoring — live resolution of prediction anchors.

Every tick the engine emits a handful of prediction anchors (+15s, +1m,
+5m, +10m, +15m). This module:

  1) keeps them in a pending queue,
  2) when a later tick's timestamp has passed the anchor's target, joins
     with the realized price and produces a resolved record,
  3) persists resolved records to data/forecasts/YYYY-MM-DD.jsonl,
  4) maintains a trailing-window in-memory store so the UI can show
     MAE and hit-rate per horizon without rescanning disk.

The persistence file is separate from the ledger so the daily audit and
the forecast audit evolve independently.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..core import serde
from ..core.config import PROJECT_ROOT


FORECAST_DIR = PROJECT_ROOT / "data" / "forecasts"


@dataclass(slots=True)
class PendingAnchor:
    tick_ts: float          # epoch seconds
    target_ts: float
    label: str
    horizon_s: int
    entry_price: float
    predicted_price: float
    expected_move_bps: float
    forecast_conf: float
    forecast_emove_bps: float
    net_pressure: float


@dataclass(slots=True)
class Resolved:
    tick_ts: float
    target_ts: float
    realized_ts: float
    label: str
    horizon_s: int
    entry_price: float
    predicted_price: float
    realized_price: float
    expected_move_bps: float
    realized_move_bps: float
    signed_error_bps: float          # predicted - realized, relative
    abs_error_bps: float
    directional_hit: bool             # sign(predicted_move)==sign(realized_move) AND both non-zero
    forecast_conf: float
    net_pressure: float


class ForecastAuditor:
    """Live backscorer. One instance per trading engine.

    Bounded memory: `max_pending` caps outstanding anchors, `ring_cap`
    caps the trailing resolved window. Both defaults are generous for a
    15s tick bot running a full day.
    """

    def __init__(
        self,
        *,
        directory: Path | None = None,
        ring_cap: int = 20_000,
        max_pending: int = 10_000,
    ) -> None:
        self.dir = directory or FORECAST_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._pending: deque[PendingAnchor] = deque()
        self._resolved: deque[Resolved] = deque(maxlen=ring_cap)
        self._max_pending = max_pending

    # --- ingest ------------------------------------------------------

    def record_tick(
        self,
        ts: datetime,
        price: float,
        predictions: list[dict],
        *,
        forecast_conf: float,
        forecast_emove_bps: float,
        net_pressure: float,
    ) -> list[Resolved]:
        """Resolve any matured anchors against `price`, then enqueue the
        new anchors from this tick. Returns the list of records just
        resolved (so the engine can attach them to its broadcast)."""
        now = ts.timestamp() if isinstance(ts, datetime) else float(ts)

        resolved_now = self._resolve_due(now, price)
        self._enqueue(now, price, predictions,
                      forecast_conf=forecast_conf,
                      forecast_emove_bps=forecast_emove_bps,
                      net_pressure=net_pressure)
        return resolved_now

    def _enqueue(
        self,
        now: float,
        price: float,
        predictions: Iterable[dict],
        *,
        forecast_conf: float,
        forecast_emove_bps: float,
        net_pressure: float,
    ) -> None:
        for p in predictions:
            horizon = int(p.get("horizon_seconds", 0))
            if horizon <= 0:
                continue
            predicted = float(p.get("predicted_price", 0.0))
            if predicted <= 0:
                continue
            anchor = PendingAnchor(
                tick_ts=now,
                target_ts=now + horizon,
                label=str(p.get("label", "")),
                horizon_s=horizon,
                entry_price=price,
                predicted_price=predicted,
                expected_move_bps=float(p.get("expected_move_bps", 0.0)),
                forecast_conf=forecast_conf,
                forecast_emove_bps=forecast_emove_bps,
                net_pressure=net_pressure,
            )
            self._pending.append(anchor)

        # Drop oldest if backlog grew unreasonably (e.g. market feed gap).
        while len(self._pending) > self._max_pending:
            self._pending.popleft()

    def _resolve_due(self, now: float, realized_price: float) -> list[Resolved]:
        out: list[Resolved] = []
        # The pending deque is chronologically ordered on tick_ts, but
        # target_ts ordering can differ across horizons so we walk all of
        # them and pop the matured ones. This is still O(n) amortized and
        # n is tiny (~5 anchors per tick × 60 pending ticks ≈ 300).
        kept: deque[PendingAnchor] = deque()
        for a in self._pending:
            if now < a.target_ts:
                kept.append(a)
                continue
            if realized_price <= 0 or a.entry_price <= 0:
                continue  # can't score without a price
            predicted_move_bps = (a.predicted_price - a.entry_price) / a.entry_price * 10_000.0
            realized_move_bps = (realized_price - a.entry_price) / a.entry_price * 10_000.0
            signed_error = (a.predicted_price - realized_price) / realized_price * 10_000.0
            hit = (predicted_move_bps > 0 and realized_move_bps > 0) or (
                predicted_move_bps < 0 and realized_move_bps < 0
            )
            rec = Resolved(
                tick_ts=a.tick_ts,
                target_ts=a.target_ts,
                realized_ts=now,
                label=a.label,
                horizon_s=a.horizon_s,
                entry_price=a.entry_price,
                predicted_price=a.predicted_price,
                realized_price=realized_price,
                expected_move_bps=a.expected_move_bps,
                realized_move_bps=realized_move_bps,
                signed_error_bps=signed_error,
                abs_error_bps=abs(signed_error),
                directional_hit=hit,
                forecast_conf=a.forecast_conf,
                net_pressure=a.net_pressure,
            )
            out.append(rec)
            self._resolved.append(rec)
            self._persist(rec)
        self._pending = kept
        return out

    def _persist(self, rec: Resolved) -> None:
        day = datetime.fromtimestamp(rec.tick_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.dir / f"{day}.jsonl"
        payload = {
            "tick_ts": rec.tick_ts,
            "target_ts": rec.target_ts,
            "realized_ts": rec.realized_ts,
            "label": rec.label,
            "horizon_s": rec.horizon_s,
            "entry_price": rec.entry_price,
            "predicted_price": rec.predicted_price,
            "realized_price": rec.realized_price,
            "expected_move_bps": rec.expected_move_bps,
            "realized_move_bps": rec.realized_move_bps,
            "signed_error_bps": rec.signed_error_bps,
            "abs_error_bps": rec.abs_error_bps,
            "directional_hit": rec.directional_hit,
            "forecast_conf": rec.forecast_conf,
            "net_pressure": rec.net_pressure,
        }
        line = serde.dumps(payload) + b"\n"
        with path.open("ab") as f:
            f.write(line)

    # --- query -------------------------------------------------------

    def snapshot(self, *, window_s: float = 3600.0, min_samples: int = 3) -> dict:
        """Per-horizon aggregates over the trailing `window_s` seconds."""
        if not self._resolved:
            return {"window_s": window_s, "pending": len(self._pending),
                    "total_resolved": 0, "by_horizon": []}

        cutoff = self._resolved[-1].realized_ts - window_s
        per: dict[str, dict] = {}
        for r in self._resolved:
            if r.realized_ts < cutoff:
                continue
            agg = per.setdefault(r.label, {
                "label": r.label, "horizon_s": r.horizon_s,
                "n": 0, "hits": 0,
                "abs_err_sum": 0.0, "signed_err_sum": 0.0,
                "realized_mag_sum": 0.0, "predicted_mag_sum": 0.0,
            })
            agg["n"] += 1
            if r.directional_hit:
                agg["hits"] += 1
            agg["abs_err_sum"] += r.abs_error_bps
            agg["signed_err_sum"] += r.signed_error_bps
            agg["realized_mag_sum"] += abs(r.realized_move_bps)
            agg["predicted_mag_sum"] += abs(r.expected_move_bps)

        rows = []
        for label, a in per.items():
            n = a["n"]
            if n < min_samples:
                mae = None
                hit_rate = None
                bias_bps = None
            else:
                mae = a["abs_err_sum"] / n
                hit_rate = a["hits"] / n
                bias_bps = a["signed_err_sum"] / n
            rows.append({
                "label": label,
                "horizon_s": a["horizon_s"],
                "n": n,
                "mae_bps": mae,
                "hit_rate": hit_rate,
                "bias_bps": bias_bps,
                "mean_predicted_mag_bps": a["predicted_mag_sum"] / n if n else None,
                "mean_realized_mag_bps": a["realized_mag_sum"] / n if n else None,
            })
        rows.sort(key=lambda r: r["horizon_s"])
        return {
            "window_s": window_s,
            "pending": len(self._pending),
            "total_resolved": len(self._resolved),
            "by_horizon": rows,
        }

    def sigma_by_horizon(self, *, window_s: float = 3600.0,
                         min_samples: int = 10) -> dict[int, float]:
        """Residual stddev of realized_move − predicted_move per horizon,
        keyed by horizon seconds. Used by the projector to build its
        uncertainty fan and sample Monte Carlo paths. Horizons with fewer
        than `min_samples` resolved anchors are omitted — the caller is
        expected to fall back to a default."""
        if not self._resolved:
            return {}
        cutoff = self._resolved[-1].realized_ts - window_s
        bucket: dict[int, list[float]] = {}
        for r in self._resolved:
            if r.realized_ts < cutoff:
                continue
            resid = r.realized_move_bps - r.expected_move_bps
            bucket.setdefault(r.horizon_s, []).append(resid)
        out: dict[int, float] = {}
        for h, xs in bucket.items():
            n = len(xs)
            if n < min_samples:
                continue
            m = sum(xs) / n
            var = sum((x - m) ** 2 for x in xs) / n
            out[h] = var ** 0.5
        return out

    def recent(self, limit: int = 200) -> list[dict]:
        take = list(self._resolved)[-limit:]
        return [
            {
                "tick_ts": r.tick_ts, "target_ts": r.target_ts,
                "realized_ts": r.realized_ts,
                "label": r.label, "horizon_s": r.horizon_s,
                "entry_price": r.entry_price,
                "predicted_price": r.predicted_price,
                "realized_price": r.realized_price,
                "expected_move_bps": r.expected_move_bps,
                "realized_move_bps": r.realized_move_bps,
                "signed_error_bps": r.signed_error_bps,
                "abs_error_bps": r.abs_error_bps,
                "directional_hit": r.directional_hit,
                "forecast_conf": r.forecast_conf,
            }
            for r in take
        ]
