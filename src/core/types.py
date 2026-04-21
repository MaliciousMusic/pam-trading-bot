"""Core data types shared across modules.

Every module that speaks on the bus or returns results uses these shapes.
Add fields only when a tribe or downstream module genuinely needs them —
the types are a contract.

Phase 1 — migrated from @dataclass(slots=True) to msgspec.Struct.
msgspec.Struct is slot-equivalent (fixed layout, no __dict__), mutable by
default, and gains native msgspec.json / msgspec.msgpack round-trip
without the Encoder.enc_hook dance. Field order, defaults, and the
keyword-only construction convention are preserved verbatim so the 60+
call sites across ingress/tribes/projector/execution stay drop-in.

We do NOT set gc=False on these Structs: Event.payload is typed
dict[str, Any] and websocket adapters do occasionally stash arbitrary
objects in it; turning off cycle-collection there is a Phase 2+ audit
after a heap profile, not a Phase 1 blind win.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from msgspec import Struct, field


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    ABSTAIN = "abstain"


class Event(Struct):
    """A normalized market-relevant happening placed on the bus.

    `tags` is the attention-filter surface: tribes subscribe by tag.
    `severity` is a coarse 0..1 estimate of how big the stimulus is, used
    for sampling and for tribes whose attention threshold is high (pros
    see everything, noobs only react above some severity).
    """
    ts: datetime
    source: str          # e.g. "binance.aggTrade"
    kind: str            # e.g. "trade", "liquidation", "funding", "news"
    symbol: str | None
    payload: dict[str, Any]
    tags: set[str] = field(default_factory=set)
    severity: float = 0.0

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(timezone.utc)


class Vote(Struct):
    """A single tribe's decision for a tick.

    `conviction` is the net directional signal (activation-rate-scaled).
    `buy_frac` / `sell_frac` are the fractions of the tribe's weight that
    respectively buy and sell this tick, derived from the internal distribution:
        active = activation_rate
        buy_frac  = active * (0.5 + raw_conviction/2)   [if side=BUY]
        sell_frac = active * (0.5 - raw_conviction/2)   [if side=BUY]
    This ensures the opposing minority within each tribe contributes to the
    opposite mass in aggregation, eliminating the monolithic-vote bias.
    """
    tribe_id: str
    side: Side
    conviction: float    # net directional strength, activation-scaled (0..1)
    horizon_seconds: int # how long the tribe expects to hold
    weight: float        # the tribe's current aggregate weight (flow share)
    reason: str = ""     # short string for audit
    buy_frac: float = 0.0   # fraction of weight that buys this tick
    sell_frac: float = 0.0  # fraction of weight that sells this tick


class Aggregate(Struct):
    """Weighted synthesis of all tribe votes for a single tick."""
    ts: datetime
    net_pressure: float  # -1..+1, signed by side, weighted by conviction*weight
    buy_mass: float
    sell_mass: float
    abstain_mass: float
    vote_count: int
    uncertainty: float   # disagreement measure


class Forecast(Struct):
    """Output of the chess projector for a single decision tick."""
    ts: datetime
    steps: list[Aggregate]       # per-step forward aggregates
    expected_move_bps: float     # cumulative expected move, basis points
    confidence: float            # decays with depth
    horizon_seconds: int
    # Wiggly-path enrichment — optional, all three default None so existing
    # callers/tests stay untouched. `steps_bps` is the cumulative expected
    # move at each step (monotonic drift); `steps_sigma_bps` is the stddev
    # around that drift derived from the backscorer; `sample_paths_bps` is
    # a bundle of Monte Carlo realizations drawn using per-step sigma.
    steps_bps: list[float] | None = None
    steps_sigma_bps: list[float] | None = None
    sample_paths_bps: list[list[float]] | None = None
    # Fraction of per-tribe MC paths whose terminal bps shares the sign of
    # `expected_move_bps`. 1.0 = every path agreed, 0.5 = total ambiguity.
    # Populated only when the projector is configured for per-tribe sampling;
    # the decision engine uses this as a size multiplier.
    directional_agreement: float | None = None
    # Sample-path forecast curve (median) and per-path terminals for audit.
    sample_terminals_bps: list[float] | None = None
    # Tribe id for each entry in `sample_paths_bps` — lets the UI color
    # each wiggly path by its archetype. Parallel-indexed with paths.
    sample_paths_tribe_ids: list[str] | None = None
    # Phase 1b — Council entropy (diversity score) ∈ [0, 1]. Shannon entropy
    # of the dominant-side directional mass across tribe categories,
    # normalized by log(n_active_categories). 1.0 = perfectly spread across
    # orthogonal categories (MM + Event + Retail + TA all agree); ~0 = one
    # cluster (e.g. 4 TA tribes). The decision engine derives confidence
    # shrink from this to defend against 'TA-bloc' dominance.
    diversity_score: float | None = None
    # Phase 1c — Per-category directional mass breakdown. Keys are the
    # categories used in chess._TRIBE_CATEGORY; each value is
    # {"buy": buy_mass, "sell": sell_mass}. Lets the decision engine
    # shrink size when a high-conf buy is actually coming from retail only.
    category_mass: dict[str, dict[str, float]] | None = None
    # Phase 3c — Weighted-average horizon (seconds) of the voters behind
    # the initial consensus. Shrinks when the council is dominated by
    # short-horizon scalpers; used for volatility-adjusted deleveraging.
    avg_voter_horizon_s: float | None = None
    # Task 4c — Path-Integrity. Pool-weight-weighted average of calibrator
    # skill multiplier across tribes voting WITH the consensus direction.
    # ~1.0 = the signal is carried by tribes the calibrator rates neutral
    # or better; < 0.7 = the consensus is being dragged by tribes pinned
    # to the floor (TA Cemetery, over-eager retail). Decision engine uses
    # this to refuse leverage *amplification* (agree_mult, crowd mults)
    # when the path's drivers aren't themselves trustworthy — a loud
    # consensus of bad predictors is still a bad prediction.
    driver_skill_avg: float | None = None
    # Task 4e — Harvest Mass Boost. `harvest_mass_ratio` is the share of
    # pool weight currently sitting in contrarian-flipped tribes (∈ [0,1]);
    # `conviction_boost` is the multiplier applied to the cumulative path
    # (≥ 1.0, capped at 1.5 by the harvest_scale constant in chess.py).
    # Published so the UI can surface the "we're harvesting dumb money"
    # state and the decision engine can audit how much of a move came
    # from the boost vs. the raw consensus.
    harvest_mass_ratio: float | None = None
    conviction_boost: float | None = None
    # Realized-volatility sanity rail (Task 2 — volatility rail). Total-window
    # sigma of log-returns in bps, computed from MarketState.prices over a
    # fixed 14-min window at project() time. Executors clamp the forecast's
    # expected_move_bps at entry to 1.25× this value so a predatory harvest
    # boost can't stamp a position with a target beyond what market vol has
    # produced recently. None when there aren't enough price samples yet.
    realized_vol_bps: float | None = None


class Position(Struct):
    """One open position inside the portfolio. Every open trade has its own
    entry price, size, TP/SL, and lifecycle — the executor auto-closes when
    mark-price hits either bracket or the horizon backstop fires."""
    id: str                        # short tag "tXXXX" assigned at open
    side: Side                     # BUY (long) or SELL (short), never ABSTAIN
    size_usd: float
    entry_price: float
    entry_ts: datetime
    tp_price: float                # take-profit mark (direction-correct)
    sl_price: float                # stop-loss mark (direction-correct)
    horizon_seconds: int
    # Audit metadata — what the forecast said at entry
    expected_move_bps: float = 0.0
    confidence: float = 0.0
    # Shape-aware exit — seconds-from-entry at which the projected favorable
    # move peaked. Executor closes the position if held significantly past
    # this point (see PaperExecutor.shape_grace_seconds). 0 disables.
    peak_favorable_s: int = 0
    # Predatory-harvest trust premium that inflated this entry's expected
    # move (Task 1 — de-coupling conviction from distance). Stamped from
    # forecast.conviction_boost at open. The forecast-target TP exit layer
    # divides expected_move_bps by this to recover the raw consensus move
    # before sizing its target — without it, a 1.5× harvest boost sets a TP
    # 50% farther out than the council's unbiased prediction.
    conviction_boost: float = 1.0
    # Shadow-ledger ownership. None on the real council portfolio; set to the
    # tribe id when the position lives inside a TribeShadowLedger so audit/UI
    # can split equity curves without threading a second Position subclass.
    tribe_id: str | None = None

    @property
    def is_flat(self) -> bool:
        # Kept for backwards-compat with callers that still reason about a
        # single logical position; an entry in the portfolio is never flat.
        return False


class TradeIntent(Struct):
    """Decision-layer output handed to the executor. In the portfolio model
    the decision engine only emits entry intents; closes happen inside the
    executor when TP/SL/horizon triggers. Legacy `close` action is kept for
    callers that still need a manual unwind path."""
    ts: datetime
    action: str                  # "open_long" | "open_short" | "close" | "hold"
    size_usd: float
    price_ref: float
    reason: str
    expected_move_bps: float
    confidence: float
    # Pre-computed brackets for the new position (ignored for close/hold).
    tp_price: float = 0.0
    sl_price: float = 0.0
    horizon_seconds: int = 0
    # Shape-aware exit metadata (see Position.peak_favorable_s).
    peak_favorable_s: int = 0
    # Forwarded from Forecast so the executor can (a) stamp it onto the new
    # Position for exit-time de-boosting and (b) apply the volatility sanity
    # rail at entry. Defaults keep older callers working.
    conviction_boost: float = 1.0
    realized_vol_bps: float | None = None
