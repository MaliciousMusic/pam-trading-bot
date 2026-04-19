"""Phase 1 verification for the replay harness.

Runs offline (no network) against the kline cache layer and the
bar→event synthesis layer. Exit 0 if every invariant holds.

Usage:  python -m scripts.test_replay_phase1
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.replay.bar_to_events import (
    bar_to_trade_events,
    funding_to_event,
)
from src.replay.kline_client import (
    _bar_from_row,
    _funding_from_row,
    _read_jsonl,
    _write_jsonl_atomic,
)


def _fixture_bar(
    open_time_ms: int = 1_700_000_000_000,
    o: float = 30000.0,
    h: float = 30120.0,
    l: float = 29960.0,
    c: float = 30080.0,
    volume: float = 50.0,
    taker_buy_volume: float = 32.0,
) -> dict:
    quote_volume = (o + h + l + c) / 4 * volume
    taker_buy_quote = (o + h + l + c) / 4 * taker_buy_volume
    return {
        "open_time_ms": open_time_ms,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": volume,
        "close_time_ms": open_time_ms + 59_999,
        "quote_volume": quote_volume,
        "trade_count": 100,
        "taker_buy_volume": taker_buy_volume,
        "taker_buy_quote_volume": taker_buy_quote,
    }


def test_cache_roundtrip_bytes_stable() -> None:
    """Writing then reading then rewriting produces identical bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roundtrip.jsonl"
        original = [_fixture_bar(open_time_ms=1_700_000_000_000 + i * 60_000)
                    for i in range(10)]
        _write_jsonl_atomic(path, original)
        first_bytes = path.read_bytes()

        rows = _read_jsonl(path)
        assert rows == original, "read-back did not match input"

        _write_jsonl_atomic(path, rows)
        second_bytes = path.read_bytes()
        assert first_bytes == second_bytes, "cache round-trip not byte-stable"
    print("  ok  cache round-trip byte-stable")


def test_bar_from_row_schema() -> None:
    """The Binance array format maps to our dict format losslessly."""
    row = [
        1_700_000_000_000, "30000.0", "30120.0", "29960.0", "30080.0",
        "50.0", 1_700_000_059_999, "1503000.0", 100,
        "32.0", "963200.0", "0",
    ]
    bar = _bar_from_row(row)
    assert bar["open"] == 30000.0
    assert bar["high"] == 30120.0
    assert bar["low"] == 29960.0
    assert bar["close"] == 30080.0
    assert bar["volume"] == 50.0
    assert bar["taker_buy_volume"] == 32.0
    assert bar["taker_buy_quote_volume"] == 963200.0
    assert bar["trade_count"] == 100
    assert bar["open_time_ms"] == 1_700_000_000_000
    assert bar["close_time_ms"] == 1_700_000_059_999
    print("  ok  _bar_from_row parses Binance array shape")


def test_funding_from_row() -> None:
    row = {"symbol": "BTCUSDT", "fundingTime": 1_700_000_000_000, "fundingRate": "0.0001"}
    d = _funding_from_row(row)
    assert d == {"funding_time_ms": 1_700_000_000_000, "funding_rate": 0.0001}
    print("  ok  _funding_from_row parses funding response")


def _assert_events_within_bar(bar: dict, events: list, synth_rate: int) -> None:
    assert len(events) == synth_rate, f"expected {synth_rate} events, got {len(events)}"
    open_ts = datetime.fromtimestamp(bar["open_time_ms"] / 1000, tz=timezone.utc)
    close_ts = datetime.fromtimestamp(bar["close_time_ms"] / 1000, tz=timezone.utc)
    lo, hi = bar["low"], bar["high"]
    for ev in events:
        assert ev.kind == "trade"
        assert ev.source == "replay.aggTrade"
        assert open_ts <= ev.ts <= close_ts, f"ts {ev.ts} outside bar"
        p = ev.payload["price"]
        assert lo - 1e-6 <= p <= hi + 1e-6, f"price {p} outside [{lo}, {hi}]"
        assert (ev.payload["is_sell_aggressor"] is True) == ("taker_sell" in ev.tags)


