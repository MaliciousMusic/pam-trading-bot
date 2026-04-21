"""Phase 10-Harden — "Merciless mode" execution overlay.

This module bolts a three-layer adversarial model on top of the existing
`PaperExecutor` fill path. Each layer is independently togglable via
`HardenConfig`; when the config's `enabled` flag is `False` the executor
behaves exactly as it did pre-Harden (the call sites short-circuit on
the `enabled` check). When `enabled=True` the three layers stack as:

  1. **Adversarial fill** — an additive bps term added to the half-spread
     + depth fill bps. Modeled as a fraction of the short-window realized
     vol plus a floor so even a dead market still charges some adverse
     move. Captures the "MM saw your order first and your fill landed on
     the wrong side of the 1 s decision-to-fill window" cost.

  2. **Fat-tailed latency** — with probability `p_spike`, a Pareto draw
     (heavy tail); otherwise an exponential with a small mean. At 1 Hz
     tick cadence, latencies below `tick_seconds` are absorbed as
     "normal network noise" and don't defer. Latencies ≥ `tick_seconds`
     defer the fill by `ceil(latency / tick_seconds)` ticks — the intent
     sits in a queue and fills at a future tick's mark (which has moved
     against us) with the adversarial layer applied *again*.

  3. **Sqrt-slippage overlay** — multiplies the final depth-bps term by
     `1 + sqrt_k * sqrt(size_usd / sqrt_ref_usd)`. The existing
     count-based `slippage_size_k` is linear in size; the sqrt-overlay
     bites medium-to-large orders harder, matching empirical Almgren-
     Chriss-style impact on liquid crypto perps.

The RNG is **seeded** and **deterministic** — the shock-report bench
reproduces numbers exactly. The seed is folded from the global
`numpy.random.default_rng` family so it plays nicely with the rest of
the project's determinism story.

None of this module mutates global state. The config is a frozen
dataclass; the RNG lives on the `HardenState` instance owned by the
executor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt, log
from typing import Optional

import numpy as np


# --- config --------------------------------------------------------------

@dataclass(frozen=True)
class HardenConfig:
    """Frozen knob set for Merciless mode.

    Every knob defaults to an *off* or *neutral* value. The `enabled`
    flag is the master switch: when False the executor skips all harden
    logic entirely. When True, each sub-layer is further gated by its
    own multiplier or probability (so you can disable, say, only the
    latency layer by setting `p_spike=0.0` and `normal_mean_s=0.0`
    while keeping adversarial fill on).
    """

    enabled: bool = False

    # --- Layer 1: adversarial fill ---
    # Fraction of short-window realized vol that becomes an adverse bps
    # hit on every fill. 0.5 means "you lose half of the 1 s realized
    # vol in the bad direction on every fill".
    adv_mult: float = 0.5
    # Floor in bps — even if realized vol is 0, you still pay this.
    # Models the "market saw your order" cost independently of volatility.
    adv_floor_bps: float = 1.5
    # Realized-vol window used for the multiplicand, in seconds. 30 s
    # matches the black-swan guard's short window.
    adv_vol_window_s: float = 30.0
    # Hard cap on the adversarial layer alone. Prevents a corrupted vol
    # reading from producing a 10 000 bp adverse fill.
    adv_cap_bps: float = 20.0

    # --- Layer 2: fat-tailed latency ---
    # Probability of a spike draw vs. a normal exponential draw.
    p_spike: float = 0.02
    # Exponential mean for the non-spike regime, in seconds.
    normal_mean_s: float = 0.08
    # Pareto scale (minimum) and alpha (tail heaviness) for the spike
    # regime. alpha=1.3 means `E[X]=scale/0.3 ≈ 5 s`, with a very heavy
    # right tail (occasional 10-30 s stalls).
    spike_scale_s: float = 1.5
    spike_alpha: float = 1.3
    # Hard cap on any single draw. Protects against freak Pareto tails
    # that would park an intent for minutes.
    spike_cap_s: float = 30.0

    # --- Layer 3: sqrt-slippage overlay ---
    # Multiplier on the final depth-bps: depth_bps *= 1 + sqrt_k *
    # sqrt(size_usd / sqrt_ref_usd). 0.0 disables the overlay.
    sqrt_k: float = 0.8
    sqrt_ref_usd: float = 100_000.0

    # --- Determinism ---
    # Seed the harden RNG. Set per-sprint so back-to-back runs produce
    # identical harden-layer draws.
    seed: int = 42

    @classmethod
    def from_yaml(cls, d: dict | None) -> "HardenConfig":
        """Build from a settings.yaml `harden:` block, tolerating missing
        keys (they keep their defaults). Unknown keys are ignored so the
        yaml can carry commentary-only fields."""
        if not d:
            return cls()
        known = {
            "enabled", "adv_mult", "adv_floor_bps", "adv_vol_window_s",
            "adv_cap_bps", "p_spike", "normal_mean_s", "spike_scale_s",
            "spike_alpha", "spike_cap_s", "sqrt_k", "sqrt_ref_usd", "seed",
        }
        payload = {k: v for k, v in d.items() if k in known}
        return cls(**payload)


# --- runtime state -------------------------------------------------------

@dataclass
class PendingFill:
    """A queued open intent waiting for its latency-shifted fill tick.

    The executor's `tick(ts, state)` method drains these when `fill_due_ts
    <= ts`. When the fill finally lands, `_fill_price` is called a second
    time against the *current* mark (not the intended mark at decision
    time), so the adversarial layer gets a fresh draw at a new price.
    This is exactly what a real book would do to you: your intent shows
    up late, the market has moved, you pay what the book asks right now.
    """

    intent: object                      # TradeIntent — typed loosely to avoid import cycle
    intended_mark: float                # mark at decision time (for reporting)
    decision_ts: object                 # datetime at decision time
    fill_due_ts: object                 # datetime the intent is eligible to fill
    drawn_latency_s: float              # the actual latency sample (for telemetry)
    was_spike: bool                     # True iff sampled from the Pareto regime


@dataclass
class HardenState:
    """Per-executor runtime state for Merciless mode. Owns the RNG and
    the telemetry counters. A `HardenState(None)` (enabled=False) is a
    no-op — all methods return neutral values or early-exit.
    """

    cfg: HardenConfig
    _rng: np.random.Generator = field(init=False)

    # Telemetry — accumulated over the session.
    adv_usd_total: float = 0.0          # dollars paid to the adversarial layer
    sqrt_overlay_usd_total: float = 0.0 # dollars paid to the sqrt overlay
    latency_spikes: int = 0             # count of fills actually deferred (latency ≥ tick_seconds)
    latency_samples: int = 0            # total fills that had a latency draw
    latency_total_s: float = 0.0        # sum of all drawn latencies
    pareto_draws: int = 0               # how many times we hit the spike regime
    deferred_fills: int = 0             # count of fills that actually landed on a later tick
    slip_bps_sum: float = 0.0           # sum of total_bps across all fills (for avg)
    slip_fills_n: int = 0               # count of fills (for the average)

    def __post_init__(self) -> None:
        # Use numpy's modern bit-generator pipeline. PCG64 is fast,
        # statelessly seedable, and reproducible across platforms.
        self._rng = np.random.default_rng(self.cfg.seed)

    # --- Layer 1 helper -------------------------------------------------

    def adversarial_bps(self, realized_vol_bps: float | None) -> float:
        """Return the adversarial bps to add to the fill price.

        `realized_vol_bps` is the short-window realized stddev of bps
        returns (the executor passes this in; computing it requires the
        MarketState, which harden.py deliberately doesn't import). None
        or zero is fine — the floor still applies.
        """
        if not self.cfg.enabled:
            return 0.0
        v = 0.0 if realized_vol_bps is None else max(0.0, float(realized_vol_bps))
        raw = self.cfg.adv_mult * v + self.cfg.adv_floor_bps
        return min(self.cfg.adv_cap_bps, raw)

    # --- Layer 2 helper -------------------------------------------------

    def sample_latency_s(self) -> tuple[float, bool]:
        """Draw one latency sample. Returns (latency_s, was_spike).

        Mixture: with probability `p_spike`, draw from Pareto(scale, alpha);
        otherwise exponential with mean `normal_mean_s`. Both are clipped
        at `spike_cap_s` to guarantee no pathological tail can stall a
        fill for minutes.

        Callers should advance the telemetry (`latency_samples`,
        `latency_total_s`, etc.) themselves — this helper is pure draw.
        """
        if not self.cfg.enabled:
            return 0.0, False
        u = float(self._rng.random())
        if u < self.cfg.p_spike:
            # Pareto draw: scale × (1 - U)^(-1/alpha).  We re-roll U so
            # the same `u` above isn't re-used (clean: one U per layer).
            u2 = float(self._rng.random())
            # Guard u2 away from 1.0 so the pow doesn't blow up. 1 - u2
            # is in (0, 1] so log is fine; clamp lower bound.
            denom = max(1e-9, 1.0 - u2)
            draw = self.cfg.spike_scale_s * (denom ** (-1.0 / self.cfg.spike_alpha))
            was_spike = True
        else:
            # Exponential inverse-CDF: -mean × ln(1 - U). Same guard.
            u2 = float(self._rng.random())
            denom = max(1e-9, 1.0 - u2)
            draw = -self.cfg.normal_mean_s * log(denom)
            was_spike = False
        draw = min(self.cfg.spike_cap_s, max(0.0, draw))
        return draw, was_spike

    # --- Layer 3 helper -------------------------------------------------

    def sqrt_overlay_mult(self, size_usd: float) -> float:
        """Return the multiplier applied to the existing depth-bps.

        Always >= 1.0 when enabled. Returns exactly 1.0 when the layer
        is off so callers can unconditionally multiply.
        """
        if not self.cfg.enabled or self.cfg.sqrt_k <= 0.0 or size_usd <= 0.0:
            return 1.0
        ref = max(1.0, float(self.cfg.sqrt_ref_usd))
        return 1.0 + self.cfg.sqrt_k * sqrt(size_usd / ref)

    # --- Telemetry snapshot --------------------------------------------

    def snapshot(self) -> dict:
        """Serializable summary for the shock report and the UI footer."""
        avg_latency = (self.latency_total_s / self.latency_samples
                       if self.latency_samples > 0 else 0.0)
        avg_slip_bps = (self.slip_bps_sum / self.slip_fills_n
                        if self.slip_fills_n > 0 else 0.0)
        return {
            "enabled": self.cfg.enabled,
            "adv_usd_total": round(self.adv_usd_total, 4),
            "sqrt_overlay_usd_total": round(self.sqrt_overlay_usd_total, 4),
            "latency_spikes": self.latency_spikes,
            "latency_samples": self.latency_samples,
            "pareto_draws": self.pareto_draws,
            "deferred_fills": self.deferred_fills,
            "avg_latency_s": round(avg_latency, 4),
            "avg_slip_bps": round(avg_slip_bps, 3),
            "cfg": {
                "adv_mult": self.cfg.adv_mult,
                "adv_floor_bps": self.cfg.adv_floor_bps,
                "adv_vol_window_s": self.cfg.adv_vol_window_s,
                "p_spike": self.cfg.p_spike,
                "normal_mean_s": self.cfg.normal_mean_s,
                "spike_scale_s": self.cfg.spike_scale_s,
                "spike_alpha": self.cfg.spike_alpha,
                "sqrt_k": self.cfg.sqrt_k,
                "sqrt_ref_usd": self.cfg.sqrt_ref_usd,
                "seed": self.cfg.seed,
            },
        }


def realized_vol_bps_from_state(state, window_s: float) -> Optional[float]:
    """Compute short-window realized vol in bps from a MarketState.

    Imported lazily inside the function so `harden.py` stays independent
    of the tribes tree. Uses the same primitive as `market_state.realized_vol_bps`
    (Phase 2 NumPy path), just with an explicit window.

    Returns None when there isn't enough data. Callers should fall back
    to `None`/the floor on that branch.
    """
    prices = getattr(state, "prices", None)
    if prices is None:
        return None
    # Reuse the RollingSeries primitive. _slice_arrays returns
    # (ts_arr, vs_arr) for the trailing window; np.diff(log) gives log
    # returns, std * 1e4 converts to bps.
    try:
        _, vs = prices._slice_arrays(float(window_s))
    except Exception:
        return None
    if vs is None or vs.size < 3:
        return None
    # Filter non-positive prints defensively (log would blow up).
    vs_f = vs[vs > 0]
    if vs_f.size < 3:
        return None
    rets = np.diff(np.log(vs_f))
    if rets.size < 2:
        return None
    return float(rets.std(ddof=1) * 10_000.0)
