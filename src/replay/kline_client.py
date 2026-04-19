"""Cached historical fetcher for Binance USDT-M perps.

Pulls 1m klines and 8h funding from the free public endpoints and caches
one UTC day per JSONL file. Two replays of the same date hit the cache and
produce byte-identical inputs — a prerequisite for deterministic re-runs.

Endpoints (free, no auth):
  GET /fapi/v1/klines?symbol=BTCUSDT&interval=1m&startTime=&endTime=&limit=1500
  GET /fapi/v1/fundingRate?symbol=BTCUSDT&startTime=&endTime=&limit=1000

Cache layout:
  data/klines/BTCUSDT_1m_YYYY-MM-DD.jsonl    — 1440 bars/day
  data/funding/BTCUSDT_YYYY-MM-DD.jsonl       — 3 rows/day (8h cadence)

Bar dict schema (what downstream synthesis consumes):
  open_time_ms, close_time_ms, open, high, low, close,
  volume, quote_volume, taker_buy_volume, taker_buy_quote_volume, trade_count

Funding dict schema:
  funding_time_ms, funding_rate
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import orjson
from loguru import logger


USER_AGENT = "pam-council/0.2 (research bot)"
REQUEST_TIMEOUT_S = 30
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

KLINES_LIMIT = 1500        # Binance max per call
FUNDING_LIMIT = 1000
POLITE_GAP_S = 0.2         # between network calls — avoid spamming the endpoint
PARALLEL_DAY_FETCHES = 2   # semaphore for range fetches


def _day_range_ms(d: date) -> tuple[int, int]:
    """UTC day [00:00.000, next-day 00:00.000) as epoch ms."""
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _kline_cache_path(cache_dir: Path, symbol: str, d: date) -> Path:
    return cache_dir / f"{symbol}_1m_{d.isoformat()}.jsonl"


def _funding_cache_path(cache_dir: Path, symbol: str, d: date) -> Path:
    return cache_dir / f"{symbol}_{d.isoformat()}.jsonl"


def _bar_from_row(row: list) -> dict:
    """Convert Binance kline array → self-describing dict.
    Row order is fixed: [openTime, open, high, low, close, volume, closeTime,
    quoteVolume, trades, takerBuyBase, takerBuyQuote, _ignored]."""
    return {
        "open_time_ms": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time_ms": int(row[6]),
        "quote_volume": float(row[7]),
        "trade_count": int(row[8]),
        "taker_buy_volume": float(row[9]),
        "taker_buy_quote_volume": float(row[10]),
    }


def _funding_from_row(row: dict) -> dict:
    return {
        "funding_time_ms": int(row["fundingTime"]),
        "funding_rate": float(row["fundingRate"]),
    }


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """Atomic JSONL write via tmp-then-rename. Two concurrent writers can't
    leave a half-written file on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        for r in rows:
            f.write(orjson.dumps(r))
            f.write(b"\n")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    with path.open("rb") as f:
        return [orjson.loads(line) for line in f if line.strip()]


async def fetch_klines_page(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = KLINES_LIMIT,
) -> list[dict]:
    async with session.get(
        KLINES_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms - 1,
            "limit": limit,
        },
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    ) as r:
        r.raise_for_status()
        rows = await r.json()
    return [_bar_from_row(row) for row in rows]


async def fetch_funding_page(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
    limit: int = FUNDING_LIMIT,
) -> list[dict]:
    async with session.get(
        FUNDING_URL,
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms - 1,
            "limit": limit,
        },
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    ) as r:
        r.raise_for_status()
        rows = await r.json()
    return [_funding_from_row(row) for row in rows]


@dataclass
class CachedHistoricalClient:
    cache_root: Path
    symbol: str = "BTCUSDT"
    user_agent: str = USER_AGENT

    def __post_init__(self) -> None:
        self._kline_dir = Path(self.cache_root) / "klines"
        self._funding_dir = Path(self.cache_root) / "funding"
        self._kline_dir.mkdir(parents=True, exist_ok=True)
        self._funding_dir.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(PARALLEL_DAY_FETCHES)
        self._net_lock = asyncio.Lock()  # serializes the POLITE_GAP_S sleep

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )

    def _day_is_complete(self, path: Path, expected_count: int) -> bool:
        """A cached day is trusted only if the row count matches. Partial
        files (e.g. fetched while the day was still in progress) are rewritten."""
        if not path.exists():
            return False
        try:
            rows = _read_jsonl(path)
        except (OSError, orjson.JSONDecodeError):
            return False
        return len(rows) == expected_count

    async def get_day_klines(self, d: date) -> list[dict]:
        path = _kline_cache_path(self._kline_dir, self.symbol, d)
        if self._day_is_complete(path, expected_count=1440):
            return _read_jsonl(path)
        start_ms, end_ms = _day_range_ms(d)
        async with self._session() as s:
            async with self._net_lock:
                bars = await fetch_klines_page(s, self.symbol, "1m", start_ms, end_ms)
                await asyncio.sleep(POLITE_GAP_S)
        if len(bars) != 1440:
            logger.warning(
                "[replay] {} kline fetch returned {} bars (expected 1440) — not caching partial",
                d.isoformat(), len(bars),
            )
            return bars
        _write_jsonl_atomic(path, bars)
        logger.info("[replay] cached {} 1m klines for {}", len(bars), d.isoformat())
        return bars

    async def get_day_funding(self, d: date) -> list[dict]:
        path = _funding_cache_path(self._funding_dir, self.symbol, d)
        if self._day_is_complete(path, expected_count=3):
            return _read_jsonl(path)
        start_ms, end_ms = _day_range_ms(d)
        async with self._session() as s:
            async with self._net_lock:
                rows = await fetch_funding_page(s, self.symbol, start_ms, end_ms)
                await asyncio.sleep(POLITE_GAP_S)
        if len(rows) != 3:
            logger.warning(
                "[replay] {} funding fetch returned {} rows (expected 3) — not caching partial",
                d.isoformat(), len(rows),
            )
            return rows
        _write_jsonl_atomic(path, rows)
        logger.info("[replay] cached {} funding events for {}", len(rows), d.isoformat())
        return rows

    async def _fetch_with_sem(self, coro):
        async with self._sem:
            return await coro

    async def get_range_klines(self, start: date, end_inclusive: date) -> list[dict]:
        """Fetches every UTC day in [start, end_inclusive]. Returns a single
        time-ordered list. Day-parallel but rate-limited via internal semaphore."""
        days: list[date] = []
        d = start
        while d <= end_inclusive:
            days.append(d)
            d += timedelta(days=1)
        tasks = [self._fetch_with_sem(self.get_day_klines(day)) for day in days]
        per_day = await asyncio.gather(*tasks)
        merged: list[dict] = []
        for bars in per_day:
            merged.extend(bars)
        merged.sort(key=lambda b: b["open_time_ms"])
        return merged

    async def get_range_funding(self, start: date, end_inclusive: date) -> list[dict]:
        days: list[date] = []
        d = start
        while d <= end_inclusive:
            days.append(d)
            d += timedelta(days=1)
        tasks = [self._fetch_with_sem(self.get_day_funding(day)) for day in days]
        per_day = await asyncio.gather(*tasks)
        merged: list[dict] = []
        for rows in per_day:
            merged.extend(rows)
        merged.sort(key=lambda r: r["funding_time_ms"])
        return merged
