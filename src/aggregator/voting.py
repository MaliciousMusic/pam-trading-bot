"""Weighted synthesis of per-tribe Votes → single Aggregate.

Formulation:

    Each tribe carries buy_frac / sell_frac representing the INTERNAL
    DISTRIBUTION within the active slice of that tribe:

        buy_frac  = activation_rate * (0.5 + raw_conviction/2)   [BUY tribe]
        sell_frac = activation_rate * (0.5 - raw_conviction/2)   [BUY tribe]

    This means a tribe voting BUY at conviction 0.6 is NOT treated as 100%
    buying — 20% of its active members are implicitly selling. Both sides
    contribute to buy_mass / sell_mass:

        buy_mass  = Σ weight_i * buy_frac_i
        sell_mass = Σ weight_i * sell_frac_i

    net_pressure is mathematically equivalent to the old signed-mass formula
    (the opposing fractions cancel in the difference), but buy_mass and
    sell_mass are now realistic, which makes uncertainty meaningful:

        active_mass = buy_mass + sell_mass
        agreement   = |buy_mass - sell_mass| / active_mass   (1 = unanimous)
        uncertainty = 1 - agreement                          (true disagreement)

Abstaining tribes add weight to the denominator only, correctly suppressing
net_pressure when a silent majority holds.
"""
from __future__ import annotations

from datetime import datetime

from ..core.types import Aggregate, Side, Vote


_EPS = 1e-9


def aggregate_votes(votes: list[Vote], ts: datetime) -> Aggregate:
    buy_mass = 0.0
    sell_mass = 0.0
    abstain_mass = 0.0
    total_weight = 0.0
    for v in votes:
        total_weight += v.weight
        if v.side is Side.ABSTAIN:
            abstain_mass += v.weight
        else:
            # Use the pre-computed internal distribution fractions so that
            # the opposing minority within each tribe is counted correctly.
            buy_mass += v.weight * v.buy_frac
            sell_mass += v.weight * v.sell_frac
    denom = max(total_weight, _EPS)
    net_pressure = (buy_mass - sell_mass) / denom
    active_mass = buy_mass + sell_mass
    agreement = abs(buy_mass - sell_mass) / max(active_mass, _EPS)
    uncertainty = 1.0 - agreement
    return Aggregate(
        ts=ts,
        net_pressure=max(-1.0, min(1.0, net_pressure)),
        buy_mass=buy_mass,
        sell_mass=sell_mass,
        abstain_mass=abstain_mass,
        vote_count=len(votes),
        uncertainty=max(0.0, min(1.0, uncertainty)),
    )
