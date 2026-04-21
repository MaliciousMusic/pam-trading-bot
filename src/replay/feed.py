"""Build a chronological Event stream from cached klines + funding.

For sprint mode (headless batch), the stream is consumed synchronously by
the engine driver. For watch mode (phase 4+), the same stream is paced
onto the live EventBus. Either way the synthesis layer is identical —
only the cadence differs.

Bars are converted to `bar_to_trade_events()` batches; funding-rate rows
become `funding_to_event()` events interleaved at their true timestamp.
Each funding event carries `mark_price_hint` = the most recent trade
price, so `MarketState.last_mark_price` updates coherently on ingest.

Phase G3 — `synth_book` opt-in. When True, `bar_to_book_events()` emits
proxy `kind="book"` events interleaved with the trade stream so replay
exercises microstructure_prober and the depth-aware projector. Default
is False to preserve existing byte-identical replay baselines.
"""
from __future__ import annotations

from typing import Iterable, Iterator

from ..core.types import Event
from .bar_to_events import bar_to_book_events, bar_to_trade_events, funding_to_event


def build_event_stream(
    bars: Iterable[dict],
    funding: Iterable[dict],
    *,
    synth_rate: int,
    symbol: str = "BTCUSDT",
    synth_book: bool = False,
) -> Iterator[Event]:
    """Yield synthesized events in chronological order.

    Funding events are merged so each fires at the timestamp of the first
    trade ≥ its `funding_time_ms`. `mark_price_hint` is the most recent
    trade price (None until the first trade fires, which only happens if
    funding predates the bar range — rare; ingest handles None by leaving
    last_mark_price untouched).

    When `synth_book=True`, proxy depth events from `bar_to_book_events()`
    are merged into the timeline as well. They share the bar's open_time
    span with trades; merging is chronological via timestamp.
    """
    funding_list = sorted(funding, key=lambda r: r["funding_time_ms"])
    f_idx = 0
    last_price: float | None = None

    for bar in bars:
        trades = bar_to_trade_events(bar, synth_rate, symbol=symbol)
        books = bar_to_book_events(bar, symbol=symbol) if synth_book else []
        # Merge trades and books in chronological order within the bar.
        # Both are already time-sorted individually. For ties (same ms),
        # emit the book event first so MarketState has depth before the
        # trade at that instant — that's the physical ordering on a live
        # WS (depth snapshot updates between trade prints).
        combined = sorted(
            [(ev.ts, 0, ev) for ev in books] + [(ev.ts, 1, ev) for ev in trades],
            key=lambda x: (x[0], x[1]),
        )
        for _, _, ev in combined:
            ev_ms = int(ev.ts.timestamp() * 1000)
            while (f_idx < len(funding_list)
                   and funding_list[f_idx]["funding_time_ms"] <= ev_ms):
                yield funding_to_event(
                    funding_list[f_idx],
                    symbol=symbol,
                    mark_price_hint=last_price,
                )
                f_idx += 1
            yield ev
            if ev.kind == "trade":
                last_price = ev.payload["price"]

    while f_idx < len(funding_list):
        yield funding_to_event(
            funding_list[f_idx],
            symbol=symbol,
            mark_price_hint=last_price,
        )
        f_idx += 1
