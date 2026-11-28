"""
Fail loudly when the tracker's data has gone stale or silently lost a source.

Run after the pipeline (the daily workflow does). Exit code 1 = something needs
a look. Checks:
  * every source file's newest date is within MAX_AGE_DAYS of today,
  * every venue has a value in the unified sheet for a recent date,
  * venues that run a parlay product don't suddenly report ~0% parlay (that is
    what a renamed product code looks like),
  * DeFi Rate (Kalshi whole-exchange) is still being read, not just projected.
"""

import csv
import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_AGE_DAYS = 4

SOURCES = [
    "novig_daily_totals.csv", "dkex_daily_totals.csv", "polymarket_us_daily_totals.csv",
    "udx_daily_totals.csv", "cdna_daily_totals.csv", "rothera_daily_totals.csv",
    "kalshi_sports_daily_totals.csv", "kalshi_daily_total.csv",
]
VENUES = ["kalshi", "novig", "dkex", "polymarket_us", "udx", "cdna", "rothera"]
COMBO_VENUES = ["kalshi", "novig", "dkex", "polymarket_us", "udx", "cdna"]


def rows(name):
    with open(os.path.join(HERE, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def age(day):
    return (datetime.now(timezone.utc).date() - date.fromisoformat(day)).days


def main():
    problems = []
    for name in SOURCES:
        data = rows(name)
        newest = max((r["date"] for r in data), default=None)
        if newest is None or age(newest) > MAX_AGE_DAYS:
            problems.append(f"{name}: newest date is {newest}")

    unified = sorted(rows("unified_sports_volume.csv"), key=lambda r: r["date"])
    recent = unified[-3:]
    for v in VENUES:
        if not any((r.get(f"{v}_contracts") or "").strip() for r in recent):
            problems.append(f"unified sheet: no {v} volume in the last {len(recent)} days")
    for v in COMBO_VENUES:
        pct = [float(r[f"{v}_combo_pct"]) for r in unified[-7:]
               if (r.get(f"{v}_combo_pct") or "").strip()]
        if pct and max(pct) < 5:
            problems.append(f"{v}: parlay share under 5% all week - product code changed?")

    kx = sorted(rows("kalshi_daily_total.csv"), key=lambda r: r["date"])
    real = [r["date"] for r in kx if r.get("source") == "defirate"]
    if not real or age(real[-1]) > MAX_AGE_DAYS + 2:
        problems.append(f"DeFi Rate: last real Kalshi total is {real[-1] if real else None}")

    if problems:
        for p in problems:
            print(f"::error::{p}")
        sys.exit(1)
    print("freshness check passed")


if __name__ == "__main__":
    main()
