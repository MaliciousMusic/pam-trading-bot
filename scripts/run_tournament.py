"""Run a variant tournament.

Usage:
    python -m scripts.run_tournament                 # defaults to today's tape
    python -m scripts.run_tournament --event-day 2026-04-18
    python -m scripts.run_tournament --event-day 2026-04-18 --audit-day 2026-04-17

Reads the event tape under `data/events/YYYY-MM-DD.jsonl` and the
ledger under `data/ledger/YYYY-MM-DD.jsonl`, audits yesterday to
generate hypotheses, spawns variant councils, replays each against
the tape, and writes a leaderboard to `data/tournaments/<tag>/`.

Promotion is flagged but never automatic: if a variant beats parent
by > --promo-threshold (USD), its tribes are staged into a
`promoted/` subdir for explicit copy-over into config/tribes/.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.audit.tournament import run_tournament
from src.core.config import settings


def _default_event_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _default_audit_day() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event-day", default=_default_event_day(),
                    help="YYYY-MM-DD tape to replay (default: today UTC)")
    ap.add_argument("--audit-day", default=None,
                    help="YYYY-MM-DD ledger to audit for hypotheses "
                         "(default: same as --event-day)")
    ap.add_argument("--max-variants", type=int, default=6)
    ap.add_argument("--promo-threshold", type=float, default=0.0,
                    help="USD equity-delta edge over parent required to stage "
                         "a promotion candidate (default 0: any edge stages).")
    ap.add_argument("--tag", default=None,
                    help="generation tag (default: tourn_<UTC timestamp>)")
    args = ap.parse_args()

    audit_day = args.audit_day or args.event_day
    cfg = settings()
    logger.info("[tournament] event_day={} audit_day={} max_variants={}",
                args.event_day, audit_day, args.max_variants)
    result = run_tournament(
        event_day=args.event_day,
        audit_day_str=audit_day,
        settings=cfg,
        max_variants=args.max_variants,
        promotion_threshold_usd=args.promo_threshold,
        tag=args.tag,
    )
    if "error" in result:
        raise SystemExit(f"tournament aborted: {result['error']}")


if __name__ == "__main__":
    main()
