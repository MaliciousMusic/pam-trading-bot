"""Rolling-window view of recent market activity.

One process-wide instance is fed every normalized Event from the bus.
Tribes read from it at vote time — they don't keep private state unless
they genuinely need to (cooldowns, held positions).

All windows store `(epoch_seconds, value)` tuples and trim on the fly
to the largest requested window. Keep this lean; tribes that need
bespoke series can carry their own.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Iterable

from ..core.types import Event


class RollingSeries:
    """Time-stamped series, trimmed to a maximum age."""

    __slots__ = ("max_age_s", "_data")

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._data: deque[tuple[float, float]] = deque()

    def add(self, ts: datetime, value: float) -> None:
        t = ts.timestamp()
        self._data.append((t, value))
        cutoff = t - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def advance_clock(self, ts: datetime) -> None:
        """Trim stale entries relative to `ts` without adding a new sample.
        Used by the chess projector when the virtual clock moves forward."""
        cutoff = ts.timestamp() - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def _window(self, window_s: float | None) -> Iterable[tuple[float, float]]:
        if not self._data:
            return ()
        latest = self._data[-1][0]
        w = window_s if window_s is not None else self.max_age_s
        cutoff = latest - w
        return ((t, v) for t, v in self._data if t >= cutoff)

    def sum(self, window_s: float | None = None) -> float:
        return sum(v for _, v in self._window(window_s))

    def first_last(self, window_s: float | None = None) -> tuple[float, float] | None:
        xs = list(self._window(window_s))
        if not xs:
            return None
        return xs[0][1], xs[-1][1]

    def min_max(self, window_s: float | None = None) -> tuple[float, float] | None:
        xs = [v for _, v in self._window(window_s)]
        if not xs:
            return None
        return min(xs), max(xs)


class TaggedSeries:
    """Time-stamped tuples of (score, tags). Supports filtered aggregation.

    Used for news events where each item carries a bag of category tags
    (`regulation`, `influencer`, `business`, `twitter`, ...) and strategies
    want to query subsets — e.g. "only regulation/macro items in the last
    30 minutes" without scanning every news event in process.
    """

    __slots__ = ("max_age_s", "_data")

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._data: deque[tuple[float, float, frozenset[str]]] = deque()

    def add(self, ts: datetime, score: float, tags: Iterable[str]) -> None:
        t = ts.timestamp()
        self._data.append((t, score, frozenset(tags)))
        cutoff = t - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def advance_clock(self, ts: datetime) -> None:
        cutoff = ts.timestamp() - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def _iter(self, window_s: float | None, any_of: frozenset[str] | None):
        if not self._data:
            return
        latest = self._data[-1][0]
        w = window_s if window_s is not None else self.max_age_s
        cutoff = latest - w
        for t, s, tags in self._data:
            if t < cutoff:
                continue
            if any_of is not None and not (tags & any_of):
                continue
            yield s, tags

    def score(self, window_s: float, any_of: Iterable[str] | None = None) -> float:
        aof = frozenset(any_of) if any_of else None
        return sum(s for s, _ in self._iter(window_s, aof))

    def count(self, window_s: float, any_of: Iterable[str] | None = None) -> int:
        aof = frozenset(any_of) if any_of else None
        return sum(1 for _ in self._iter(window_s, aof))


class MarketState:
    """Shared read model the voting tribes query."""

    def __init__(self, max_age_s: float = 600.0) -> None:
        self.MAX_AGE_S = float(max_age_s)
        self.taker_flow = RollingSeries(self.MAX_AGE_S)   # signed notional (+buy, -sell)
        self.long_liq = RollingSeries(self.MAX_AGE_S)      # long-position liquidation notional
        self.short_liq = RollingSeries(self.MAX_AGE_S)     # short-position liquidation notional
        # Prices retained for 1h — covers swing_trend_follower's 30min long
        # window, leverage_cycle's 1h window, and the event-wave projector's
        # gaussian integration. Kept independent of MAX_AGE_S (which sits at
        # 600s for fast flow series) so hourly strategies see non-truncated data.
        self.prices = RollingSeries(3600.0)
        # Social/sentiment series — signed score ∈ [-1,+1] per reddit post / pulse.
        # Kept longer than market windows because headlines carry across minutes.
        self.social = RollingSeries(1800.0)                # 30min social pulse history
        # News series from curated RSS (SEC/Fed, Coindesk/Cointelegraph, substack,
        # Nitter mirrors). Tagged with category so strategies can filter:
        #   regulation_pulse() — SEC/CFTC/Fed weight
        #   influencer_pulse() — Twitter/Substack talkers
        #   business_pulse()   — Reuters/mainstream
        # Kept 2h because regulation items keep mattering for the whole session.
        self.news = TaggedSeries(7200.0)
        self.last_price: float | None = None
        self.last_mark_price: float | None = None
        self.last_funding_rate: float = 0.0
        self.fgi_value: int | None = None                  # 0..100
        self.fgi_classification: str = ""
        self.fgi_ts: datetime | None = None
        self.last_update: datetime | None = None
        # Top-of-book + depth snapshot from @depth20@100ms. The projector
        # kernel uses `bid_depth_5bps_usd` / `ask_depth_5bps_usd` to scale
        # pressure → bps: thin book amplifies, deep book absorbs. None
        # until the first book tick lands, in which case the projector
        # falls back to the constant kernel.
        self.last_best_bid: float | None = None
        self.last_best_ask: float | None = None
        self.last_mid: float | None = None
        self.last_spread_bps: float | None = None
        self.bid_depth_5bps_usd: float | None = None
        self.ask_depth_5bps_usd: float | None = None
        # Pending classified events (news with event_type != "none"). Each is
        # {"ts": epoch_s, "side": +/-1, "impact_bps": float, "event_type": str}.
        # Trimmed on ingest/advance_clock to items younger than `MAX_AGE_S`.
        # The event-wave projector reads this list to inject a Gaussian
        # propagation wave into its per-step pressure.
        self.pending_events: list[dict] = []
        # Phase F — per-venue OI history. Each series stores USD notional
        # samples; an hour of poll-cadence snapshots is plenty for the
        # "OI built up over the rally" / "OI drained during the flush"
        # heuristics that leverage_builder and capitulation_detector need.
        # Keyed by venue label (binance|bybit|okx|…).
        self.oi: dict[str, RollingSeries] = {}
        self.last_oi_usd: dict[str, float] = {}
        # Per-venue cross-venue taker flow (signed notional). Intentionally
        # NOT merged with `taker_flow` so Binance's own flow (the execution
        # venue's actual felt pressure) stays distinguishable from external
        # venues. Lets cross_venue_arb tribes detect divergence.
        self.cross_flow: dict[str, RollingSeries] = {}

    # -- ingest ---------------------------------------------------------

    def ingest(self, ev: Event) -> None:
        self.last_update = ev.ts
        if ev.kind == "trade":
            p = ev.payload.get("price")
            n = ev.payload.get("notional_usd", 0.0)
            signed = -n if ev.payload.get("is_sell_aggressor") else n
            self.taker_flow.add(ev.ts, signed)
            if p is not None:
                self.last_price = p
                self.prices.add(ev.ts, p)
        elif ev.kind == "liquidation":
            n = ev.payload.get("notional_usd", 0.0)
            if "liq_long" in ev.tags:
                self.long_liq.add(ev.ts, n)
            elif "liq_short" in ev.tags:
                self.short_liq.add(ev.ts, n)
        elif ev.kind == "funding":
            mp = ev.payload.get("mark_price")
            if mp:
                self.last_mark_price = mp
                if self.last_price is None:
                    self.last_price = mp
                self.prices.add(ev.ts, mp)
            self.last_funding_rate = ev.payload.get("funding_rate", 0.0)
        elif ev.kind == "social_pulse":
            score = float(ev.payload.get("score", 0.0))
            if score != 0.0:
                self.social.add(ev.ts, score)
        elif ev.kind == "sentiment":
            # FGI update — keep the latest value; it changes at most daily.
            v = ev.payload.get("value")
            if isinstance(v, (int, float)):
                self.fgi_value = int(v)
                self.fgi_classification = str(ev.payload.get("classification", ""))
                self.fgi_ts = ev.ts
        elif ev.kind == "book":
            p = ev.payload
            bb = p.get("best_bid"); ba = p.get("best_ask")
            if bb and ba and ba > bb > 0:
                self.last_best_bid = bb
                self.last_best_ask = ba
                self.last_mid = p.get("mid") or 0.5 * (bb + ba)
                self.last_spread_bps = p.get("spread_bps")
                self.bid_depth_5bps_usd = p.get("bid_depth_5bps_usd")
                self.ask_depth_5bps_usd = p.get("ask_depth_5bps_usd")
        elif ev.kind == "oi":
            # Per-venue OI snapshot. Persist both the USD series (for
            # delta queries) and the last value (for "sum across venues").
            venue = str(ev.payload.get("venue") or ev.source.split(".", 1)[0])
            usd = float(ev.payload.get("oi_usd") or 0.0)
            if usd > 0:
                series = self.oi.setdefault(venue, RollingSeries(3600.0))
                series.add(ev.ts, usd)
                self.last_oi_usd[venue] = usd
        elif ev.kind == "cross_trade":
            # Cross-venue taker flow. Store signed notional per venue.
            venue = str(ev.payload.get("venue") or ev.source.split(".", 1)[0])
            n = float(ev.payload.get("notional_usd") or 0.0)
            signed = -n if ev.payload.get("is_sell_aggressor") else n
            series = self.cross_flow.setdefault(venue, RollingSeries(self.MAX_AGE_S))
            series.add(ev.ts, signed)
        elif ev.kind == "news":
            # Signed score in [-1,+1] — RSS poller already clipped. Severity is
            # the source-weighted impact (SEC=1.0, HN=0.3) and becomes the
            # magnitude contribution — an SEC bear story (score=-0.5, sev=1.0)
            # contributes -0.5 while a random blog (score=-0.5, sev=0.3) feeds
            # -0.15. This is how we let Fed news swamp Substack chatter.
            sc = float(ev.payload.get("score", 0.0))
            sev = float(ev.severity or 0.0)
            effective = sc * sev if sc != 0 else 0.0
            if effective != 0 or "regulation" in ev.tags or "macro" in ev.tags:
                # Regulation/macro items still register at 0 score — their
                # occurrence alone is a regime signal for swing/news tribes.
                self.news.add(ev.ts, effective, ev.tags)
            # If the classifier tagged this as a structured event with a
            # calibrated impact_bps, enqueue it for the event-wave projector.
            # The projector will consume it by distributing impact across
            # the next ~hour of virtual steps, weighted by each tribe's
            # Gaussian reaction latency.
            et = ev.payload.get("event_type")
            imp = float(ev.payload.get("impact_bps", 0.0))
            side = int(ev.payload.get("side", 0))
            if et and et != "none" and imp > 0 and side != 0:
                self.pending_events.append({
                    "ts": ev.ts.timestamp(),
                    "side": side,
                    "impact_bps": imp,
                    "event_type": et,
                })
                # Keep the list bounded — 7200s matches the news window.
                cutoff = ev.ts.timestamp() - self.MAX_AGE_S * 12  # 2h worth
                self.pending_events = [
                    e for e in self.pending_events if e["ts"] >= cutoff
                ]

    # -- queries --------------------------------------------------------

    def taker_flow_usd(self, window_s: float) -> float:
        return self.taker_flow.sum(window_s)

    def long_liq_usd(self, window_s: float) -> float:
        return self.long_liq.sum(window_s)

    def short_liq_usd(self, window_s: float) -> float:
        return self.short_liq.sum(window_s)

    def price_change_bps(self, window_s: float) -> float:
        fl = self.prices.first_last(window_s)
        if fl is None or fl[0] == 0:
            return 0.0
        return (fl[1] - fl[0]) / fl[0] * 10_000.0

    def price_range(self, window_s: float) -> tuple[float, float] | None:
        return self.prices.min_max(window_s)

    # -- open interest --------------------------------------------------

    def oi_total_usd(self) -> float:
        """Sum of the latest per-venue OI snapshots. 0 until at least one
        poll has landed; missing venues contribute 0 (partial view is
        acceptable — tribes treat missing data as no-signal)."""
        return float(sum(self.last_oi_usd.values()))

    def oi_delta_pct(self, window_s: float) -> float:
        """% change in summed OI over `window_s`. Positive = leverage
        building. Approach: take per-venue first_last within the window,
        sum both sides, compute (end-start)/start. Returns 0.0 if we
        don't have a non-zero start."""
        start_total = 0.0
        end_total = 0.0
        for series in self.oi.values():
            fl = series.first_last(window_s)
            if fl is None:
                continue
            start_total += fl[0]
            end_total += fl[1]
        if start_total <= 0:
            return 0.0
        return (end_total - start_total) / start_total * 100.0

    def oi_by_venue_usd(self) -> dict[str, float]:
        """Snapshot of the last-seen OI per venue. Empty until polls land."""
        return dict(self.last_oi_usd)

    # -- cross-venue flow -----------------------------------------------

    def cross_venue_flow_usd(self, venue: str, window_s: float) -> float:
        """Signed taker flow at an external venue. Positive = buy-aggression
        dominates. Returns 0.0 if the venue has no data yet."""
        series = self.cross_flow.get(venue)
        return series.sum(window_s) if series else 0.0

    def cross_venue_divergence(self, window_s: float) -> float:
        """Signed divergence between Binance flow and the external-venue
        average. Positive = Binance more aggressive than the rest (often
        precedes a spread-close move against Binance). Zero when we
        have no cross-venue data.

        Normalized by the absolute total flow so the value stays in
        [-1, +1] regardless of volume scale."""
        if not self.cross_flow:
            return 0.0
        venues = list(self.cross_flow.keys())
        flows = [self.cross_flow[v].sum(window_s) for v in venues]
        external_mean = sum(flows) / max(1, len(flows))
        binance_flow = self.taker_flow.sum(window_s)
        diff = binance_flow - external_mean
        denom = abs(binance_flow) + sum(abs(f) for f in flows)
        if denom <= 0:
            return 0.0
        return max(-1.0, min(1.0, diff / denom))

    # -- sentiment queries ----------------------------------------------

    def social_score(self, window_s: float) -> float:
        """Summed signed sentiment over `window_s`. Positive = bullish chatter."""
        return self.social.sum(window_s)

    def social_volume(self, window_s: float) -> int:
        """Raw count of scored social pulses in the window."""
        # RollingSeries exposes trimming via sum path; reuse _window().
        return sum(1 for _ in self.social._window(window_s))

    def fgi_bias(self) -> float:
        """Return FGI as a signed bias in [-1,+1] — positive = greed, negative = fear.
        Zero when unknown."""
        if self.fgi_value is None:
            return 0.0
        return (self.fgi_value - 50) / 50.0

    # -- news queries (tagged RSS) ---------------------------------------

    def news_score(self, window_s: float, tag: str | None = None) -> float:
        """Signed, severity-weighted sum of news scores in `window_s`.
        Positive = bullish headlines dominate. If `tag` is given, filter
        to items carrying that tag (e.g. 'regulation', 'influencer')."""
        return self.news.score(window_s, [tag] if tag else None)

    def news_volume(self, window_s: float, tag: str | None = None) -> int:
        """Raw count of news items matching `tag` within the window."""
        return self.news.count(window_s, [tag] if tag else None)

    def regulation_pulse(self, window_s: float = 3600.0) -> float:
        """Signed score from SEC/Fed/CFTC items only. High-impact regime shifts."""
        return self.news.score(window_s, ["regulation", "macro"])

    def influencer_pulse(self, window_s: float = 1800.0) -> float:
        """Signed score from substack/Twitter talkers. Retail copy-traders
        react to this before they react to price."""
        return self.news.score(window_s, ["influencer", "twitter"])

    def business_pulse(self, window_s: float = 3600.0) -> float:
        """Signed score from Reuters/mainstream business coverage — often
        the vector for TradFi attention spikes."""
        return self.news.score(window_s, ["business", "mainstream"])

    # -- projection support --------------------------------------------

    def snapshot(self) -> "MarketState":
        """Full copy so the chess projector can mutate freely without
        corrupting the live read model."""
        s = MarketState(self.MAX_AGE_S)
        s.taker_flow._data = deque(self.taker_flow._data)
        s.long_liq._data = deque(self.long_liq._data)
        s.short_liq._data = deque(self.short_liq._data)
        s.prices._data = deque(self.prices._data)
        s.social._data = deque(self.social._data)
        s.news._data = deque(self.news._data)
        s.last_price = self.last_price
        s.last_mark_price = self.last_mark_price
        s.last_funding_rate = self.last_funding_rate
        s.fgi_value = self.fgi_value
        s.fgi_classification = self.fgi_classification
        s.fgi_ts = self.fgi_ts
        s.last_update = self.last_update
        s.last_best_bid = self.last_best_bid
        s.last_best_ask = self.last_best_ask
        s.last_mid = self.last_mid
        s.last_spread_bps = self.last_spread_bps
        s.bid_depth_5bps_usd = self.bid_depth_5bps_usd
        s.ask_depth_5bps_usd = self.ask_depth_5bps_usd
        s.pending_events = list(self.pending_events)  # shallow copy of dicts
        # Phase F — deep-copy per-venue series so the projector's virtual
        # clock can trim them independently.
        for venue, series in self.oi.items():
            ns = RollingSeries(series.max_age_s)
            ns._data = deque(series._data)
            s.oi[venue] = ns
        for venue, series in self.cross_flow.items():
            ns = RollingSeries(series.max_age_s)
            ns._data = deque(series._data)
            s.cross_flow[venue] = ns
        s.last_oi_usd = dict(self.last_oi_usd)
        return s

    def advance_clock(self, ts: datetime) -> None:
        self.taker_flow.advance_clock(ts)
        self.long_liq.advance_clock(ts)
        self.short_liq.advance_clock(ts)
        self.prices.advance_clock(ts)
        self.social.advance_clock(ts)
        self.news.advance_clock(ts)
        for series in self.oi.values():
            series.advance_clock(ts)
        for series in self.cross_flow.values():
            series.advance_clock(ts)
        self.last_update = ts
