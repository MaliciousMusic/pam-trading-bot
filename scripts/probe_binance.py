"""Quick Binance-WS connectivity probe.

Connects to the live perps WS for N seconds, counts events by kind,
samples one book payload, and exits. Proves (or disproves) that the
depth20 stream reaches this host before we burn 5 min on a full
pulse test.

Two modes:
  - default: subscribe via BinancePerpsWS and count normalized Events.
  - --raw:   bypass the normalizer and count raw stream frames by
             stream-name prefix. Useful to diagnose "normalizer silent"
             vs "WS silent".

Usage:
    python -m scripts.probe_binance                  # 20s, normalized
    python -m scripts.probe_binance --seconds 30
    python -m scripts.probe_binance --raw --seconds 15
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

import websockets
from loguru import logger

from src.core import serde
from src.core.bus import EventBus
from src.core.config import data_sources
from src.ingress.binance_ws import BinancePerpsWS, BASE_URL


async def run(duration_s: float) -> int:
    cfg = data_sources()["sources"]["binance_perps_ws"]
    if not cfg.get("enabled"):
        print("[probe] binance_perps_ws disabled in config")
        return 2

    bus = EventBus()
    client = BinancePerpsWS(bus, streams=cfg["streams"])
    q = bus.subscribe()

    counts: Counter[str] = Counter()
    book_sample: dict | None = None

    async def drain() -> None:
        nonlocal book_sample
        async for ev in EventBus.drain(q):
            counts[ev.kind] += 1
            if ev.kind == "book" and book_sample is None:
                book_sample = dict(ev.payload)

    ws_task = asyncio.create_task(client.run())
    drain_task = asyncio.create_task(drain())
    try:
        await asyncio.sleep(duration_s)
    finally:
        ws_task.cancel()
        drain_task.cancel()
        for t in (ws_task, drain_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print("---")
    print(f"[probe] {duration_s:.0f}s summary (streams={cfg['streams']}):")
    if not counts:
        print("  NO EVENTS RECEIVED -- connection failed or geo-blocked")
        return 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<14} {v:>6}")
    if book_sample:
        # Print only the scalar fields; raw bids/asks arrays are huge.
        slim = {k: v for k, v in book_sample.items() if not isinstance(v, list)}
        print(f"[probe] sample book payload: {slim}")
    verdict = "PASS" if counts.get("book", 0) > 0 else "NO_BOOK_EVENTS"
    print(f"[probe] verdict: {verdict}")
    return 0 if verdict == "PASS" else 1


async def run_raw(duration_s: float) -> int:
    cfg = data_sources()["sources"]["binance_perps_ws"]
    streams = cfg["streams"]
    url = f"{BASE_URL}?streams={'/'.join(streams)}"
    print(f"[probe-raw] connecting to {url}")

    counts: Counter[str] = Counter()
    depth_sample: dict | None = None
    first_frames: list[dict] = []

    async def consume(ws) -> None:
        nonlocal depth_sample
        async for raw in ws:
            try:
                msg = serde.loads(raw)
            except Exception:
                counts["_bad_json"] += 1
                continue
            stream_name = msg.get("stream", "<no-stream>")
            # Bucket by the @key suffix so depth20/aggTrade/forceOrder/markPrice
            # each count into their own bin.
            key = stream_name.split("@", 1)[-1] if "@" in stream_name else stream_name
            counts[key] += 1
            if "depth" in key and depth_sample is None:
                data = msg.get("data") or msg
                depth_sample = {
                    "stream": stream_name,
                    "top_bid": (data.get("bids") or [[None]])[0],
                    "top_ask": (data.get("asks") or [[None]])[0],
                    "has_e_field": "e" in data,
                    "data_keys": sorted(list(data.keys()))[:10],
                }
            if len(first_frames) < 3:
                first_frames.append({"stream": stream_name, "sample": str(raw)[:200]})

    try:
        async with websockets.connect(
            url, ping_interval=30, ping_timeout=10, close_timeout=5, max_size=2**20,
        ) as ws:
            try:
                await asyncio.wait_for(consume(ws), timeout=duration_s)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        print(f"[probe-raw] WS error: {e}")
        return 1

    print("---")
    print(f"[probe-raw] {duration_s:.0f}s frame counts by stream suffix:")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v:>6}")
    print(f"[probe-raw] first 3 frames (trimmed):")
    for i, f in enumerate(first_frames):
        print(f"  [{i}] {f['stream']}: {f['sample']}")
    if depth_sample:
        print(f"[probe-raw] depth sample: {depth_sample}")
    else:
        print("[probe-raw] NO depth frames received -- stream silent from Binance side")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--raw", action="store_true",
                    help="Bypass the normalizer -- count raw stream frames")
    args = ap.parse_args()

    # Silence the noisy INFO stream so the summary is easy to read.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    try:
        if args.raw:
            return asyncio.run(run_raw(args.seconds))
        return asyncio.run(run(args.seconds))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
