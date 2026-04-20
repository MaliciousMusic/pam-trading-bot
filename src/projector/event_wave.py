"""Event-latency wave — inject classified events into the forward simulation.

When the classifier stamps a news item as e.g. `etf_approval / +1 / 287bps`,
the price doesn't move 287bps in the next tick and then stop. Different
participants take different amounts of time to see, interpret, and press
the button. MMs react in < 1s; workstation pros in 5–15s; copy-traders
in minutes; retail app-only users in tens of minutes.

Latency distribution is **log-normal**, not Gaussian. Observed retail
reaction times during volatility spikes are fat-tailed: the median app
user presses the button in ~90s, but a non-trivial slice takes 10+ minutes
(notifications missed, phone on silent, app not open). A Gaussian
centered on 90s treats 900s as impossibly rare; a log-normal with the
same median puts proportionally more mass in the tail — which is what
the tape actually shows.

Per-step wave:

    dmass_i(k) = Φ((ln age_k − μ_ln_i)/σ_ln_i) − Φ((ln age_{k-1} − μ_ln_i)/σ_ln_i)
    event_bps_k = side × impact_bps × Σ_i (weight_i × dmass_i(k))

where Φ is the standard-Normal CDF (the log-normal's CDF is Φ on log-ages),
`age_k` is the virtual age of the event at projector step k, and
`weight_i` is tribe i's population-derived weight. Summed over the full
horizon the result approaches `side × impact_bps` (exactly so for an
infinite horizon — the projector's N-step window captures only the
prefix, which is honest).

Backward-compat mapping from the Gaussian spec fields:
    μ_ln = ln(max(μ_seconds, 1s))
    σ_ln = ln(1 + max(σ/μ, 0.1))  — moment-matched coefficient-of-variation;
           floored so a YAML with σ=0 still produces a sensible spread.

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


def _lognormal_params(mu_seconds: float, sigma_seconds: float) -> tuple[float, float]:
    """Map Gaussian-style (μ, σ) in seconds → log-normal (μ_ln, σ_ln).

    Interprets the YAML μ as the distribution's median (exp(μ_ln)) and σ
    as a coefficient-of-variation scale. With σ_seconds = μ_seconds the
    tribe's reaction mass has a ~40% spread in log-space — enough to
    surface the fat tail without collapsing the median onto a delta."""
    mu = max(1.0, float(mu_seconds))
    sigma = max(0.0, float(sigma_seconds))
    mu_ln = math.log(mu)
    # Floor the CoV at 0.1 so a YAML with σ=0 still has a finite spread.
    cov = max(0.10, sigma / mu)
    sigma_ln = math.log(1.0 + cov)
    return mu_ln, max(0.05, sigma_ln)


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

    # Pre-extract tribe reaction params + weights. Convert the YAML's
    # Gaussian-style (μ, σ) seconds into log-normal (μ_ln, σ_ln) once so
    # the per-event loop just evaluates Φ on log-ages.
    params: list[tuple[float, float, float]] = []
    total_w = 0.0
    for t in tribes:
        p = t.spec.personality
        mu_raw = float(getattr(p, "reaction_mu_seconds", 60.0))
        sigma_raw = float(getattr(p, "reaction_sigma_seconds", 90.0))
        mu_ln, sigma_ln = _lognormal_params(mu_raw, sigma_raw)
        w = float(t.spec.flow.weight)
        if w <= 0:
            continue
        params.append((mu_ln, sigma_ln, w))
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
        # Log-normal CDF is Φ on the log-age. Undefined for age ≤ 0, so the
        # previous-step CDF is 0 when the event hadn't fired yet at start.
        log_end = math.log(age_end)
        log_prev = math.log(age_prev) if age_prev > 0 else None
        mass_delta = 0.0
        for mu_ln, sigma_ln, w in params:
            curr_cdf = _phi((log_end - mu_ln) / sigma_ln)
            prev_cdf = (_phi((log_prev - mu_ln) / sigma_ln)
                        if log_prev is not None else 0.0)
            inc = curr_cdf - prev_cdf
            if inc <= 0:
                continue
            mass_delta += w * inc
        total_bps += side * impact * mass_delta / max(total_w, 1e-9)
    return total_bps


def trim_pending(events: list[dict], now_ts: float, max_age_s: float = 7200.0) -> list[dict]:
    """Drop events older than `max_age_s`. Caller decides when to trim."""
    return [e for e in events if (now_ts - float(e.get("ts", 0.0))) <= max_age_s]
