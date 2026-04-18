"""Bottom-up derivation of per-tribe flow weights.

    weight_i  =  count_i × geometric_mid(capital_band_i) × activation_rate_i
    weight_i  /=  Σ weight_j                                    (normalize)

Geometric midpoint is used because capital distributions inside every
archetype are heavy-tailed (log-normal): the retail_app_newbie band
[15, 500] centers on $86.6, not on the arithmetic mean $257.5, and a
mm_algo band [1M, 500M] centers on $22.4M, not $250M.

The product (count × capital × activation) is "dollars of directional
conviction expressed per tick" — exactly what the aggregator should
weight a tribe's vote by. Tribes that lack any of the three inputs
fall back to their hand-tuned `flow.weight` and are folded into the
normalization as-is.
"""
from __future__ import annotations

from math import sqrt

from loguru import logger

from .spec import TribeSpec


def _geometric_mid(band: list[float]) -> float:
    if not band or len(band) < 2:
        return 0.0
    lo, hi = float(band[0]), float(band[1])
    if lo <= 0.0 or hi <= 0.0:
        return max(lo, hi, 0.0)
    return sqrt(lo * hi)


def _raw_weight(spec: TribeSpec) -> tuple[float, bool]:
    """Return (raw_weight, derived_flag).

    derived=True  → computed bottom-up from count/capital/activation.
    derived=False → fell back to spec.flow.weight (manual override).
    """
    count = spec.demographics.count_estimate
    cap_mid = _geometric_mid(spec.demographics.capital_band_usd)
    rate = getattr(spec.flow, "activation_rate", None)
    if count and cap_mid > 0 and rate is not None and rate > 0:
        return float(count) * cap_mid * float(rate), True
    return float(spec.flow.weight), False


def derive_weights(specs: list[TribeSpec]) -> dict[str, float]:
    """Compute normalized per-tribe weights. Sum == 1.0 (unless all zero)."""
    raws: list[tuple[str, float, bool]] = []
    for s in specs:
        w, derived = _raw_weight(s)
        raws.append((s.id, max(0.0, w), derived))
    total = sum(w for _, w, _ in raws)
    if total <= 0.0:
        return {tid: 0.0 for tid, _, _ in raws}
    return {tid: w / total for tid, w, _ in raws}


def apply_and_log(specs: list[TribeSpec]) -> None:
    """Mutate each spec's `flow.weight` in place to the derived value and
    emit a one-shot summary table. Safe to call exactly once at boot."""
    derived = derive_weights(specs)
    logger.info(
        "[council-derivation] weight = count × geo_mid(capital) × activation_rate, then normalized"
    )
    logger.info(
        "  {:<28} {:>10} {:>14} {:>10} {:>14} {:>8} → {:>8}",
        "tribe", "count", "cap_mid_usd", "act_rate", "aum_per_tick", "w_old", "w_new",
    )
    total_aum = 0.0
    for s in specs:
        count = s.demographics.count_estimate or 0
        cap_mid = _geometric_mid(s.demographics.capital_band_usd)
        rate = float(getattr(s.flow, "activation_rate", 0.0))
        aum_tick = count * cap_mid * rate
        total_aum += aum_tick
        old = s.flow.weight
        new = derived.get(s.id, old)
        logger.info(
            "  {:<28} {:>10,} {:>14,.0f} {:>10.3f} {:>14,.0f} {:>8.3f} → {:>8.3f}",
            s.id, count, cap_mid, rate, aum_tick, old, new,
        )
        s.flow.weight = new
    logger.info("  {:<28} {:>10} {:>14} {:>10} {:>14,.0f}",
                "TOTAL", "", "", "", total_aum)
