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
        self._add_epoch(ts.timestamp(), value)

    def _add_epoch(self, t: float, value: float) -> None:
        """Hot-path variant of add() — the projector's inner loop reuses the
        same step-epoch it already computed for _trim_to_epoch, avoiding a
        second ts.timestamp() per simulated price append."""
        self._data.append((t, value))
        cutoff = t - self.max_age_s
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def advance_clock(self, ts: datetime) -> None:
        """Trim stale entries relative to `ts` without adding a new sample.
        Used by the chess projector when the virtual clock moves forward."""
        self._trim_to_epoch(ts.timestamp())

    def _trim_to_epoch(self, t: float) -> None:
        """Hot-path variant of advance_clock. Callers that already hold the
        epoch-float (projector loop) use this to skip a redundant
        datetime.timestamp() call — profiling showed ~3.6M such calls per
        200-tick run, ~15% of wall-clock. Same trimming semantics."""
        cutoff = t - self.max_age_s
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
        self._trim_to_epoch(ts.timestamp())

    def _trim_to_epoch(self, t: float) -> None:
        cutoff = t - self.max_age_s
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
        # Epoch seconds of the most recent aggTrade print. Mark price
        # (1s cadence) refreshes last_price when this goes stale — aggTrade
        # silence from a WS hiccup / reconnect would otherwise flatline the
        # chart for seconds to minutes with a frozen last_price.
        self._last_trade_ts: float | None = None
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
        # Phase 12 — short rolling histories for the silent microstructure
        # tribe. Depth deltas (queue-position decay) and per-trade notionals
        # (trade-size clustering / iceberg detection) are window-derived.
        self.bid_depth_series = RollingSeries(120.0)
        self.ask_depth_series = RollingSeries(120.0)
        # Per-trade signed notionals (positive = taker buy, negative = taker
        # sell). Distinct from `taker_flow` — this preserves individual
        # prints so we can compute a Shannon-style size-bucket entropy.
        self.trade_sizes = RollingSeries(120.0)
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
        # Higher-timeframe trend bias from the 1h kline poller. Score is
        # tanh((ema_fast - ema_slow) / atr) → [-1, +1]. 0 until the first
        # poll lands. `htf_trend_ts` is kept so stale scores can be
        # detected (poller down, last update > 1h ago = treat as 0).
        self.htf_trend_score: float = 0.0
        self.htf_trend_tf: str = ""
        self.htf_trend_ts: datetime | None = None
        # Phase G — Deribit options aggregate (polled every ~120s). Last-seen
        # scalars plus a rolling series of put/call ratio and OI-weighted IV
        # so tribes can read trend direction, not just latest value.
        self.last_pc_ratio: float | None = None
        self.last_net_delta_exposure_contracts: float | None = None
        self.last_oi_weighted_strike: float | None = None
        self.last_oi_weighted_iv: float | None = None
        self.last_total_call_oi: float | None = None
        self.last_total_put_oi: float | None = None
        self.last_options_spot_ref: float | None = None
        self.last_options_ts: datetime | None = None
        self.pc_ratio_series = RollingSeries(7200.0)         # 2h history
        self.iv_series = RollingSeries(7200.0)
        # Phase G — stablecoin supply series (USDT + USDC combined). Polled
        # every ~1800s, so 48h of history = 96 samples — comfortably enough
        # for % change queries over 2/6/24h windows.
        self.stablecoin_supply_series = RollingSeries(172800.0)  # 48h
        self.last_usdt_supply: float | None = None
        self.last_usdc_supply: float | None = None
        self.last_stablecoin_combined: float | None = None
        self.last_stablecoin_ts: datetime | None = None

    # -- derived signals ------------------------------------------------

    def realized_vol_bps(self, window_s: float = 840.0) -> float | None:
        """Total-window stddev of log-returns in bps over the last
        ``window_s`` of trade prices. Used by the projector as a realized-
        volatility rail against the predatory harvest boost — a forecast
        expected-move greater than 1.25× this value is clamped at entry.

        Default window is 14 minutes (14 × 60s), the convention for 14-period
        ATR on 1-min bars. Returns None when fewer than three usable samples
        fall in the window (cold start / long gap in trade flow).

        Scaling: per-tick log-return sigma × √N → cumulative-window sigma.
        Multiplied by 1e4 to express as basis points of price.
        """
        import math
        samples = list(self.prices._window(window_s))
        if len(samples) < 3:
            return None
        px = [v for _, v in samples if v is not None and v > 0]
        if len(px) < 3:
            return None
        rets: list[float] = []
        for i in range(1, len(px)):
            if px[i - 1] > 0 and px[i] > 0:
                rets.append(math.log(px[i] / px[i - 1]))
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sigma_per_tick = math.sqrt(var)
        return sigma_per_tick * math.sqrt(len(rets)) * 1e4

    # -- ingest ---------------------------------------------------------

    def ingest(self, ev: Event) -> None:
        self.last_update = ev.ts
        if ev.kind == "trade":
            p = ev.payload.get("price")
            n = ev.payload.get("notional_usd", 0.0)
            signed = -n if ev.payload.get("is_sell_aggressor") else n
            self.taker_flow.add(ev.ts, signed)
            # Phase 12 — keep raw signed notional per print so the silent
            # tribe can probe the trade-size distribution (iceberg / clustering).
            self.trade_sizes.add(ev.ts, signed)
            if p is not None:
                self.last_price = p
                self.prices.add(ev.ts, p)
                self._last_trade_ts = ev.ts.timestamp()
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
                # Refresh last_price from mark when the most recent aggTrade
                # is stale (>5s) or absent. Normal flow: aggTrade fires many
                # times per second on BTC, so last_price stays trade-driven;
                # during WS hiccups mark (1s cadence) takes over so the UI
                # keeps moving instead of flatlining.
                trade_stale = (
                    self._last_trade_ts is None
                    or ev.ts.timestamp() - self._last_trade_ts > 5.0
                )
                if self.last_price is None or trade_stale:
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
                # Phase 12 — feed rolling depth so the silent tribe can
                # measure queue-position decay (depth dropping fast = the
                # resting side is being burnt away → continuation).
                if self.bid_depth_5bps_usd:
                    self.bid_depth_series.add(ev.ts,
                                              float(self.bid_depth_5bps_usd))
                if self.ask_depth_5bps_usd:
                    self.ask_depth_series.add(ev.ts,
                                              float(self.ask_depth_5bps_usd))
        elif ev.kind == "oi":
            # Per-venue OI snapshot. Persist both the USD series (for
            # delta queries) and the last value (for "sum across venues").
            venue = str(ev.payload.get("venue") or ev.source.split(".", 1)[0])
            usd = float(ev.payload.get("oi_usd") or 0.0)
            if usd > 0:
                series = self.oi.setdefault(venue, RollingSeries(3600.0))
                series.add(ev.ts, usd)
                self.last_oi_usd[venue] = usd
        elif ev.kind == "htf_trend":
            sc = ev.payload.get("score")
            if isinstance(sc, (int, float)):
                self.htf_trend_score = max(-1.0, min(1.0, float(sc)))
                self.htf_trend_tf = str(ev.payload.get("timeframe") or "")
                self.htf_trend_ts = ev.ts
        elif ev.kind == "options_stats":
            # Deribit options aggregate — put/call ratio, net delta exposure,
            # OI-weighted strike + IV. Consumed by the gamma / pc_fader tribes.
            p = ev.payload
            pc = p.get("pc_ratio")
            if isinstance(pc, (int, float)) and pc >= 0:
                self.last_pc_ratio = float(pc)
                self.pc_ratio_series.add(ev.ts, float(pc))
            iv = p.get("oi_weighted_iv")
            if isinstance(iv, (int, float)) and iv > 0:
                self.last_oi_weighted_iv = float(iv)
                self.iv_series.add(ev.ts, float(iv))
            nde = p.get("net_delta_exposure_contracts")
            if isinstance(nde, (int, float)):
                self.last_net_delta_exposure_contracts = float(nde)
            ows = p.get("oi_weighted_strike")
            if isinstance(ows, (int, float)) and ows > 0:
                self.last_oi_weighted_strike = float(ows)
            tco = p.get("total_call_oi")
            if isinstance(tco, (int, float)):
                self.last_total_call_oi = float(tco)
            tpo = p.get("total_put_oi")
            if isinstance(tpo, (int, float)):
                self.last_total_put_oi = float(tpo)
            spot = p.get("spot_ref")
            if isinstance(spot, (int, float)) and spot > 0:
                self.last_options_spot_ref = float(spot)
            self.last_options_ts = ev.ts
        elif ev.kind == "stablecoin_supply":
            p = ev.payload
            usdt = p.get("usdt_supply")
            usdc = p.get("usdc_supply")
            combined = p.get("combined_supply")
            if isinstance(usdt, (int, float)) and usdt > 0:
                self.last_usdt_supply = float(usdt)
            if isinstance(usdc, (int, float)) and usdc > 0:
                self.last_usdc_supply = float(usdc)
            if isinstance(combined, (int, float)) and combined > 0:
                self.last_stablecoin_combined = float(combined)
                self.stablecoin_supply_series.add(ev.ts, float(combined))
            self.last_stablecoin_ts = ev.ts
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

    # -- options / stablecoin queries (Phase G) -------------------------

    def pc_ratio_change(self, window_s: float) -> float:
        """Δ in put/call ratio over `window_s` (absolute, not %). Positive
        = fear building, negative = fear easing. 0 when series empty."""
        fl = self.pc_ratio_series.first_last(window_s)
        if fl is None:
            return 0.0
        return fl[1] - fl[0]

    def iv_change_pct(self, window_s: float) -> float:
        """% change in OI-weighted IV over `window_s`. 0 when empty."""
        fl = self.iv_series.first_last(window_s)
        if fl is None or fl[0] <= 0:
            return 0.0
        return (fl[1] - fl[0]) / fl[0] * 100.0

    def stablecoin_supply_change_pct(self, window_s: float) -> float:
        """% change in combined USDT+USDC supply over `window_s`. Positive
        = net minting (bullish BTC proxy). 0 when series empty."""
        fl = self.stablecoin_supply_series.first_last(window_s)
        if fl is None or fl[0] <= 0:
            return 0.0
        return (fl[1] - fl[0]) / fl[0] * 100.0

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
        # Phase 12 — copy microstructure series so projector forks see them.
        s.bid_depth_series._data = deque(self.bid_depth_series._data)
        s.ask_depth_series._data = deque(self.ask_depth_series._data)
        s.trade_sizes._data = deque(self.trade_sizes._data)
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
        s.htf_trend_score = self.htf_trend_score
        s.htf_trend_tf = self.htf_trend_tf
        s.htf_trend_ts = self.htf_trend_ts
        # Phase G — options + stablecoin state.
        s.last_pc_ratio = self.last_pc_ratio
        s.last_net_delta_exposure_contracts = self.last_net_delta_exposure_contracts
        s.last_oi_weighted_strike = self.last_oi_weighted_strike
        s.last_oi_weighted_iv = self.last_oi_weighted_iv
        s.last_total_call_oi = self.last_total_call_oi
        s.last_total_put_oi = self.last_total_put_oi
        s.last_options_spot_ref = self.last_options_spot_ref
        s.last_options_ts = self.last_options_ts
        s.pc_ratio_series._data = deque(self.pc_ratio_series._data)
        s.iv_series._data = deque(self.iv_series._data)
        s.last_usdt_supply = self.last_usdt_supply
        s.last_usdc_supply = self.last_usdc_supply
        s.last_stablecoin_combined = self.last_stablecoin_combined
        s.last_stablecoin_ts = self.last_stablecoin_ts
        s.stablecoin_supply_series._data = deque(self.stablecoin_supply_series._data)
        return s

    def advance_clock(self, ts: datetime) -> None:
        """Public entry point — computes the epoch once and fans out to the
        ~15 rolling series below via their internal _trim_to_epoch path.
        Phase 2-lite: before this cache, 200-tick profiles showed ts.timestamp()
        called 3.6M times (~15 per (tribe × step) iteration inside the
        projector). Consolidating to one call per advance_clock plus the
        projector-level cache at the caller site brings this down to ~50/tick."""
        self._advance_clock_epoch(ts.timestamp(), ts)

    def _advance_clock_epoch(self, t: float, ts: datetime) -> None:
        """Hot-path entry. The projector precomputes a per-step epoch vector
        once per project() call and calls this directly, skipping the
        ts.timestamp() redo. `ts` is still threaded for `last_update`
        bookkeeping — other state reads rely on it being a datetime."""
        self.taker_flow._trim_to_epoch(t)
        self.long_liq._trim_to_epoch(t)
        self.short_liq._trim_to_epoch(t)
        self.prices._trim_to_epoch(t)
        self.social._trim_to_epoch(t)
        self.news._trim_to_epoch(t)
        # Phase 12 — keep silent-tribe series in sync with the virtual clock.
        self.bid_depth_series._trim_to_epoch(t)
        self.ask_depth_series._trim_to_epoch(t)
        self.trade_sizes._trim_to_epoch(t)
        for series in self.oi.values():
            series._trim_to_epoch(t)
        for series in self.cross_flow.values():
            series._trim_to_epoch(t)
        # Phase G — options + stablecoin rolling series.
        self.pc_ratio_series._trim_to_epoch(t)
        self.iv_series._trim_to_epoch(t)
        self.stablecoin_supply_series._trim_to_epoch(t)
        self.last_update = ts
