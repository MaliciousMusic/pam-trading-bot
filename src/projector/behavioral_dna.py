"""Behavioral DNA — per-archetype response functions for the recursive ABM.

Each tribe category (mm/arb/retail/ta/event/hedger/microstructure) has a
deterministic Response Function that maps:

  (state_{i,h-1}, delta_{i,h-1}, regime, perceived_shocks_{i,h})
                            ↓
                  delta_bps for step h

Tribes are no longer driven by independent Gaussian draws — wiggle emerges
from the interaction of these response functions with a sparse schedule
of typed World Shocks plus the tribe's own initial directional intent.

──────────────────────────────────────────────────────────────────────────
Information Asymmetry is the model's core: each category has a Latency
(how many horizons after a shock fires before it 'reaches' the tribe)
and a Visibility map (which kinds of shocks it sees and at what
multiplier). A liquidity shock hits MM at h=0 immediately (causing a
microstructure-led spike), reaches arb a step later (which begins
fading the spike toward fair value), shows up to TA only as
prev_delta-driven momentum at h=2-3, and finally trips Retail's panic
threshold at h=5+. The cascade IS the alpha.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


ShockKind = Literal["macro", "micro", "sentiment", "liquidity"]
SHOCK_KINDS: tuple[ShockKind, ...] = ("macro", "micro", "sentiment", "liquidity")


@dataclass(slots=True, frozen=True)
class WorldShock:
    """A single typed market shock fired at horizon `h_origin`.

    Magnitude is signed bps; positive = upward push. The same shock
    reaches different tribes at different horizons (latency) with
    different effective magnitudes (visibility multiplier).
    """
    kind: ShockKind
    magnitude: float
    h_origin: int


@dataclass(slots=True, frozen=True)
class ShockProfile:
    """How a tribe category perceives world shocks.

    `latency_steps`: horizons between shock firing and tribe perceiving it.
    `visibility[kind]`: multiplier on magnitude when the shock arrives
        (0.0 = invisible to this category).
    `panic_threshold_bps`: absolute magnitude floor below which a
        perceived shock is filtered out entirely. Used by retail to
        ignore everything except large moves.
    """
    latency_steps: int
    visibility: dict[ShockKind, float]
    panic_threshold_bps: float = 0.0


_PROFILES: dict[str, ShockProfile] = {
    "arb": ShockProfile(
        latency_steps=1,
        visibility={"macro": 1.0, "micro": 1.0, "sentiment": 0.6, "liquidity": 1.0},
    ),
    "microstructure": ShockProfile(
        latency_steps=0,
        visibility={"macro": 0.4, "micro": 1.5, "sentiment": 0.2, "liquidity": 1.2},
    ),
    "mm": ShockProfile(
        latency_steps=0,
        visibility={"macro": 0.3, "micro": 1.0, "sentiment": 0.1, "liquidity": 1.4},
    ),
    "ta": ShockProfile(
        latency_steps=2,
        # TA's path is shaped mostly by prev_delta amplification (see
        # ta_dna). The visibility here is a weak leaked-headline channel.
        visibility={"macro": 0.5, "micro": 0.3, "sentiment": 0.5, "liquidity": 0.3},
    ),
    "event": ShockProfile(
        latency_steps=2,
        visibility={"macro": 1.2, "micro": 0.2, "sentiment": 1.4, "liquidity": 0.3},
    ),
    "hedger": ShockProfile(
        latency_steps=4,
        visibility={"macro": 1.0, "micro": 0.1, "sentiment": 0.4, "liquidity": 0.2},
    ),
    "retail": ShockProfile(
        latency_steps=5,
        visibility={"macro": 0.8, "micro": 0.4, "sentiment": 1.0, "liquidity": 0.5},
        panic_threshold_bps=15.0,
    ),
    # MEV / Front-Runner — colocated, sees micro+liquidity before anyone.
    # Zero latency (acts in the same block as the shock) and elevated
    # visibility on the two shock kinds that carry immediate flow.
    "mev": ShockProfile(
        latency_steps=0,
        visibility={"macro": 0.2, "micro": 1.8, "sentiment": 0.1, "liquidity": 1.6},
    ),
    # Passive Whale / ETF TWAP — price-insensitive, slow, blind to noise.
    # Long latency (their schedule is set in a planning meeting, not by
    # the tape) and low shock visibility — a whale slicing $200M over 6h
    # doesn't accelerate because of a 30bps micro wiggle.
    "whale": ShockProfile(
        latency_steps=6,
        visibility={"macro": 0.4, "micro": 0.0, "sentiment": 0.1, "liquidity": 0.1},
    ),
}


def profile_for(category: str) -> ShockProfile:
    return _PROFILES.get(category, _PROFILES["ta"])


# ── Shock schedule generation ───────────────────────────────────────────
# Per-regime distribution over which kinds of events the tape is likely
# to throw at the council during this regime. Cascades are dominated by
# liquidity/micro events; trending regimes by macro/sentiment.
_REGIME_SHOCK_MIX: dict[str, dict[ShockKind, float]] = {
    "normal":       {"macro": 0.25, "micro": 0.30, "sentiment": 0.20, "liquidity": 0.25},
    "chop":         {"macro": 0.10, "micro": 0.45, "sentiment": 0.15, "liquidity": 0.30},
    "trend_up":     {"macro": 0.30, "micro": 0.20, "sentiment": 0.35, "liquidity": 0.15},
    "trend_down":   {"macro": 0.30, "micro": 0.20, "sentiment": 0.35, "liquidity": 0.15},
    "cascade_up":   {"macro": 0.15, "micro": 0.30, "sentiment": 0.20, "liquidity": 0.35},
    "cascade_down": {"macro": 0.15, "micro": 0.30, "sentiment": 0.20, "liquidity": 0.35},
}


def generate_shock_schedule(
    n_steps: int,
    *,
    regime: str,
    sigma_terminal_bps: float,
    rng,
    target_count: int = 4,
) -> list[WorldShock]:
    """Sample a sparse schedule of typed shocks across [0, n_steps).

    Magnitudes scale with the projector's terminal sigma so cascades
    naturally produce bigger shocks than calm regimes. Sign of each
    shock is independent — direction is decided by tribe intent + DNA,
    not by shock polarity.
    """
    if n_steps <= 0 or target_count <= 0:
        return []
    mix = _REGIME_SHOCK_MIX.get(regime) or _REGIME_SHOCK_MIX["normal"]
    kinds = list(mix.keys())
    weights = [mix[k] for k in kinds]
    # 25% of terminal sigma is a "typical" event; cascades hit bigger.
    base_mag = max(2.0, 0.25 * float(sigma_terminal_bps))
    out: list[WorldShock] = []
    for _ in range(target_count):
        h = rng.randint(0, n_steps - 1)
        kind = rng.choices(kinds, weights=weights, k=1)[0]
        # Cluster around base_mag with a long tail for the occasional
        # "real" shock — half-Gaussian shape on the multiplier.
        mag = base_mag * (0.5 + abs(rng.gauss(0.0, 1.2)))
        if rng.random() < 0.5:
            mag = -mag
        out.append(WorldShock(kind=kind, magnitude=mag, h_origin=h))
    return out


def perceive_for_tribe(
    schedule: list[WorldShock],
    profile: ShockProfile,
    *,
    n_steps: int,
) -> list[list[WorldShock]]:
    """Per-horizon list of shocks this tribe perceives.

    Applies the profile's latency (shifts h_origin → h_origin + latency)
    and visibility (multiplies magnitude per kind, drops zero-visibility
    or sub-panic-threshold events). The returned list preserves the
    shock's `kind`, so DNA functions can react differently to e.g. a
    liquidity vs. macro shock at the same horizon.
    """
    by_h: list[list[WorldShock]] = [[] for _ in range(n_steps)]
    for s in schedule:
        h_perc = s.h_origin + profile.latency_steps
        if h_perc < 0 or h_perc >= n_steps:
            continue
        vis = profile.visibility.get(s.kind, 0.0)
        if vis <= 0.0:
            continue
        eff_mag = s.magnitude * vis
        if abs(eff_mag) < profile.panic_threshold_bps:
            continue
        by_h[h_perc].append(WorldShock(
            kind=s.kind, magnitude=eff_mag, h_origin=s.h_origin,
        ))
    return by_h


# ── Behavioral DNA per category ─────────────────────────────────────────
# Each function maps a DNAContext to delta_bps for the next horizon step.
# Inputs cover the recursive state, the tribe's initial directional intent
# and base strength (decayed by the per-tribe horizon kernel before being
# passed in), the regime label, and the shocks this tribe perceives at h
# (already latency- and visibility-filtered, with `kind` preserved).

@dataclass(slots=True)
class DNAContext:
    prev_state_bps: float       # cumulative bps so far (state at h-1)
    prev_delta_bps: float       # last step's delta (delta at h-1)
    regime: str
    perceived: list[WorldShock] # shocks visible to this tribe at h
    signed_intent: float        # +1 / -1 / 0 from initial vote
    base_strength: float        # per-step bps amplitude this tribe wants
    h: int                      # current horizon step (0-indexed)


def _perceived_sum(ctx: DNAContext, kinds: set[str] | None = None) -> float:
    if kinds is None:
        return sum(s.magnitude for s in ctx.perceived)
    return sum(s.magnitude for s in ctx.perceived if s.kind in kinds)


def arb_dna(ctx: DNAContext) -> float:
    """Arbitrage: sees everything, fades dislocations toward fair value.

    Shape: small intent-driven drift + strong mean reversion to "fair
    price" (proxied by zero cum_bps) + immediate full-magnitude shock
    response as repricing pressure. Geometry: mean-reverting curve that
    keeps trying to drag everything back to anchor.
    """
    intent_drift   = 0.30 * ctx.base_strength * ctx.signed_intent
    fair_value_pull = -0.18 * ctx.prev_state_bps
    shock_response  = _perceived_sum(ctx) * 0.55
    return intent_drift + fair_value_pull + shock_response


def microstructure_dna(ctx: DNAContext) -> float:
    """Microstructure prober: fast, micro-shock-dominant, imbalance fader.

    Reads order-flow noise — small intent push, strong reaction to micro
    + liquidity shocks, fast revert from extremes, light prev_delta
    momentum (microstructure trend within the tape).
    """
    intent_drift   = 0.20 * ctx.base_strength * ctx.signed_intent
    imbalance_fade = -0.25 * ctx.prev_state_bps
    shock_response = _perceived_sum(ctx) * 0.70
    micro_momentum = 0.10 * ctx.prev_delta_bps
    return intent_drift + imbalance_fade + shock_response + micro_momentum


def mm_dna(ctx: DNAContext) -> float:
    """Market Maker: tight bracket, but liquidity shocks cause an
    immediate spike before fading.

    On a liquidity shock the MM widens spread and the next print fills
    at the new wider quote → price gaps in the direction of the imbalance.
    They re-tighten and pull price back almost immediately. This is the
    "Microstructure Lead": MM moves before arb has computed a new fair
    value, and the spike+revert is the alpha.
    """
    intent_drift   = 0.10 * ctx.base_strength * ctx.signed_intent
    bracket_pull   = -0.40 * ctx.prev_state_bps
    liquidity_kick = _perceived_sum(ctx, {"liquidity"}) * 1.20
    micro_kick     = _perceived_sum(ctx, {"micro"}) * 0.35
    return intent_drift + bracket_pull + liquidity_kick + micro_kick


def ta_dna(ctx: DNAContext) -> float:
    """Trend Follower / Momentum: amplifies prev_delta, doesn't see raw
    shocks (much).

    TA reads price action, not the underlying event. Information reaches
    it primarily through what other tribes have already done in prior
    steps (prev_delta) — this is the channel through which arb/MM moves
    cascade into TA-driven momentum. Includes a fatigue term so trends
    don't run forever.
    """
    intent_drift = 0.25 * ctx.base_strength * ctx.signed_intent
    momentum_amp = 0.65 * ctx.prev_delta_bps
    fatigue      = -0.05 * ctx.prev_state_bps if abs(ctx.prev_state_bps) > 200.0 else 0.0
    leaked       = _perceived_sum(ctx) * 0.20
    return intent_drift + momentum_amp + fatigue + leaked


def event_dna(ctx: DNAContext) -> float:
    """Event hunter (news / liquidations): one-shot impulse on
    macro/sentiment shocks at the latency offset.

    Quiet most of the time, then a sentiment or macro shock arrives
    (after the 2-step lag) and the tribe slams in a directional impulse
    aligned with intent. Decays slowly afterward.
    """
    intent_drift = 0.20 * ctx.base_strength * ctx.signed_intent
    impulse      = _perceived_sum(ctx, {"macro", "sentiment"}) * 1.10
    decay        = -0.08 * ctx.prev_state_bps
    return intent_drift + impulse + decay


def hedger_dna(ctx: DNAContext) -> float:
    """Options hedger / leverage cycle: lagged contrarian.

    Hedgers lean against directional moves to keep delta-neutral. Sees
    macro shocks at long lag, fades recent prev_delta to rebalance.
    """
    intent_drift   = 0.10 * ctx.base_strength * ctx.signed_intent
    contrarian     = -0.30 * ctx.prev_delta_bps
    macro_response = _perceived_sum(ctx, {"macro"}) * 0.45
    return intent_drift + contrarian + macro_response


def mev_dna(ctx: DNAContext) -> float:
    """MEV / Front-Runner: pre-positions in the direction of the micro or
    liquidity shock before arb/MM have fully repriced.

    Geometry: a sharp, same-direction-as-intent push at the instant the
    shock fires (high-magnitude impulse with zero latency), followed by
    immediate inventory-flat mean reversion — they extract and leave.
    Unlike arb, MEV doesn't fade the shock to fair value; they ride it.
    Unlike retail, they're done by the time retail even notices.
    """
    intent_drift  = 0.35 * ctx.base_strength * ctx.signed_intent
    # Shock response is the alpha — amplified aggressively when the shock
    # aligns with intent (they're front-running their own read), neutral
    # when it doesn't (they stand aside rather than fade).
    shock_raw = _perceived_sum(ctx, {"micro", "liquidity"})
    shock_response = shock_raw * 1.45 if shock_raw * ctx.signed_intent >= 0 else shock_raw * 0.35
    # Quick inventory flush — the position doesn't live long.
    flush = -0.55 * ctx.prev_state_bps
    return intent_drift + shock_response + flush


def whale_dna(ctx: DNAContext) -> float:
    """Passive Whale / ETF TWAP: inflexible, price-insensitive slicing.

    A whale running a TWAP treats the forecast horizon as ~flat: they
    contribute a steady signed pressure that doesn't chase prev_delta,
    doesn't fade state, and barely responds to shocks. The only thing
    that nudges them is long-latency macro news (fund flows follow
    headlines on the scale of hours, not seconds). In the 10-minute
    projection window they look like a constant-velocity lean.

    Geometry: flat line in the direction of intent, minimal curvature.
    """
    intent_drift = 0.55 * ctx.base_strength * ctx.signed_intent
    macro_nudge  = _perceived_sum(ctx, {"macro"}) * 0.15
    # Tiny anti-runaway so cumulative drift doesn't diverge — but weaker
    # than every other category, because whales really are price-insensitive.
    anchor = -0.02 * ctx.prev_state_bps
    return intent_drift + macro_nudge + anchor


def retail_dna(ctx: DNAContext) -> float:
    """Retail: chase prev_delta normally, panic-amplify when shocks
    breach the threshold or cum_bps grows large.

    Below panic floor: small intent drift + chase momentum.
    Above: quadratic amplification of prev_delta in the direction
    things are already moving — the "Crash" / "Breakout" geometry.
    Note: perceive_for_tribe has already filtered out below-threshold
    shocks via the profile's panic_threshold_bps, so any non-empty
    perceived list IS a panic trigger.
    """
    intent_drift = 0.15 * ctx.base_strength * ctx.signed_intent
    panic_now = bool(ctx.perceived) or abs(ctx.prev_state_bps) > 120.0
    if panic_now:
        chase     = 4.0 * ctx.prev_delta_bps
        # Quadratic state-overshoot kick: scales with state^2 in the
        # direction of state, normalized so a 100bps state contributes
        # a bps unit. Produces convex blow-out shapes.
        overshoot = 0.04 * ctx.prev_state_bps * abs(ctx.prev_state_bps) / 100.0
        # Direct shock entry — retail piles in the direction of any
        # perceived shock with extra urgency (FOMO entry / stop hunt).
        # Without this, a tribe with no prior momentum can't react to
        # a fresh shock, and retail would stay flat through any cascade
        # that reaches it.
        shock_panic = _perceived_sum(ctx) * 1.5
        return intent_drift + chase + overshoot + shock_panic
    return intent_drift + 0.40 * ctx.prev_delta_bps


_DNA_BY_CATEGORY: dict[str, Callable[[DNAContext], float]] = {
    "arb":            arb_dna,
    "microstructure": microstructure_dna,
    "mm":             mm_dna,
    "ta":             ta_dna,
    "event":          event_dna,
    "hedger":         hedger_dna,
    "retail":         retail_dna,
    "mev":            mev_dna,
    "whale":          whale_dna,
}


def dna_for(category: str) -> Callable[[DNAContext], float]:
    return _DNA_BY_CATEGORY.get(category, ta_dna)
