"""Declarative tribe specifications.

Every block allows extra fields (`extra="allow"`) so we can add new
characteristics (hardware specs, jurisdiction, broker fees, preferred
order types, etc.) without breaking existing YAMLs. Fields that the
strategy layer doesn't read today still travel with the tribe for
documentation, future evolution, and audit.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class Demographics(_Base):
    capital_band_usd: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    count_estimate: int | None = None
    experience_years: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    jurisdiction: list[str] = Field(default_factory=list)


class Hardware(_Base):
    tier: str = "laptop"                       # phone | laptop | workstation | colo
    typical_latency_ms: int = 1000
    uptime: str = "intermittent"               # intermittent | continuous
    specs: dict[str, str] = Field(default_factory=dict)


class Software(_Base):
    platforms: list[str] = Field(default_factory=list)   # e.g. ["binance_app", "tradingview"]
    tools: list[str] = Field(default_factory=list)       # e.g. ["custom_py_bot", "hummingbot"]
    charting: str = "none"                                # none | basic | advanced | custom
    order_types_used: list[str] = Field(default_factory=list)
    data_feeds: list[str] = Field(default_factory=list)  # which streams they actually ingest


class StrategySpec(_Base):
    family: str                                # key into the strategy registry
    params: dict[str, Any] = Field(default_factory=dict)
    style_notes: str = ""


class Attention(_Base):
    """What the tribe actually perceives. Documentation-first for now —
    strategies implement the filtering in code — but evolution can later
    mutate these fields to spawn variant tribes that 'pay attention' to
    different signals."""
    kinds: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    ignored_tags: list[str] = Field(default_factory=list)
    min_severity: float = 0.0


class Personality(_Base):
    risk_tolerance: float = 0.5
    fomo: float = 0.5
    panic: float = 0.5
    patience: float = 0.5
    herding: float = 0.5


class Flow(_Base):
    # `weight` is the *derived* per-tick flow share. It is overwritten at load
    # time by derive.py using `count_estimate × geometric_mid(capital_band_usd)
    # × activation_rate`, then normalized across all tribes. A value written
    # here is a fallback used only when the tribe lacks the inputs needed to
    # derive one (missing count/capital/rate).
    weight: float = 0.0
    # Fraction of the population that expresses a directional view during
    # a single tick. 1.0 = every member trading every tick (colo MMs); 0.005
    # = once-per-week newbie retail. Calibrated from public aggregates
    # (Binance perps volume, Kaiko flow reports, Glassnode active-addresses)
    # — see per-tribe YAML comments for source reasoning.
    activation_rate: float = 0.1
    typical_hold_seconds: list[int] = Field(default_factory=lambda: [60, 600])
    default_conviction: float = 0.5
    cooldown_seconds: int = 0                              # min gap between STRONG votes
    strong_vote_conviction: float = 0.3                    # only convictions ≥ this trigger cooldown


class Disposition(_Base):
    """Idle posture. Every archetype has a stance at all times — even when
    their signal is quiet. This captures it.

      idle_bias ∈ [-1, +1]      — direction when nothing specific is happening
      idle_conviction ∈ [0, 1]  — how strongly they hold that idle view (typically small)

    Strategies can override this with data-dependent idle behavior (e.g. a
    trend-follower's idle is the sign of the longer-term trend) — the spec
    value is the default when no better information is available.
    """
    idle_bias: float = 0.0
    idle_conviction: float = 0.08


class Circadian(_Base):
    """Time-of-day participation. Humans sleep; bots don't. Different
    timezones wake up at different UTC hours, so each tribe's share of the
    order flow isn't constant over a 24h window.

      presence_by_hour_utc[h] ∈ [0, 1]  — fraction of full weight the tribe
        contributes when UTC hour == h. Runtime multiplies `flow.weight`
        by this factor per-tick.

    Default is 24×1.0 (always-on), matching bot/24-7 archetypes. Retail
    tribes should drop to 0.2–0.4 during their home-region sleep hours.
    The `timezone_label` is documentation only — it's not parsed.
    """
    timezone_label: str = "global"                      # APAC | EU | US | global | 24/7
    presence_by_hour_utc: list[float] = Field(
        default_factory=lambda: [1.0] * 24
    )


class TribeSpec(_Base):
    id: str
    name: str
    description: str = ""
    demographics: Demographics = Field(default_factory=Demographics)
    hardware: Hardware = Field(default_factory=Hardware)
    software: Software = Field(default_factory=Software)
    strategy: StrategySpec
    attention: Attention = Field(default_factory=Attention)
    personality: Personality = Field(default_factory=Personality)
    flow: Flow = Field(default_factory=Flow)
    disposition: Disposition = Field(default_factory=Disposition)
    circadian: Circadian = Field(default_factory=Circadian)
    # Declarative indicator lens. Each entry is
    #   <indicator_name>: {<params>, weight: <float>}
    # with params matching the indicator's signature in
    # src/tribes/indicators.py (window_s, extreme, action, …). The strategy
    # reads the blended signal via Tribe.indicator_blend(state) and is free
    # to combine it with its core logic at any weight it chooses. This
    # block is the place to capture the archetype's "view of the tape" in
    # range-of-use form — what windows it watches, what thresholds trigger
    # an opinion, whether it fades or follows. Leave empty to skip the blend.
    indicators: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # How hard the indicator-blend pushes the strategy's vote, [0,1]. 0 =
    # indicators are decorative (voter still calls strat logic). 1 = blend
    # fully co-signs the strat's signed signal.
    indicator_weight: float = 0.5
