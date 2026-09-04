"""
DKeX (DraftKings Predictions Exchange, formerly Railbird) daily volume + combo tracker.

Source: DKeX publishes a manifest of daily per-market CSV reports at
    https://www.railbirdexchange.com/reports/daily-market/manifest.json
Each entry lists a date and a relative href to that day's full CSV
(one row per individual market: Business Date, Symbol, Status, Open
Interest, Trade Volume, High/Low/Settlement price, Maturity Date/Time).

Every row in these reports is a sports market (MLB, CFB, WNBA, KBO, NPB,
EPL, LaLiga, MLS, Bundesliga, PGA, NASCAR, IndyCar, etc.) - no separate
sports filter is needed. Combo/parlay markets are directly identifiable:
their Symbol starts with "COMBO-". Everything else is a straight market.

This script:
  1. Pulls the manifest, finds dates not yet processed.
  2. Downloads each day's CSV (caches raw files in raw_cache/ so re-runs
     don't re-download).
  3. Sums Trade Volume into straight vs. combo vs. total per day.
  4. Also breaks out volume by sport (symbol prefix) for the non-combo rows.
  5. Writes dkex_daily_totals.csv (date, total_volume, straight_volume,
     combo_volume, combo_pct) and dkex_daily_by_sport.csv (date, sport,
     volume).

Usage:
    pip install requests
    python dkex_daily_volume.py

Re-running only processes new dates - safe to run daily on a schedule.
"""

import csv
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
import json

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_URL = "https://www.railbirdexchange.com/reports/daily-market/manifest.json"
BASE_URL = "https://www.railbirdexchange.com"
RAW_CACHE_DIR = os.path.join(HERE, "dkex_raw_cache")
TOTALS_CSV = os.path.join(HERE, "dkex_daily_totals.csv")
BY_SPORT_CSV = os.path.join(HERE, "dkex_daily_by_sport.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}


def fetch_manifest() -> list[dict]:
    req = urllib.request.Request(MANIFEST_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"Failed to fetch manifest: {e.code} {e.read().decode('utf-8')}")
    reports = data.get("reports", [])
    if not reports:
        sys.exit("Manifest returned no reports - check the URL/format hasn't changed.")
    return reports


def cache_path_for(date: str) -> str:
    return os.path.join(RAW_CACHE_DIR, f"{date}.csv")


def download_report(href: str, date: str) -> str:
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    local_path = cache_path_for(date)
    if os.path.exists(local_path):
        return local_path

    # href is a relative path with spaces/parens - needs URL-encoding for the request,
    # but requests to this host seem to accept the raw path too. Encode defensively.
    full_url = BASE_URL + urllib.parse.quote(href, safe="/()")
    req = urllib.request.Request(full_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        print(f"  Warning: failed to download {date} ({e.code}) - skipping.")
        return ""

    with open(local_path, "wb") as f:
        f.write(content)
    return local_path


def _price_dollars(row: dict) -> float:
    """DKeX quotes 'High Price (USD)' / 'Low Price (USD)' like '$0.54'. Use the
    H/L midpoint; the Settlement/Last column is the resolved 0/1 outcome, not a
    trade price, so it is not used."""
    def num(x):
        x = (x or "").replace("$", "").strip()
        return float(x) if x else None
    hi, lo = num(row.get("High Price (USD)")), num(row.get("Low Price (USD)"))
    if hi is not None and lo is not None:
        return (hi + lo) / 2
    return hi if hi is not None else (lo if lo is not None else 0.5)


def parse_report(path: str) -> dict:
    """Returns dict with total/straight/combo volume, per-sport volume, notional."""
    straight_vol = 0.0
    combo_vol = 0.0
    notional = 0.0
    sport_vol: dict = {}

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vol_raw = row.get("Trade Volume", "")
            if not vol_raw:
                continue
            try:
                vol = float(vol_raw)
            except ValueError:
                continue
            if vol == 0:
                continue

            notional += vol * _price_dollars(row)
            symbol = row.get("Symbol", "")
            if symbol.startswith("COMBO"):
                combo_vol += vol
            else:
                straight_vol += vol
                prefix = symbol.split("-")[0] if "-" in symbol else symbol
                sport_vol[prefix] = sport_vol.get(prefix, 0.0) + vol

    return {
        "straight_volume": straight_vol,
        "combo_volume": combo_vol,
        "total_volume": straight_vol + combo_vol,
        "sport_volume": sport_vol,
        "notional_est": notional,
    }


def main():
    print("Fetching DKeX manifest...")
    reports = fetch_manifest()

    # Re-download the trailing few days (DKeX finalises a report over the course
    # of the day after the business date, so a freshly-seen day is often partial).
    vc.drop_stale_cache(lambda d: [cache_path_for(d)])

    href_by_date = {r["date"]: r["href"] for r in reports}
    target_dates = [d for d in vc.daterange() if d in href_by_date]
    missing = [d for d in vc.daterange() if d not in href_by_date]
    if missing:
        print(f"Note: DKeX manifest has no report for {len(missing)} date(s) in "
              f"range ({missing[0]}..{missing[-1]}) - skipped.")

    totals_rows = []
    sport_rows = []

    for date in target_dates:
        print(f"  Processing {date}...")
        local_path = download_report(href_by_date[date], date)
        if not local_path:
            continue

        parsed = parse_report(local_path)
        combo_pct = (
            100 * parsed["combo_volume"] / parsed["total_volume"]
            if parsed["total_volume"] > 0 else 0
        )
        totals_rows.append({
            "date": date,
            "total_volume": parsed["total_volume"],
            "straight_volume": parsed["straight_volume"],
            "combo_volume": parsed["combo_volume"],
            "combo_pct": round(combo_pct, 2),
            "notional_est": round(parsed["notional_est"], 2),
        })
        for sport, vol in parsed["sport_volume"].items():
            sport_rows.append({"date": date, "sport": sport, "volume": vol})

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "straight_volume", "combo_volume",
                  "combo_pct", "notional_est"],
                 totals_rows)
    vc.write_csv(BY_SPORT_CSV, ["date", "sport", "volume"], sport_rows)

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")
    print(f"Per-sport breakdown -> {BY_SPORT_CSV}")


if __name__ == "__main__":
    main()
