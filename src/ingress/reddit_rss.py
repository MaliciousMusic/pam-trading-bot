"""Reddit public RSS poller — cheap social-pulse signal for crypto subs.

Scrapes `/r/<sub>/new/.rss` (free, no auth, just requires a polite
User-Agent). For each unseen post we compute a crude keyword sentiment
score in [-1, +1] and emit a `kind="social_pulse"` Event.

This is NOT a sentiment model. It's a cheap, transparent proxy good
enough to wake up the `news_fader` tribe. Upgradability: same API shape
will work when we plug in a real model later.
"""
from __future__ import annotations

import asyncio
import re
from xml.etree import ElementTree as ET
from datetime import datetime, timezone

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event


SUBREDDITS = ["Bitcoin", "CryptoCurrency", "BitcoinMarkets"]
POLL_SECONDS = 180       # 3 min, plenty gentle for public RSS
RETRY_SECONDS = 60
MAX_SEEN = 5000

# Cheap lexicon. Ordered by directional conviction (strong first).
POS = {
    "moon": 1.0, "pump": 0.8, "rally": 0.7, "bullish": 0.9, "breakout": 0.7,
    "ath": 0.8, "parabolic": 1.0, "squeeze": 0.6, "surge": 0.7, "buy": 0.3,
    "long": 0.3, "lfg": 0.6, "green": 0.3, "sending": 0.5, "up only": 0.8,
}
NEG = {
    "crash": 1.0, "dump": 0.8, "capitulation": 1.0, "bearish": 0.9,
    "liquidated": 0.7, "rekt": 0.8, "correction": 0.5, "sell": 0.3,
    "short": 0.3, "red": 0.3, "tanking": 0.8, "bloodbath": 1.0, "panic": 0.9,
    "dead": 0.4, "bottom": 0.5,
}

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _score_title(title: str) -> float:
    """Return a signed score for one title. Magnitude capped at 1."""
    t = title.lower()
    pos = sum(w for kw, w in POS.items() if re.search(rf"\b{re.escape(kw)}\b", t))
    neg = sum(w for kw, w in NEG.items() if re.search(rf"\b{re.escape(kw)}\b", t))
    total = pos + neg
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / max(1.0, total)))


class RedditRSSPoller:
    def __init__(self, bus: EventBus, subs: list[str] | None = None) -> None:
        self.bus = bus
        self.subs = subs or SUBREDDITS
        self._seen: set[str] = set()

    async def run(self) -> None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "pam-council/0.1 (research bot; contact: local)"}
        ) as s:
            # First loop — seed seen IDs without emitting, so we don't
            # flood the bus with a 25-post burst on startup.
            await self._seed(s)
            while True:
                try:
                    for sub in self.subs:
                        await self._poll_sub(s, sub, emit=True)
                        await asyncio.sleep(2)   # space out requests
                    await asyncio.sleep(POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("reddit poll failed: {}", e)
                    await asyncio.sleep(RETRY_SECONDS)

    async def _seed(self, s: aiohttp.ClientSession) -> None:
        for sub in self.subs:
            try:
                await self._poll_sub(s, sub, emit=False)
            except Exception as e:
                logger.warning("reddit seed {} failed: {}", sub, e)

    async def _poll_sub(self, s: aiohttp.ClientSession, sub: str, *, emit: bool) -> None:
        url = f"https://www.reddit.com/r/{sub}/new/.rss"
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            body = await r.text()
        root = ET.fromstring(body)
        for entry in root.iter(ATOM_NS + "entry"):
            eid = (entry.findtext(ATOM_NS + "id") or "").strip()
            title = (entry.findtext(ATOM_NS + "title") or "").strip()
            if not eid or not title or eid in self._seen:
                continue
            self._seen.add(eid)
            if len(self._seen) > MAX_SEEN:
                # drop oldest arbitrary — set is unordered so just rebuild keeping recent
                self._seen = set(list(self._seen)[-MAX_SEEN // 2 :])
            if not emit:
                continue
            score = _score_title(title)
            if score == 0.0:
                continue        # unscored posts are noise, don't emit
            side = "social_bull" if score > 0 else "social_bear"
            ev = Event(
                ts=Event.now_utc(),
                source=f"reddit.{sub}",
                kind="social_pulse",
                symbol=None,
                payload={"title": title[:200], "score": score, "subreddit": sub},
                tags={"social", side},
                severity=min(1.0, abs(score)),
            )
            await self.bus.publish(ev)
