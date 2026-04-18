"""Rule-combinatoric event classifier.

Maps a free-text news item (title + summary + source category) to a
structured `(event_type, impact_bps, side)` triple. The keyword lexicon
in rss_feeds.py gives a ±1 sentiment slider; this module adds *kind* —
what happened — and a *calibrated magnitude* in bps the market tends
to react by within ~1h.

The bps numbers below are priors: order-of-magnitude starting points
drawn from anecdotal post-mortems of prior BTC events (ETF approval =
huge; Elon tweet = small). Phase G's audit→calibration loop replaces
these constants with values learned from ForecastAuditor backscoring.

Design:
- Pure function. No I/O. Cheap to call per RSS item.
- Tiered matching: strongest signal wins (hack > regulation > macro >
  adoption > influencer). A title that matches "hack" AND "approved"
  is classified as hack_exploit — market reacts to the big bad thing.
- Returns "none" for low-signal items (random market commentary) so
  the RSS emitter can still fall through to the keyword sentiment
  score path.

The magnitude is a *potential* move, not a prediction. It's used by the
event-latency projector (Phase D) to size the pressure wave injected
into the forward simulation, and by the audit loop to filter which
events actually moved price.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Classification:
    event_type: str        # class label or "none"
    impact_bps: float      # calibrated magnitude at ~1h horizon (positive number)
    side: int              # -1 / 0 / +1
    reason: str            # short human-readable why


# ── Rule table (order matters — first match wins) ────────────────────────
# Each rule is (name, regex, side_hint, impact_bps).
# Regex is applied to title.lower() + " " + summary.lower() to give short
# titles + descriptive summaries a fair shot.

_RULES: list[tuple[str, re.Pattern, int, float]] = [
    # Exchange insolvency — catastrophic, FTX-scale reference.
    ("exchange_solvency", re.compile(
        r"\b(bankruptcy|insolvent|insolvency|chapter 11|halts withdrawals?|"
        r"withdrawal (pause|freeze|halt)|frozen funds|contagion)\b"
    ), -1, 600.0),
    # Hacks, exploits, stolen funds.
    ("hack_exploit", re.compile(
        r"\b(hack|hacked|exploit|exploited|drained|stolen \$?\d|"
        r"security breach|private key.*(leak|compromise))\b"
    ), -1, 120.0),
    # ETF approvals / rejections / large flows.
    ("etf_approval", re.compile(
        r"(etf.{0,30}(approval|approved|green.?light)|"
        r"(spot )?(btc|bitcoin) etf approv\w*|"
        r"sec approv\w*.{0,40}etf|approv\w*.{0,20}spot (btc|bitcoin) etf)"
    ), +1, 250.0),
    ("etf_rejection", re.compile(
        r"\b(etf (reject\w*|denied|disapprov\w*)|sec (reject\w*|disapprov\w*).*etf)\b"
    ), -1, 180.0),
    ("etf_flow_in", re.compile(
        r"\b(etf.{0,30}(inflow|net inflow|record inflow)|(blackrock|fidelity|ibit).{0,30}(buys?|acquires?|adds?))\b"
    ), +1, 60.0),
    ("etf_flow_out", re.compile(
        r"\b(etf.{0,30}(outflow|net outflow|record outflow))\b"
    ), -1, 50.0),
    # Enforcement action — sec/cftc suit, indictment, crackdown.
    ("reg_enforcement", re.compile(
        r"\b(sec (charges|sues|lawsuit|files|enforcement)|"
        r"cftc (charges|sues|lawsuit)|"
        r"indict(ment|ed)|subpoena|crackdown|banned|criminal charges)\b"
    ), -1, 140.0),
    # Regulatory tailwind — rule change favorable to crypto.
    ("reg_tailwind", re.compile(
        r"\b(pro.?crypto|deregulation|rules? eased|regulatory clarity|"
        r"bitcoin reserve|strategic (bitcoin )?reserve|legal tender)\b"
    ), +1, 120.0),
    # Monetary policy — FOMC, rate moves.
    ("monetary_policy_easing", re.compile(
        r"\b(rate cut|dovish|fed (cut|cuts)|easing|qe announced)\b"
    ), +1, 90.0),
    ("monetary_policy_tightening", re.compile(
        r"\b(rate hike|hawkish|fed (hike|raises)|tightening|qt announced)\b"
    ), -1, 90.0),
    # Macro data surprises.
    ("macro_cpi_hot", re.compile(
        r"(cpi.{0,30}(hot\w*|above|surge|higher than expected)|inflation (hot\w*|spikes))"
    ), -1, 70.0),
    ("macro_cpi_cool", re.compile(
        r"(cpi.{0,30}(cool\w*|below|lower than expected|soft\w*)|inflation (cool\w*|eas\w*|soft\w*))"
    ), +1, 70.0),
    # Corporate / sovereign adoption.
    ("adoption_corp", re.compile(
        r"\b((microstrategy|strategy|tesla|metaplanet).{0,40}(buys?|acquires?|adds? bitcoin)|"
        r"corporate (treasury|buy).{0,30}bitcoin)\b"
    ), +1, 55.0),
    # Influencer signals (fired by Nitter/Substack feeds).
    ("influencer_bullish", re.compile(
        r"\b(bullish|moon|accumulate|buying|ath|new high|pump)\b"
    ), +1, 25.0),
    ("influencer_bearish", re.compile(
        r"\b(bearish|dump|selling|crash|top is in|capitulate|rekt)\b"
    ), -1, 25.0),
]


def classify(title: str, summary: str = "", category: str = "news",
             source: str = "") -> Classification:
    """Return the strongest rule match. Falls through to 'none' when no rule
    fires (caller can still emit via keyword sentiment if it wants to).

    `category` ∈ {news, regulation, business, influencer, macro} biases the
    impact scaling: SEC press items carry more weight than a random
    substack, so we multiply the rule's base impact by a category factor.
    """
    text = (title + " " + summary).lower()
    # Category → impact multiplier. Regulation press releases tend to
    # print a bigger, more durable move than the same words on a blog;
    # influencer posts run hot but fade fast so we dampen their prior
    # bps budget.
    cat_mul = {
        "regulation": 1.15,
        "macro": 1.00,
        "business": 0.95,
        "news": 0.85,
        "influencer": 0.60,
    }.get(category, 1.0)

    for name, pat, side, bps in _RULES:
        if pat.search(text):
            # Gate influencer rules to influencer/news categories — an SEC
            # press release containing the word "bullish" should not be
            # mapped to influencer_bullish.
            if name.startswith("influencer_") and category not in (
                "influencer", "news"
            ):
                continue
            impact = max(1.0, bps * cat_mul)
            return Classification(
                event_type=name,
                impact_bps=impact,
                side=side,
                reason=f"matched /{pat.pattern[:60]}…/ in {category}",
            )
    return Classification(event_type="none", impact_bps=0.0, side=0, reason="no rule")
