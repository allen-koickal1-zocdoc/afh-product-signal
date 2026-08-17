#!/usr/bin/env python3
"""
resolve_window.py -- work out which window a monthly run should cover.

The report is cumulative on purpose: the trend column, the sparklines and the
level-shift detector all need the full history, so `start` is always the first
AFH ticket. What the month selects is the *end* of the window, and therefore
which month is the last complete bar on the chart.

Default is the last complete month, not today. A run on the 1st that ended
"today" would put a one-day partial month in the last sparkline slot and make
every month-over-month trend read negative.

Asking for the current month is allowed -- the end clamps to today and the
label says month-to-date, so nobody mistakes a partial month for a finished one.
"""

import argparse
import calendar
import os
import re
import sys
from datetime import date, datetime


def month_bounds(year: int, month: int):
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def previous_month(today: date):
    return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)


def resolve(month: str, first_day: str, today: date) -> dict:
    if month:
        # strptime("%Y-%m") accepts "2026-1", which would silently produce a
        # differently-named archive path than the padded form. Be strict.
        m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", month)
        if not m:
            sys.exit(f"--month must be YYYY-MM, got {month!r}")
        year, mon = int(m.group(1)), int(m.group(2))
    else:
        year, mon = previous_month(today)

    m_start, m_end = month_bounds(year, mon)
    if m_start > today:
        sys.exit(f"{month} has not started yet (today is {today})")

    partial = m_end >= today
    end = min(m_end, today)
    name = f"{calendar.month_name[mon]} {year}"

    return {
        "start": first_day,
        "end": end.isoformat(),
        "month": f"{year:04d}-{mon:02d}",
        "label": f"{name} (month to date)" if partial else name,
        "partial": "true" if partial else "false",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="", help="YYYY-MM; blank = last complete month")
    ap.add_argument("--first-day", required=True, help="Start of history, YYYY-MM-DD")
    ap.add_argument("--today", default=None, help="Override today (testing)")
    args = ap.parse_args()

    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    )
    out = resolve(args.month.strip(), args.first_day, today)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in out.items()]
    if gh_output:
        with open(gh_output, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
