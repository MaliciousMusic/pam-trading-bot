"""Event-latency wave — inject classified events into the forward simulation.

When the classifier stamps a news item as e.g. `etf_approval / +1 / 287bps`,
the price doesn't move 287bps in the next tick and then stop. Different
participants take different amounts of time to see, interpret, and press
the button. MMs react in < 1s; workstation pros in 5–15s; copy-traders
in minutes; retail app-only users in tens of minutes.

This module converts that into a per-step pressure wave:

    dmass_i(k) = Φ((age_k − μ_i)/σ_i) − Φ((age_{k-1} − μ_i)/σ_i)
    event_bps_k = side × impact_bps × Σ_i (weight_i × dmass_i(k))

where Φ is the standard-Normal CDF, `age_k` is the virtual age of the
event at projector step k, and `weight_i` is tribe i's population-
derived weight. Summed over the full horizon the result approaches
`side × impact_bps` (exactly so for an infinite horizon — the
projector's N-step window captures only the prefix, which is honest).

The output is pure bps — the projector adds it to vote-derived pressure
before the depth kernel applies. A thin book with a big event therefore
pays double: depth amplifies whatever the wave injects.
"""
from __future__ import annotations

import math

from ..tribes.tribe import Tribe


def _phi(x: float) -> float:
    """Standard Normal CDF. `math.erf` is stdlib; we avoid numpy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def step_event_bps(
    pending_events: list[dict],
    tribes: list[Tribe],
    step_start_ts: float,
    step_end_ts: float,
) -> float:
    """Total event-driven bps to add at the step spanning (start, end].

    `pending_events` is a list of {ts, side, impact_bps, event_type} dicts;
    each event contributes a Gaussian mass increment for every tribe, scaled
    by tribe weight, event impact, and event side. Events older than
    μ + 4σ for every tribe are effectively fully propagated and drop out
    of the sum automatically.
    """
    if not pending_events or not tribes:
        return 0.0

    # Pre-extract tribe reaction params + weights.
    params: list[tuple[float, float, float]] = []
    total_w = 0.0
    for t in tribes:
        p = t.spec.personality
        mu = float(getattr(p, "reaction_mu_seconds", 60.0))
        sigma = max(1.0, float(getattr(p, "reaction_sigma_seconds", 90.0)))
        w = float(t.spec.flow.weight)
        if w <= 0:
            continue
        params.append((mu, sigma, w))
        total_w += w
    if total_w <= 0:
        return 0.0

    total_bps = 0.0
    for ev in pending_events:
        side = int(ev.get("side", 0))
        impact = float(ev.get("impact_bps", 0.0))
        ts = float(ev.get("ts", 0.0))
        if side == 0 or impact <= 0 or ts <= 0:
            continue
        age_prev = step_start_ts - ts
        age_end = step_end_ts - ts
        if age_end <= 0:
            continue  # event is in the future vs this step (projector didn't rewind)
        # Sum mass increments across tribes. Because each tribe weight is
        # already normalized (Σ weight_i ≈ 1 across the full council), the
        # mass term is a weighted CDF increment.
        mass_delta = 0.0
        for mu, sigma, w in params:
            prev_cdf = _phi((age_prev - mu) / sigma) if age_prev > -6 * sigma else 0.0
            curr_cdf = _phi((age_end - mu) / sigma)
            inc = curr_cdf - prev_cdf
            if inc <= 0:
                continue
            mass_delta += w * inc
        total_bps += side * impact * mass_delta / max(total_w, 1e-9)
    return total_bps


def trim_pending(events: list[dict], now_ts: float, max_age_s: float = 7200.0) -> list[dict]:
    """Drop events older than `max_age_s`. Caller decides when to trim."""
    return [e for e in events if (now_ts - float(e.get("ts", 0.0))) <= max_age_s]
