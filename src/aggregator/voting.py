"""Weighted synthesis of per-tribe Votes → single Aggregate.

Formulation (kept deliberately simple; tune later against audit data):

    signed_mass(v) = v.weight * v.conviction * (+1 BUY, -1 SELL, 0 ABSTAIN)
    net_pressure   = sum(signed_mass) / sum(all v.weight)          ∈ [-1, +1]

    active_mass    = buy_mass + sell_mass
    agreement      = |buy_mass - sell_mass| / active_mass          (1 unanimous, 0 split)
    uncertainty    = 1 - agreement                                 (pure disagreement signal)

Uncertainty reflects only DISAGREEMENT among tribes that are voting,
NOT how much of the population is voting. Population participation is
already modeled upstream via each tribe's `activation_rate` (which
scales conviction per tick). Mixing participation into uncertainty
would double-penalize: a retail-heavy crowd with 0.5% activation would
automatically have participation≈0 even when everyone active agreed,
stranding the forecast at zero confidence. The net_pressure magnitude
already reflects low participation (small convictions → small pressure).

Abstention is preserved in the denominator, so a silent majority
correctly suppresses net_pressure even when a few conviction-heavy
tribes are active. That's the user's first-class-abstention contract.
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
        if v.side is Side.BUY:
            buy_mass += v.weight * v.conviction
        elif v.side is Side.SELL:
            sell_mass += v.weight * v.conviction
        else:
            abstain_mass += v.weight
    denom = max(total_weight, _EPS)
    net_pressure = (buy_mass - sell_mass) / denom
    active_mass = buy_mass + sell_mass
    agreement = abs(buy_mass - sell_mass) / max(active_mass, _EPS)
    # Pure disagreement measure — decoupled from participation. Low activation
    # is already captured by small convictions → small net_pressure.
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
