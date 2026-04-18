"""Audit-driven hypothesis generation.

Each hypothesis is an ATOMIC observation: change exactly ONE parameter
on ONE tribe and see whether the council's equity improves. That
constraint is deliberate — if we mutate several knobs at once we can't
attribute wins or losses to any single change.

A hypothesis becomes a variant. The day's daily audit can produce
1..MAX_VARIANTS hypotheses depending on how many actionable signals it
finds; each variant is a child council that runs its own paper book.

Fitness is equity-delta (USD and percent). Tribe-level participation
and hit-rate are diagnostic, not primary: a tribe that hits 80% while
only voting twice is less useful than one at 55% voting constantly.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from ..tribes.spec import TribeSpec
from .daily import DailyReport, TribeScore


MAX_VARIANTS = 6


@dataclass(slots=True)
class Hypothesis:
    tribe_id: str
    field: str                    # e.g. "strategy.params.min_net_flow_usd"
    current: float
    proposed: float
    rationale: str                # one-line explanation for the lineage log
    priority: float               # 0..1 — higher means audit thinks it matters more


# ── heuristic bands ─────────────────────────────────────────────────────
#
# Under the always-leaning v2 contract every tribe votes directionally on
# nearly every tick; `participation` is now ≈ 1.0 for everyone and is no
# longer informative. What we care about is `engaged_rate` — how often the
# tribe raises its voice above a whisper (conviction ≥ ENGAGED_CONVICTION).

LOW_ENGAGED_RATE = 0.05       # raised voice on <5% of ticks → starved or over-tight
HIGH_ENGAGED_RATE = 0.60      # raised voice on >60% → trigger-happy
WEAK_HIT_RATE = 0.38          # conviction-weighted hit-rate below this is bad
STRONG_HIT_RATE = 0.58        # above this is notable

# Per-family: which param is the "gate" (lower = more firing) and what to nudge.
GATE_PARAM: dict[str, str] = {
    "app_fomo":           "strategy.params.min_move_bps",
    "momentum_chaser":    "strategy.params.min_net_flow_usd",
    "breakout_trader":    "strategy.params.break_buffer_bps",
    "trend_follower":     "strategy.params.min_persistent_move_bps",
    "mean_reverter_mm":   "strategy.params.max_imbalance_usd",
    "liquidation_hunter": "strategy.params.min_liq_usd",
    "funding_fader":      "strategy.params.extreme_funding_rate",
    "news_reactor":       "strategy.params.extreme_social_score",
    "grid_bot":           "strategy.params.grid_band_bps",
    "copy_trader":        "strategy.params.min_social_score",
}
WEIGHT_PATH = "flow.weight"


def _get(obj: dict, path: str) -> Any:
    for k in path.split("."):
        obj = obj[k]
    return obj


def _tribe_by_id(tribes: list[TribeSpec], tid: str) -> TribeSpec | None:
    return next((t for t in tribes if t.id == tid), None)


def _score(r: DailyReport, tid: str) -> TribeScore:
    return r.tribes.get(tid, TribeScore(tribe_id=tid))


def council_fitness(r: DailyReport) -> dict[str, float]:
    """Return the single number we actually care about + context."""
    delta = r.end_equity - r.start_equity
    pct = 0.0 if r.start_equity == 0 else delta / r.start_equity
    return {
        "equity_delta_usd": delta,
        "equity_delta_pct": pct,
        "start_equity": r.start_equity,
        "end_equity": r.end_equity,
        "trades": float(r.trades),
        "wins": float(r.wins),
        "win_rate": r.win_rate,
    }


def generate(report: DailyReport, tribes: list[TribeSpec]) -> list[Hypothesis]:
    """Produce a bounded set of single-parameter experiments worth running.

    Ranking logic:
      - tribes with 0 participation are the MOST urgent to loosen (silent
        tribes contribute nothing)
      - tribes with high participation but weak hit-rate are TIGHTENED
      - strongly profitable tribes get their WEIGHT bumped (audit's
        hypothesis: "this tribe deserves more voice")
      - consistently wrong tribes get their WEIGHT cut
    The daily equity delta is used as a GAIN modifier — a losing day
    produces more aggressive mutations than a winning one.
    """
    fitness = council_fitness(report)
    losing_day = fitness["equity_delta_usd"] < 0
    # Losing days get wider mutations, winning days get narrow drift.
    gain = 0.55 if losing_day else 0.25

    hypotheses: list[Hypothesis] = []
    for spec in tribes:
        data = spec.model_dump()
        s = _score(report, spec.id)
        family = spec.strategy.family
        gate = GATE_PARAM.get(family)

        # 1) Whispering tribes — loosen gate so they actually commit.
        if s.engaged_rate < LOW_ENGAGED_RATE and gate:
            try:
                cur = float(_get(data, gate))
                proposed = cur * (1 - gain)
                if proposed != cur and proposed > 0:
                    hypotheses.append(Hypothesis(
                        tribe_id=spec.id, field=gate,
                        current=cur, proposed=proposed,
                        rationale=f"whispering (engaged={s.engaged_rate:.1%}) "
                                  f"— loosen gate by {gain:.0%}",
                        priority=0.95 if s.engaged_votes == 0 else 0.75,
                    ))
            except (KeyError, TypeError):
                pass

        # 2) Over-engaged tribes with weak hit-rate — tighten gate.
        elif s.engaged_rate > HIGH_ENGAGED_RATE and \
             s.conviction_weighted_hit_rate < WEAK_HIT_RATE and gate:
            try:
                cur = float(_get(data, gate))
                proposed = cur * (1 + gain)
                if proposed != cur:
                    hypotheses.append(Hypothesis(
                        tribe_id=spec.id, field=gate,
                        current=cur, proposed=proposed,
                        rationale=f"over-engaged ({s.engaged_rate:.1%}) "
                                  f"with hr={s.conviction_weighted_hit_rate:.1%} — tighten",
                        priority=0.80,
                    ))
            except (KeyError, TypeError):
                pass

        # 3) Profitable voter — bump weight.
        if s.engaged_votes >= 10 and s.conviction_weighted_hit_rate >= STRONG_HIT_RATE:
            try:
                cur = float(_get(data, WEIGHT_PATH))
                proposed = min(0.5, cur * (1 + gain))
                if proposed > cur:
                    hypotheses.append(Hypothesis(
                        tribe_id=spec.id, field=WEIGHT_PATH,
                        current=cur, proposed=proposed,
                        rationale=f"strong voter (hr={s.conviction_weighted_hit_rate:.1%},"
                                  f" n={s.directional_votes}) — promote",
                        priority=0.85,
                    ))
            except (KeyError, TypeError):
                pass

        # 4) Chronically wrong voter — cut weight.
        if s.directional_votes >= 10 and s.conviction_weighted_hit_rate <= WEAK_HIT_RATE:
            try:
                cur = float(_get(data, WEIGHT_PATH))
                proposed = max(0.01, cur * (1 - gain))
                if proposed < cur:
                    hypotheses.append(Hypothesis(
                        tribe_id=spec.id, field=WEIGHT_PATH,
                        current=cur, proposed=proposed,
                        rationale=f"chronically wrong (hr={s.conviction_weighted_hit_rate:.1%},"
                                  f" n={s.directional_votes}) — demote",
                        priority=0.70,
                    ))
            except (KeyError, TypeError):
                pass

    # Rank & bound.
    hypotheses.sort(key=lambda h: -h.priority)
    return hypotheses[:MAX_VARIANTS]


def hypothesis_to_dict(h: Hypothesis) -> dict:
    return asdict(h)
