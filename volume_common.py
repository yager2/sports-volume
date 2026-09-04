"""Shared helpers for the sports-volume pullers.

Design contract (all five pullers follow this):

  * START_DATE is the earliest date any puller records. Set once here so the
    whole sheet lines up. History before this is intentionally not collected.

  * Each run recomputes EVERY date in [START_DATE, yesterday] from cached raw
    files. Raw files are re-downloaded when they are missing or within
    RECHECK_DAYS of today (so a report that was still being finalised when we
    first saw it gets corrected on the next run). Older raw files are trusted
    from cache.

  * Totals CSVs are rewritten in full every run, sorted by date. This makes a
    backfill and a daily update the exact same code path and means a bad/partial
    day can never get frozen into the file.

"Trade volume" note: every source here reports volume as a COUNT OF CONTRACTS
traded (standard CFTC/futures one-sided convention - a buy matched with a sell
is one contract, not two). Each contract is a $1-max-payout instrument, so the
totals are "notional volume at $1/contract". That is ~2x the USDC-changed-hands
figure the platforms cite publicly (which is contracts x price). The convention
is identical across all five sources, so cross-platform comparison is
apples-to-apples; just read the numbers as contracts, not dollars. See
VOLUME_METHODOLOGY.md.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone

# Earliest date collected, shared by every puller.
START_DATE = "2026-08-01"

# Always re-download + recompute this many trailing days each run, in case the
# source report was still being written when we first fetched it.
RECHECK_DAYS = 3

_FMT = "%Y-%m-%d"


def yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime(_FMT)


def today() -> str:
    return datetime.now(timezone.utc).strftime(_FMT)


def daterange(start: str = START_DATE, end: str | None = None):
    """Yield 'YYYY-MM-DD' from start to end inclusive (end defaults to yesterday)."""
    end = end or yesterday()
    cur = datetime.strptime(start, _FMT)
    stop = datetime.strptime(end, _FMT)
    while cur <= stop:
        yield cur.strftime(_FMT)
        cur += timedelta(days=1)


def in_range(date: str, end: str | None = None) -> bool:
    return START_DATE <= date <= (end or yesterday())


def is_stale(date: str) -> bool:
    """True if `date` is recent enough that its source report may still change."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime(_FMT)
    return date >= cutoff


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    """Overwrite `path` with `rows`, sorted by the 'date' field."""
    rows = sorted(rows, key=lambda r: r["date"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def drop_stale_cache(cache_paths_for_date) -> None:
    """Delete cached raw files for every in-range date that `is_stale`.

    `cache_paths_for_date` is a callable: date -> list[str] of file paths.
    """
    for date in daterange():
        if is_stale(date):
            for p in cache_paths_for_date(date):
                if p and os.path.exists(p):
                    os.remove(p)