def test_synth_conservation_each_rate() -> None:
    """Cumulative synthesized notional matches the bar's totals within
    rounding tolerance for every supported synth_rate."""
    bar = _fixture_bar()
    target_total = bar["quote_volume"]
    target_buy = bar["taker_buy_quote_volume"]

    for synth_rate in (6, 60, 600):
        events = bar_to_trade_events(bar, synth_rate)
        _assert_events_within_bar(bar, events, synth_rate)
        total = sum(e.payload["notional_usd"] for e in events)
        buy = sum(e.payload["notional_usd"] for e in events
                  if not e.payload["is_sell_aggressor"])
        # Tolerance scales with 1/n because of buy-count rounding.
        rel = max(0.002, 2.0 / synth_rate)
        assert abs(total - target_total) / target_total < rel, \
            f"synth_rate={synth_rate}: total {total} vs target {target_total}"
        assert abs(buy - target_buy) / target_buy < rel, \
            f"synth_rate={synth_rate}: buy {buy} vs target {target_buy}"
        print(f"  ok  synth_rate={synth_rate} preserves flow within {rel:.3%}")


def test_zero_volume_bar() -> None:
    bar = _fixture_bar(volume=0.0, taker_buy_volume=0.0)
    events = bar_to_trade_events(bar, synth_rate=60)
    assert len(events) == 1, "zero-volume bar should emit exactly one close-price event"
    ev = events[0]
    assert ev.payload["price"] == bar["close"]
    assert ev.payload["qty"] == 0.0
    assert ev.payload["notional_usd"] == 0.0
    assert ev.severity == 0.0
    print("  ok  zero-volume bar emits one advance-price event")


def test_direction_heuristic() -> None:
    """Up-bar visits low before high; down-bar visits high before low."""
    up = _fixture_bar(o=30000, h=30120, l=29960, c=30080)
    events = bar_to_trade_events(up, synth_rate=60)
    prices = [e.payload["price"] for e in events]
    first_touch_low = min(range(len(prices)), key=lambda i: prices[i])
    first_touch_high = min(range(len(prices)), key=lambda i: -prices[i])
    assert first_touch_low < first_touch_high, "up-bar should touch low first"

    down = _fixture_bar(o=30100, h=30150, l=29900, c=29950)
    events = bar_to_trade_events(down, synth_rate=60)
    prices = [e.payload["price"] for e in events]
    first_touch_low = min(range(len(prices)), key=lambda i: prices[i])
    first_touch_high = min(range(len(prices)), key=lambda i: -prices[i])
    assert first_touch_high < first_touch_low, "down-bar should touch high first"
    print("  ok  direction heuristic: up=L-first, down=H-first")


def test_funding_event_shapes() -> None:
    row = {"funding_time_ms": 1_700_000_000_000, "funding_rate": 0.0001}
    ev = funding_to_event(row, mark_price_hint=30050.0)
    assert ev.kind == "funding"
    assert ev.source == "replay.fundingRate"
    assert ev.payload["funding_rate"] == 0.0001
    assert ev.payload["mark_price"] == 30050.0
    assert "funding" in ev.tags and "mark_price" in ev.tags

    ev_none = funding_to_event(row, mark_price_hint=None)
    assert ev_none.payload["mark_price"] == 0.0, \
        "no-hint case must leave mark_price falsy so ingest skips the update"
    print("  ok  funding_to_event shapes correct (with + without mark hint)")


def test_determinism_same_input_same_events() -> None:
    """The synthesis is pure: two runs on the same bar → identical events."""
    bar = _fixture_bar()
    a = bar_to_trade_events(bar, synth_rate=60)
    b = bar_to_trade_events(bar, synth_rate=60)
    assert len(a) == len(b)
    for ea, eb in zip(a, b):
        assert ea.ts == eb.ts
        assert ea.payload == eb.payload
        assert ea.tags == eb.tags
        assert ea.severity == eb.severity
    print("  ok  synthesis is deterministic")


def main() -> int:
    tests = [
        test_cache_roundtrip_bytes_stable,
        test_bar_from_row_schema,
        test_funding_from_row,
        test_synth_conservation_each_rate,
        test_zero_volume_bar,
        test_direction_heuristic,
        test_funding_event_shapes,
        test_determinism_same_input_same_events,
    ]
    print(f"running {len(tests)} phase-1 tests...")
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 1
    print("all phase-1 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
