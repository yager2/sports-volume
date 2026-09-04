"""
Underdog Exchange (UDX / Aristotle Exchange DCM) daily volume + combo tracker.

Source: manifest at
    https://cftc-public-reports.underdogfantasy.com/manifest.json
Lists two report families per date:
  - market_data: per-market daily snapshot (what this script uses)
  - trade_data: individual trade records ("TCR" = Trade Capture Report,
    a standard CFTC term) - much larger, not used here but available if
    per-trade granularity is ever needed.

market_data JSON is a flat list of rows, one per market:
    {date, ticker_name, report_ticker, payout_type, open_interest,
     daily_volume, block_volume, high, low, status}

Every ticker observed is UDX-prefixed sports (NCAAMFB, MLB, WNBA, EPL,
Serie A, LaLiga, Liga MX, Ligue 1, MLS, ATP, etc.) - this exchange is
100% sports, no category filtering needed (unlike Polymarket US).

Combo identification: report_ticker == "UDXCOMBO" - Underdog's combo/
multi-event product, confirmed directly in sample data.

Manifest entries with row_count == 0 (many early dates, before the
exchange had real volume - roughly pre-July 24 2026) are skipped
automatically to avoid wasted downloads.

Usage:
    pip install requests
    python udx_daily_volume.py

Outputs:
    udx_daily_totals.csv -> date, total_volume, combo_volume, combo_pct
"""

import csv
import json
import os
import sys
import urllib.request
import urllib.error

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_URL = "https://cftc-public-reports.underdogfantasy.com/manifest.json"
RAW_CACHE_DIR = os.path.join(HERE, "udx_raw_cache")
TOTALS_CSV = os.path.join(HERE, "udx_daily_totals.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}
COMBO_TICKER = "UDXCOMBO"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def get_manifest() -> list[dict]:
    data = json.loads(fetch(MANIFEST_URL).decode("utf-8"))
    entries = data.get("reports", {}).get("market_data", [])
    if not entries:
        sys.exit("Manifest returned no market_data entries - check the URL/format hasn't changed.")
    return entries


def cache_path_for(date: str) -> str:
    return os.path.join(RAW_CACHE_DIR, f"{date}.json")


def get_cached_or_fetch(date: str, url: str) -> list:
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    cache_path = cache_path_for(date)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    content = fetch(url)
    with open(cache_path, "wb") as f:
        f.write(content)
    return json.loads(content.decode("utf-8"))


def parse_rows(rows: list) -> tuple[float, float]:
    """Returns (total_volume, combo_volume)."""
    total = 0.0
    combo = 0.0
    for row in rows:
        vol = row.get("daily_volume") or 0
        vol = float(vol)
        total += vol
        if row.get("report_ticker") == COMBO_TICKER:
            combo += vol
    return total, combo


def main():
    print("Fetching Underdog Exchange manifest...")
    entries = get_manifest()

    vc.drop_stale_cache(lambda d: [cache_path_for(d)])

    # Keep only in-range dates that actually have rows (early dates are 0-row).
    entry_by_date = {
        e["date"]: e for e in entries
        if vc.in_range(e["date"]) and e.get("row_count", 0) > 0
    }
    target_dates = sorted(entry_by_date)

    if not target_dates:
        print("No in-range dates with data yet.")
        return

    print(f"{len(target_dates)} date(s) to process.")

    totals_rows = []

    for date in target_dates:
        e = entry_by_date[date]
        print(f"  Processing {date} ({e.get('row_count', 0):,} rows)...")

        try:
            rows = get_cached_or_fetch(date, e["url"])
            total, combo = parse_rows(rows)
        except Exception as ex:
            print(f"    FAILED ({ex}) - skipping this run")
            continue

        combo_pct = round(100 * combo / total, 2) if total > 0 else 0.0
        totals_rows.append({
            "date": date,
            "total_volume": round(total, 2),
            "combo_volume": round(combo, 2),
            "combo_pct": combo_pct,
        })
        print(f"    total = {total:,.0f}  |  combo = {combo:,.0f} ({combo_pct}%)")

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "combo_volume", "combo_pct"],
                 totals_rows)

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")


if __name__ == "__main__":
    main()
