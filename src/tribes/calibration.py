"""Council calibration report.

Phase F companion to `derive.py`. Prints the effective per-tribe share of
tick-level *directional force* — not just raw AUM weight. The honest
aggregate is:

    contribution_i  =  weight_i × activation_rate_i × presence_i(h) × default_conviction_i

where `weight_i` is the normalized AUM weight from derive.py,
`activation_rate_i` is the fraction of the cohort expressing a view in
a single tick, `presence_i(h)` is the circadian multiplier at UTC hour
`h`, and `default_conviction_i` approximates the typical directional
strength each vote carries. Without conviction, MMs look dominant (always
present, activation=1.0) when in reality they quote two-sided with near-
zero directional lean. Conviction closes that gap.

The helper here is a diagnostic, not a runtime dependency. Call
`log_calibration(tribes)` once after `derive.apply_and_log()` to get a
table you can eyeball against Kaiko / Glassnode / Binance flow reports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from .tribe import Tribe


def _presence_avg(t: Tribe) -> float:
    curve = t.spec.circadian.presence_by_hour_utc or []
    if not curve:
        return 1.0
    return sum(curve) / len(curve)


def _presence_peak(t: Tribe) -> float:
    curve = t.spec.circadian.presence_by_hour_utc or []
    return max(curve) if curve else 1.0


def log_calibration(tribes: list[Tribe]) -> None:
    """Emit a per-tribe contribution table: weight × activation × presence.

    Meant to be eyeballed against public references (Kaiko flow reports,
    Glassnode active-entity cohorts, Coinglass Pro positioning data) so a
    human can see whether any archetype is over- or under-representing
    itself at tick time."""
    logger.info("[council-calibration] tick-level directional force = w × act × presence × default_conviction")
    logger.info(
        "  {:<28} {:>8} {:>8} {:>6} {:>9} {:>9} {:>12} {:>12}",
        "tribe", "weight", "act", "conv", "pres_avg", "pres_peak",
        "contrib_avg", "contrib_peak",
    )
    contrib_avg_rows: list[tuple[str, float]] = []
    contrib_peak_rows: list[tuple[str, float]] = []
    total_w = 0.0
    for t in tribes:
        w = float(t.spec.flow.weight)
        act = float(getattr(t.spec.flow, "activation_rate", 1.0))
        conv = float(getattr(t.spec.flow, "default_conviction", 0.5))
        p_avg = _presence_avg(t)
        p_peak = _presence_peak(t)
        c_avg = w * act * p_avg * conv
        c_peak = w * act * p_peak * conv
        total_w += w
        contrib_avg_rows.append((t.id, c_avg))
        contrib_peak_rows.append((t.id, c_peak))
        logger.info(
            "  {:<28} {:>8.3f} {:>8.3f} {:>6.2f} {:>9.3f} {:>9.3f} {:>12.4f} {:>12.4f}",
            t.id, w, act, conv, p_avg, p_peak, c_avg, c_peak,
        )
    tot_avg = sum(c for _, c in contrib_avg_rows)
    tot_peak = sum(c for _, c in contrib_peak_rows)
    logger.info(
        "  {:<28} {:>8.3f} {:>8} {:>6} {:>9} {:>9} {:>12.4f} {:>12.4f}",
        "TOTAL", total_w, "", "", "", "", tot_avg, tot_peak,
    )
    # Normalized contribution shares — this is the honest "who drives the
    # tape at tick time" breakdown. If one tribe is > 40% something is off.
    logger.info("[council-calibration] normalized tick contribution share (avg hour)")
    if tot_avg > 0:
        for tid, c in sorted(contrib_avg_rows, key=lambda r: -r[1]):
            logger.info("  {:<28} {:>6.1%}", tid, c / tot_avg)
    # Quick sanity flags — any single tribe dominating, or any near-zero.
    # MM dominance up to ~60% is expected per Kaiko net-flow decompositions;
    # raise the alarm only above that. Near-silent < 0.5% deserves a look
    # unless the cohort is intentionally event-gated (funding fader, news
    # fader) — those sleep on most ticks and wake up around events.
    event_gated = {"contrarian_funding_fader", "news_fader"}
    if tot_avg > 0:
        for tid, c in contrib_avg_rows:
            share = c / tot_avg
            if share > 0.60:
                logger.warning(
                    "[council-calibration] {} dominates at {:.1%} of tick flow "
                    "— review count/capital/activation_rate", tid, share,
                )
            elif share < 0.005 and c > 0 and tid not in event_gated:
                logger.warning(
                    "[council-calibration] {} is near-silent ({:.2%}) — check "
                    "activation_rate and presence curve", tid, share,
                )


def current_hour_contribution(tribes: list[Tribe]) -> dict[str, float]:
    """Right-now contribution per tribe at current UTC hour. Useful for
    exposing via /api/council_state so the UI can show who's dominating."""
    h = datetime.now(timezone.utc).hour
    out: dict[str, float] = {}
    for t in tribes:
        w = float(t.spec.flow.weight)
        act = float(getattr(t.spec.flow, "activation_rate", 1.0))
        conv = float(getattr(t.spec.flow, "default_conviction", 0.5))
        curve = t.spec.circadian.presence_by_hour_utc or [1.0] * 24
        p = float(curve[h % len(curve)])
        out[t.id] = w * act * p * conv
    total = sum(out.values())
    if total > 0:
        out = {k: v / total for k, v in out.items()}
    return out
