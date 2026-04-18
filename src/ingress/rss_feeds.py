"""Generic RSS/Atom feed poller — curated free OSINT stream.

One poller, many feeds. Each feed is declared in `config/data_sources.yaml`
with a category that determines the downstream event shape:

    kind:
      - "news"          → generic bitcoin-relevant news (Coindesk, Cointelegraph, …)
      - "regulation"    → SEC/CFTC/Fed filings & press releases
      - "business"      → Reuters, WSJ, FT crypto feeds (when accessible)
      - "influencer"    → Substack/Nitter mirrors of big talkers
      - "macro"         → macro calendar / central-bank announcements

Events land on the bus as `kind="news"` with the sub-category carried in
`ev.tags` — so MarketState can route scores per tag (regulation_pulse,
influencer_pulse, …) and strategies can filter on what they care about.

Design constraints:
- **Free only.** No API keys. No paywalled feeds. Respect `robots.txt`.
  Explicit polite User-Agent. Minimum 30s between identical feed hits.
- **Never crash the bot.** Any single feed can go stale, 404, or return
  malformed XML — that's logged at warning level and the poll continues.
- **Dedupe aggressively.** RSS re-emits the same item for days; seen-set
  is keyed by (feed_id, entry_id|link|title_hash) and trimmed at MAX_SEEN.
- **Score is a floor, not a ceiling.** The keyword lexicon is crude on
  purpose; a real sentiment model can be plugged in later via the same
  event shape without ingester changes.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import aiohttp
from loguru import logger

from ..core.bus import EventBus
from ..core.types import Event
from .event_classifier import classify as classify_event


USER_AGENT = "pam-council/0.2 (research bot; https://github.com/MaliciousMusic/pam-trading-bot)"
MAX_SEEN_PER_FEED = 1500
REQUEST_TIMEOUT_S = 20
PER_FEED_GAP_S = 2.0        # pause between feeds in one sweep to stay polite

# ── Bitcoin-relevant keyword lexicon ──────────────────────────────────────
# Kept intentionally small. Each keyword has a signed magnitude in [-1, +1].
# Scoring a title == average of matched keyword weights, clipped. The lexicon
# focuses on market-moving terms, not everyday chatter.

POS = {
    # market structure
    "breakout": 0.7, "rally": 0.7, "surge": 0.7, "soar": 0.8, "ath": 0.8,
    "record high": 0.9, "bull run": 0.9, "parabolic": 1.0,
    # flows & adoption
    "etf approval": 1.0, "etf inflow": 0.8, "spot etf": 0.6,
    "accumulation": 0.5, "whale buying": 0.6, "inflows": 0.4,
    # policy / regulatory tailwind
    "approved": 0.6, "green light": 0.7, "pro-crypto": 0.7, "deregulation": 0.6,
    "rate cut": 0.5, "dovish": 0.5,
    # corporate / sovereign adoption
    "strategic reserve": 0.8, "treasury buy": 0.7, "corporate buy": 0.6,
    "adoption": 0.3, "institutional": 0.3, "legal tender": 0.7,
}

NEG = {
    # market structure
    "crash": 1.0, "plunge": 0.9, "slump": 0.6, "selloff": 0.7, "capitulation": 1.0,
    "correction": 0.5, "breakdown": 0.6, "rout": 0.9, "bloodbath": 1.0,
    # flows
    "outflow": 0.5, "whale selling": 0.6, "liquidations": 0.6, "liquidated": 0.5,
    # regulatory headwind
    "rejected": 0.7, "banned": 1.0, "ban": 0.8, "lawsuit": 0.7, "sued": 0.6,
    "indictment": 0.9, "charges": 0.6, "subpoena": 0.5, "enforcement action": 0.7,
    "crackdown": 0.8, "hack": 0.9, "exploit": 0.8, "stolen": 0.6, "fraud": 0.8,
    # policy / macro
    "rate hike": 0.5, "hawkish": 0.5, "recession": 0.5, "tightening": 0.4,
    # exchange/solvency
    "bankruptcy": 0.9, "insolvent": 0.9, "delisted": 0.7, "halted": 0.6,
}

BITCOIN_RELEVANCE = re.compile(
    r"\b(bitcoin|btc|cryptocurren|crypto market|digital asset|"
    r"coinbase|binance|kraken|microstrategy|blackrock.*bitcoin|"
    r"sec|cftc|federal reserve|fomc|rate|etf|stablecoin|"
    r"spot bitcoin|hodl|hashrate|mining|miners)\b",
    re.IGNORECASE,
)

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _score_title(title: str) -> float:
    """Signed [-1,+1] score from keyword matches. 0 if no hits."""
    t = title.lower()
    pos = sum(w for kw, w in POS.items() if kw in t)
    neg = sum(w for kw, w in NEG.items() if kw in t)
    total = pos + neg
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / max(1.0, total)))


def _is_bitcoin_relevant(title: str, summary: str = "") -> bool:
    """Filter out noise feeds can contain (e.g. coindesk covers many coins)."""
    return bool(BITCOIN_RELEVANCE.search(title) or BITCOIN_RELEVANCE.search(summary))


@dataclass
class FeedSpec:
    id: str                             # stable identifier — used in event.source
    url: str
    category: str                       # news | regulation | business | influencer | macro
    weight: float = 1.0                 # severity multiplier — SEC=1.0, random blog=0.3
    bitcoin_filter: bool = True         # skip entries that don't mention BTC
    tags: list[str] = field(default_factory=list)


def _hash_entry(feed_id: str, eid: str, title: str) -> str:
    """Stable dedupe key — prefer the feed-provided id, fall back to title hash."""
    base = eid or title
    return f"{feed_id}:{hashlib.sha1(base.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


def _extract_entries(root: ET.Element) -> list[tuple[str, str, str]]:
    """Return (id, title, summary) tuples. Accepts Atom and RSS 2.0."""
    out: list[tuple[str, str, str]] = []
    # Atom
    for entry in root.iter(ATOM_NS + "entry"):
        eid = (entry.findtext(ATOM_NS + "id") or "").strip()
        title = (entry.findtext(ATOM_NS + "title") or "").strip()
        summary = (entry.findtext(ATOM_NS + "summary") or "").strip()
        if title:
            out.append((eid, title, summary))
    if out:
        return out
    # RSS 2.0
    for entry in root.iter("item"):
        eid = (entry.findtext("guid") or entry.findtext("link") or "").strip()
        title = (entry.findtext("title") or "").strip()
        summary = (entry.findtext("description") or "").strip()
        if title:
            out.append((eid, title, summary))
    return out


class RSSFeedPoller:
    """Polls a list of FeedSpecs, emits normalized news events."""

    def __init__(
        self,
        bus: EventBus,
        feeds: list[FeedSpec],
        poll_seconds: int = 180,
        retry_seconds: int = 120,
    ) -> None:
        self.bus = bus
        self.feeds = feeds
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self._seen: dict[str, set[str]] = {f.id: set() for f in feeds}

    async def run(self) -> None:
        if not self.feeds:
            logger.info("[rss] no feeds configured; poller idle")
            return
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, */*"}
        ) as s:
            await self._seed(s)
            logger.info("[rss] live with {} feeds", len(self.feeds))
            while True:
                try:
                    for f in self.feeds:
                        await self._poll_feed(s, f, emit=True)
                        await asyncio.sleep(PER_FEED_GAP_S)
                    await asyncio.sleep(self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # pragma: no cover
                    logger.warning("[rss] sweep failed: {}", e)
                    await asyncio.sleep(self.retry_seconds)

    async def _seed(self, s: aiohttp.ClientSession) -> None:
        """First pass — populate seen set without emitting. Avoids flooding
        the bus with a 50-item burst on boot."""
        for f in self.feeds:
            try:
                await self._poll_feed(s, f, emit=False)
            except Exception as e:
                logger.warning("[rss] seed {} failed: {}", f.id, e)

    async def _poll_feed(self, s: aiohttp.ClientSession, f: FeedSpec, *, emit: bool) -> None:
        try:
            async with s.get(f.url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)) as r:
                if r.status >= 400:
                    logger.warning("[rss] {} HTTP {}", f.id, r.status)
                    return
                body = await r.text()
        except asyncio.TimeoutError:
            logger.warning("[rss] {} timeout", f.id)
            return
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            logger.warning("[rss] {} parse failed: {}", f.id, e)
            return
        seen = self._seen.setdefault(f.id, set())
        new_emitted = 0
        for eid, title, summary in _extract_entries(root):
            key = _hash_entry(f.id, eid, title)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_SEEN_PER_FEED:
                # drop oldest ~half arbitrarily; dedupe doesn't need to be exact
                self._seen[f.id] = set(list(seen)[-MAX_SEEN_PER_FEED // 2:])
                seen = self._seen[f.id]
            if not emit:
                continue
            if f.bitcoin_filter and not _is_bitcoin_relevant(title, summary):
                continue
            score = _score_title(title + " " + summary)
            if score == 0.0 and f.category not in ("regulation", "macro"):
                # zero-scored entries from chatter feeds are noise; drop.
                # Regulation/macro feeds we emit even at score 0 — the
                # occurrence itself (a Fed press release) is news worth
                # waking tribes for.
                continue
            await self._emit(f, title, summary, score)
            new_emitted += 1
        if emit and new_emitted:
            logger.debug("[rss] {}: +{} emitted", f.id, new_emitted)

    async def _emit(self, f: FeedSpec, title: str, summary: str, score: float) -> None:
        # Event classifier promotes a matched news item from "±1 sentiment
        # score" to "this is a MonetaryPolicyEasing with ~90bps priors and
        # a +1 side." When it fires, we override the keyword-derived side
        # and carry the calibrated impact_bps in the payload so the
        # event-latency projector (Phase D) can inject an appropriately
        # sized pressure wave. When it misses, we keep the original ±1
        # score — at worst we fall back to the crude-but-present signal.
        cls = classify_event(title, summary, f.category, f"rss.{f.id}")
        if cls.event_type != "none":
            # Side from classifier overrides sentiment in both tags and
            # the score (score now ∈ {-1, 0, +1} × ~weight for back-compat).
            score = float(cls.side) * min(1.0, cls.impact_bps / 200.0)
        side_tag = "news_bull" if score > 0 else "news_bear" if score < 0 else "news_neutral"
        tags = {"news", f.category, side_tag, *f.tags}
        if cls.event_type != "none":
            tags.add(f"event:{cls.event_type}")
        severity = min(1.0, abs(score) * f.weight) if score != 0 else min(0.5, 0.3 * f.weight)
        ev = Event(
            ts=Event.now_utc(),
            source=f"rss.{f.id}",
            kind="news",
            symbol=None,
            payload={
                "title": title[:240], "score": score, "category": f.category,
                "summary": summary[:400],
                "event_type": cls.event_type,
                "impact_bps": cls.impact_bps,
                "side": cls.side,
                "classifier_reason": cls.reason,
            },
            tags=tags,
            severity=severity,
        )
        await self.bus.publish(ev)


# ── Curated free feed list ───────────────────────────────────────────────
# All feeds below are publicly available without auth. Weights reflect the
# impact of the source on BTC price historically — SEC/Fed announcements
# are high-impact, a random crypto blog is low. Weight is used only as a
# severity multiplier; it does not filter.

CURATED_FEEDS: list[FeedSpec] = [
    # ── Regulation / policy — highest impact ──────────────────────────
    FeedSpec(id="sec_press", category="regulation", weight=1.0, bitcoin_filter=True,
             url="https://www.sec.gov/news/pressreleases.rss",
             tags=["sec", "policy"]),
    FeedSpec(id="fed_press", category="regulation", weight=0.9, bitcoin_filter=False,
             url="https://www.federalreserve.gov/feeds/press_all.xml",
             tags=["fed", "monetary_policy", "macro"]),
    FeedSpec(id="cftc_press", category="regulation", weight=0.8, bitcoin_filter=True,
             url="https://www.cftc.gov/PressRoom/PressReleases/rss.xml",
             tags=["cftc", "policy"]),

    # ── Crypto news majors ─────────────────────────────────────────────
    FeedSpec(id="coindesk", category="news", weight=0.7, bitcoin_filter=True,
             url="https://www.coindesk.com/arc/outboundfeeds/rss/",
             tags=["crypto_press"]),
    FeedSpec(id="cointelegraph", category="news", weight=0.6, bitcoin_filter=True,
             url="https://cointelegraph.com/rss",
             tags=["crypto_press"]),
    FeedSpec(id="bitcoinmagazine", category="news", weight=0.6, bitcoin_filter=False,
             url="https://bitcoinmagazine.com/feed",
             tags=["crypto_press", "bitcoin_maximalist"]),
    FeedSpec(id="decrypt", category="news", weight=0.5, bitcoin_filter=True,
             url="https://decrypt.co/feed",
             tags=["crypto_press"]),
    FeedSpec(id="theblock", category="news", weight=0.7, bitcoin_filter=True,
             url="https://www.theblock.co/rss.xml",
             tags=["crypto_press", "business"]),

    # ── Business / mainstream — often moves price via mainstream attention
    FeedSpec(id="reuters_business", category="business", weight=0.7, bitcoin_filter=True,
             url="https://www.reutersagency.com/feed/?best-topics=business-finance",
             tags=["mainstream"]),
    FeedSpec(id="hn_frontpage", category="news", weight=0.3, bitcoin_filter=True,
             url="https://hnrss.org/frontpage",
             tags=["tech"]),

    # ── Influencer substacks (free RSS) ────────────────────────────────
    # These are high-signal for copy_trader archetype — substack's free RSS
    # doesn't require auth. Add more as you find them.
    FeedSpec(id="substack_pomp", category="influencer", weight=0.5, bitcoin_filter=False,
             url="https://pomp.substack.com/feed",
             tags=["influencer", "bitcoin_maximalist"]),
    FeedSpec(id="substack_balaji", category="influencer", weight=0.6, bitcoin_filter=False,
             url="https://balajis.substack.com/feed",
             tags=["influencer", "macro"]),
    # Nitter mirror for @elonmusk — UNSTABLE (nitter instances come & go).
    # If this 404s, the poller just logs a warning and moves on. Replace
    # with whatever nitter instance is alive in your locale.
    FeedSpec(id="nitter_elonmusk", category="influencer", weight=0.6, bitcoin_filter=True,
             url="https://nitter.net/elonmusk/rss",
             tags=["influencer", "twitter"]),
    FeedSpec(id="nitter_saylor", category="influencer", weight=0.5, bitcoin_filter=False,
             url="https://nitter.net/saylor/rss",
             tags=["influencer", "twitter", "bitcoin_maximalist"]),
    FeedSpec(id="nitter_cz", category="influencer", weight=0.5, bitcoin_filter=True,
             url="https://nitter.net/cz_binance/rss",
             tags=["influencer", "twitter", "exchange"]),
]
