"""Run the daily audit against a ledger file.

Usage:
    python -m scripts.run_audit                # today (UTC)
    python -m scripts.run_audit 2026-04-18     # explicit day
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from src.audit.daily import audit_day, print_report


def main() -> None:
    if len(sys.argv) > 1:
        day = sys.argv[1]
    else:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = audit_day(day)
    print_report(report)


if __name__ == "__main__":
    main()
