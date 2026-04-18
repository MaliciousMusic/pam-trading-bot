"""Daily audit. Reads a day's ledger, scores tribes and the system.

Per-tribe alignment: for each tick we logged the tribe's vote and the
price at that tick. For each non-abstain vote, we look `audit_horizon_s`
seconds forward and check whether price moved in the voted direction.

Tribe metrics:
  - hit_rate       (% of directional votes that aligned with forward price)
  - participation  (% of ticks where this tribe did NOT abstain)
  - engaged_rate   (% of ticks where conviction ≥ ENGAGED_CONVICTION —
                    the metric that actually matters under the always-
                    leaning v2 contract, since nearly every tick is
                    "directional" now; a tribe whispering at 0.08 all day
                    is effectively silent.)
  - alignment      (weighted by conviction)

System metrics from execution records:
  - trades, wins, realized_pnl_usd, equity_curve endpoints.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import orjson
from loguru import logger

from ..execution.ledger import LEDGER_DIR


# Conviction ≥ this counts as a real take, not a whisper. Matches the
# default Flow.strong_vote_conviction in tribes/spec.py.
ENGAGED_CONVICTION = 0.3


@dataclass(slots=True)
class TribeScore:
    tribe_id: str
    total_votes: int = 0
    abstain_votes: int = 0
    directional_votes: int = 0
    engaged_votes: int = 0          # directional AND conviction ≥ ENGAGED_CONVICTION
    hits: int = 0
    engaged_hits: int = 0
    conviction_sum: float = 0.0
    hit_conviction_sum: float = 0.0

    @property
    def participation(self) -> float:
        return 0.0 if self.total_votes == 0 else self.directional_votes / self.total_votes

    @property
    def engaged_rate(self) -> float:
        return 0.0 if self.total_votes == 0 else self.engaged_votes / self.total_votes

    @property
    def hit_rate(self) -> float:
        return 0.0 if self.directional_votes == 0 else self.hits / self.directional_votes

    @property
    def engaged_hit_rate(self) -> float:
        return 0.0 if self.engaged_votes == 0 else self.engaged_hits / self.engaged_votes

    @property
    def conviction_weighted_hit_rate(self) -> float:
        return 0.0 if self.conviction_sum == 0 else self.hit_conviction_sum / self.conviction_sum


@dataclass(slots=True)
class DailyReport:
    day: str
    trades: int = 0
    wins: int = 0
    realized_pnl_usd: float = 0.0
    start_equity: float = 0.0
    end_equity: float = 0.0
    tribes: dict[str, TribeScore] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return 0.0 if self.trades == 0 else self.wins / self.trades


def _read_jsonl(path: Path):
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield orjson.loads(line)


def audit_day(day: str, horizon_seconds: int = 60) -> DailyReport:
    """Compile a DailyReport for a given YYYY-MM-DD day."""
    path = LEDGER_DIR / f"{day}.jsonl"
    if not path.exists():
        logger.warning("no ledger for {}", day)
        return DailyReport(day=day)

    vote_ticks: list[dict] = []  # each has ts, price, votes=[{tribe_id, side, conviction}]
    report = DailyReport(day=day)
    first_equity: float | None = None
    last_equity: float | None = None

    for rec in _read_jsonl(path):
        ev = rec.get("event")
        if ev == "tick":
            vote_ticks.append(rec)
        elif ev == "open":
            if first_equity is None:
                first_equity = rec.get("equity_usd")
        elif ev == "close":
            report.trades += 1
            pnl = rec.get("pnl_usd", 0.0) - rec.get("fee_usd", 0.0)
            report.realized_pnl_usd += pnl
            if pnl > 0:
                report.wins += 1
        if "equity_usd" in rec:
            last_equity = rec["equity_usd"]

    # Score tribes
    for i, tick in enumerate(vote_ticks):
        price_now = tick.get("price", 0.0)
        target_ts = _parse_ts(tick["ts"]).timestamp() + horizon_seconds
        price_future = None
        for j in range(i + 1, len(vote_ticks)):
            if _parse_ts(vote_ticks[j]["ts"]).timestamp() >= target_ts:
                price_future = vote_ticks[j].get("price", 0.0)
                break
        if price_future is None or price_now == 0:
            continue
        actual_move = price_future - price_now
        for v in tick.get("votes", []):
            tid = v["tribe_id"]
            score = report.tribes.setdefault(tid, TribeScore(tribe_id=tid))
            score.total_votes += 1
            side = v["side"]
            if side == "abstain":
                score.abstain_votes += 1
                continue
            score.directional_votes += 1
            conv = float(v.get("conviction", 0.0))
            score.conviction_sum += conv
            engaged = conv >= ENGAGED_CONVICTION
            if engaged:
                score.engaged_votes += 1
            hit = (actual_move > 0 and side == "buy") or (actual_move < 0 and side == "sell")
            if hit:
                score.hits += 1
                score.hit_conviction_sum += conv
                if engaged:
                    score.engaged_hits += 1

    report.start_equity = first_equity or 0.0
    report.end_equity = last_equity or 0.0
    return report


def _parse_ts(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def print_report(r: DailyReport) -> None:
    logger.info("=" * 72)
    logger.info("DAILY AUDIT  {}  trades={} wins={} win_rate={:.1%} pnl=${:+.2f} eq ${:.2f}→${:.2f}",
                r.day, r.trades, r.wins, r.win_rate, r.realized_pnl_usd, r.start_equity, r.end_equity)
    logger.info("-" * 72)
    rows = sorted(r.tribes.values(), key=lambda s: -s.conviction_weighted_hit_rate)
    for s in rows:
        logger.info(
            "  {:<32} eng={:.1%}  eng_votes={:<5} eng_hr={:.1%}  conv_hit={:.1%}",
            s.tribe_id, s.engaged_rate, s.engaged_votes,
            s.engaged_hit_rate, s.conviction_weighted_hit_rate,
        )
    logger.info("=" * 72)
