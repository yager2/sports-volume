"""
Novig daily volume aggregator (v2 - adds combo/straight split).

Pulls Novig's public exchange-data files (data.novig.com) and builds a running
CSV of total daily notional volume, split into combo vs. straight, plus a
breakdown by product (reportTicker).

Source: https://docs.novig.com/api-reference/trade-data
- Static files, no auth, one markets.csv per trading day.
- markets.csv columns: date, marketId, reportTicker, openInterest, dailyVolume,
  open, high, low, close, status
- dailyVolume is in contracts, where 1 contract = $1 notional at settlement,
  so summing dailyVolume across all rows for a date = that day's total
  notional volume.
- Combo split: every combo trade shows up in markets.csv as its own row with
  reportTicker == "COMBO" (each combo trades once and appears only on the day
  it executed). So combo volume for a date = sum of dailyVolume where
  reportTicker == "COMBO"; straight volume = everything else. No need to
  parse trades.csv separately - it's already broken out in the file this
  script already reads.

Usage:
    python novig_daily_volume.py

Outputs (created in the same folder as this script):
    novig_daily_totals.csv     -> date, total_volume, straight_volume, combo_volume, combo_pct
    novig_daily_by_product.csv -> date, reportTicker, volume
    raw_cache/<date>_markets.csv -> raw file per day, cached so re-runs are fast
"""

import csv
import json
import os
import urllib.request
from collections import defaultdict

import volume_common as vc

BASE = "https://data.novig.com/reporting/trade-data"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "raw_cache")
TOTALS_CSV = os.path.join(HERE, "novig_daily_totals.csv")
BY_PRODUCT_CSV = os.path.join(HERE, "novig_daily_by_product.csv")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "mention-edge/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def get_available_market_dates() -> list[str]:
    manifest = json.loads(fetch(f"{BASE}/index.json"))
    # marketDates covers the markets.csv (volume) files; absent on old manifests
    return manifest.get("marketDates", [])


def get_cached_or_fetch_markets_csv(date: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{date}_markets.csv")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()
    text = fetch(f"{BASE}/{date}/markets.csv")
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def cache_path_for(date: str) -> str:
    return os.path.join(CACHE_DIR, f"{date}_markets.csv")


def _price_dollars(row: dict) -> float:
    """Novig OHLC are in cents (e.g. '49.0' == $0.49). Use close, then mid."""
    for k in ("close", "open"):
        v = (row.get(k) or "").strip()
        if v:
            return min(float(v) / 100.0, 1.0)
    hi = (row.get("high") or "").strip()
    lo = (row.get("low") or "").strip()
    if hi and lo:
        return (float(hi) + float(lo)) / 200.0
    return 0.5


def parse_markets_csv(text: str):
    """Returns (total, straight, combo, by_product, notional_est)."""
    reader = csv.DictReader(text.splitlines())
    total = 0.0
    combo = 0.0
    notional = 0.0
    by_product: dict[str, float] = defaultdict(float)
    for row in reader:
        vol_str = (row.get("dailyVolume") or "0").strip()
        vol = float(vol_str) if vol_str else 0.0
        total += vol
        if vol:
            notional += vol * _price_dollars(row)
        ticker = row.get("reportTicker", "UNKNOWN")
        by_product[ticker] += vol
        if ticker == "COMBO":
            combo += vol
    straight = total - combo
    return total, straight, combo, dict(by_product), notional


def main():
    # Re-download the trailing few days so a report that was still being
    # finalised when we first saw it gets corrected.
    vc.drop_stale_cache(lambda d: [cache_path_for(d)])

    available = set(get_available_market_dates())
    target_dates = [d for d in vc.daterange() if d in available]
    missing = [d for d in vc.daterange() if d not in available]
    if missing:
        print(f"Note: Novig has no markets.csv for {len(missing)} date(s) in range "
              f"(e.g. {missing[0]}..{missing[-1]}) - skipped.")

    totals_rows = []
    by_product_rows = []

    for date in target_dates:
        try:
            text = get_cached_or_fetch_markets_csv(date)
            total, straight, combo, by_product, notional = parse_markets_csv(text)
        except Exception as e:
            print(f"  {date}: FAILED ({e}) - skipping this run")
            continue

        combo_pct = round(100 * combo / total, 2) if total > 0 else 0.0
        totals_rows.append({
            "date": date,
            "total_volume": round(total, 2),
            "straight_volume": round(straight, 2),
            "combo_volume": round(combo, 2),
            "combo_pct": combo_pct,
            "notional_est": round(notional, 2),
        })
        for ticker, vol in by_product.items():
            by_product_rows.append(
                {"date": date, "reportTicker": ticker, "volume": round(vol, 2)}
            )
        print(f"  {date}: total = {total:,.0f}  |  combo = {combo:,.0f} ({combo_pct}%)  |  straight = {straight:,.0f}")

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "straight_volume", "combo_volume",
                  "combo_pct", "notional_est"],
                 totals_rows)
    vc.write_csv(BY_PRODUCT_CSV, ["date", "reportTicker", "volume"], by_product_rows)

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}\nBy-product -> {BY_PRODUCT_CSV}")


if __name__ == "__main__":
    main()
