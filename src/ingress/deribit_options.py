"""Deribit BTC options poller — free public API, no auth.

Polls `get_book_summary_by_currency?currency=BTC&kind=option` and
aggregates the ~5000 instruments into a handful of tape-ready scalars:

    - total_call_oi / total_put_oi  (contracts, 1 contract = $10 for BTC opts)
    - pc_ratio                       (put OI / call OI — crowd fear proxy)
    - net_delta_exposure_contracts   (call_oi − put_oi, sign-convention:
                                      positive = net dealer short calls →
                                      short gamma → amplifying. Negative =
                                      net dealer long puts → long gamma →
                                      damping.)
    - oi_weighted_strike              (gamma-pin proxy: spot gravitates
                                      toward the OI-weighted mean strike
                                      when dealers are long gamma near
                                      expiry)
    - oi_weighted_iv                  (mean mark IV across the book,
                                      weighted by OI — tighter than the
                                      DVOL index)

Event shape:
    kind="options_stats"
    source="deribit.options"
    payload={venue, currency, total_call_oi, total_put_oi, pc_ratio,
             net_delta_exposure_contracts, oi_weighted_strike,
             oi_weighted_iv, spot_ref, instrument_count}
    severity=0.0  (state variable, not a shock)

Poll cadence: 120s. The response is large (5-10kB gzipped, ~5000 rows)
and options OI moves slowly — per-minute polling wastes bandwidth. At
120s we get ~30 samples/hour, enough for a 15-min rolling series to
catch meaningful shifts.
"""
from __future__ import annotations

import asyncio

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


USER_AGENT = "pam-council/0.2 (research bot)"
REQUEST_TIMEOUT_S = 20
API_BASE = "https://www.deribit.com/api/v2"


class DeribitOptionsPoller:
    """Polls Deribit option book summaries and emits one aggregated event
    per poll. Instrument names encode expiry + strike + put/call — we
    parse the last character ('C' or 'P') to bucket.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        currency: str = "BTC",
        poll_seconds: int = 120,
        retry_seconds: int = 60,
    ) -> None:
        self.bus = bus
        self.currency = currency
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as s:
            while True:
                try:
                    await self._poll(s)
                    await asyncio.sleep(self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[deribit_opts] poll failed: {}", e)
                    await asyncio.sleep(self.retry_seconds)

    async def _poll(self, s: aiohttp.ClientSession) -> None:
        try:
            async with s.get(
                f"{API_BASE}/public/get_book_summary_by_currency",
                params={"currency": self.currency, "kind": "option"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as r:
                r.raise_for_status()
                payload = await r.json()
        except Exception as e:
            logger.warning("[deribit_opts] fetch failed: {}", e)
            return

        rows = payload.get("result") or []
        if not rows:
            logger.warning("[deribit_opts] empty result")
            return

        total_call_oi = 0.0
        total_put_oi = 0.0
        weighted_strike_sum = 0.0   # Σ strike × oi
        weighted_iv_sum = 0.0       # Σ iv × oi
        oi_denom = 0.0
        spot_ref_samples: list[float] = []

        for row in rows:
            name = row.get("instrument_name") or ""
            # Format: "BTC-26JUL24-65000-C" — fields[2]=strike, fields[3]=C|P
            parts = name.split("-")
            if len(parts) < 4:
                continue
            try:
                strike = float(parts[2])
            except (TypeError, ValueError):
                continue
            put_call = parts[3].upper()
            oi = float(row.get("open_interest") or 0.0)
            if oi <= 0:
                continue
            if put_call == "C":
                total_call_oi += oi
            elif put_call == "P":
                total_put_oi += oi
            else:
                continue
            iv = float(row.get("mark_iv") or 0.0)
            weighted_strike_sum += strike * oi
            weighted_iv_sum += iv * oi
            oi_denom += oi
            # Underlying index price — all option instruments carry the
            # same `underlying_price`; sampling a few is enough.
            up = row.get("underlying_price")
            if up and len(spot_ref_samples) < 8:
                spot_ref_samples.append(float(up))

        if oi_denom <= 0:
            logger.warning("[deribit_opts] zero OI across {} rows", len(rows))
            return

        oi_weighted_strike = weighted_strike_sum / oi_denom
        oi_weighted_iv = weighted_iv_sum / oi_denom
        pc_ratio = total_put_oi / max(total_call_oi, 1e-9)
        net_delta_exposure = total_call_oi - total_put_oi
        spot_ref = (sum(spot_ref_samples) / len(spot_ref_samples)) if spot_ref_samples else 0.0

        ev = Event(
            ts=Event.now_utc(),
            source="deribit.options",
            kind="options_stats",
            symbol=f"{self.currency}USD",
            payload={
                "venue": "deribit",
                "currency": self.currency,
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "pc_ratio": pc_ratio,
                "net_delta_exposure_contracts": net_delta_exposure,
                "oi_weighted_strike": oi_weighted_strike,
                "oi_weighted_iv": oi_weighted_iv,
                "spot_ref": spot_ref,
                "instrument_count": len(rows),
            },
            tags={"options", "deribit"},
            severity=0.0,
        )
        await self.bus.publish(ev)
