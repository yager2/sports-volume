"""
Rothera Exchange daily sports volume tracker.

Rothera (formerly LedgerX / MIAX Derivatives Exchange) is a CFTC DCM+DCO owned
by a Robinhood / Susquehanna joint venture. It is the exchange behind
**Robinhood's** prediction-market product - a top-3 US venue by volume,
overwhelmingly sports.

Source: Rothera publishes a CFTC Part-16 end-of-day market report every day at
~4:15pm CT, listed at https://www.rothera.io/volume-open-interest and hosted at
    https://publicfiles.rotheramarkets.com/clearing/outbound/mkt_eod_YYYYMMDD.csv
One row per contract, columns:
    date, product, symbol, expiration_date, trade_count, volume, oi,
    block_volume, settlement_price, open, high, low, close, contracts_delivered
`volume` is contracts traded (one-sided, CFTC convention); every contract is a
$1-max-payout binary (open/high/low/close all price 0.00-1.00) - same basis as
the other venues. `open/high/low/close` are real trade prices, so notional is
estimated per-market (H/L midpoint), not with a blended average.

Sports filter: Rothera lists mostly sports (MLB, NFL, NCAAF, NBA, NHL, EPL,
UCL, PGA, US Open tennis, ...) plus a modest non-sports tail - US macro/econ
(`EC*`), and elections (`USPRES*`, `*RACE`, `CNTRL*`, `VPPARTYNOM`). Those
prefixes are excluded; everything else counts as sports. New/unknown prefixes
are logged to rothera_unknown_prefixes.csv for review.

No combo/parlay product appears in the report, so Rothera has no combo column
(like Kalshi).

Usage:
    python rothera_daily_volume.py

Outputs:
    rothera_daily_totals.csv   -> date, total_volume, notional_est
    rothera_daily_by_sport.csv -> date, sport, volume
"""

import csv
import io
import os
import re
import urllib.error
import urllib.request

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://publicfiles.rotheramarkets.com/clearing/outbound"
RAW_CACHE_DIR = os.path.join(HERE, "rothera_raw_cache")
TOTALS_CSV = os.path.join(HERE, "rothera_daily_totals.csv")
BY_SPORT_CSV = os.path.join(HERE, "rothera_daily_by_sport.csv")
UNKNOWN_CSV = os.path.join(HERE, "rothera_unknown_prefixes.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}

# Non-sports markets on Rothera: US macro/econ prints and politics/elections.
NON_SPORTS_EXACT = {
    "GOVRACE", "SENRACE", "HOURACE", "USPRESIDENT", "USPRESNOM", "USPRESPARTY",
    "VPPARTYNOM", "POTUS", "CNTRLCONG", "CNTRLHOU", "CNTRLSEN",
}
NON_SPORTS_RE = re.compile(r"^(EC[A-Z]*|USPRES|CNTRL|VP[A-Z]*NOM)")

# Known sports prefixes - anything matching is definitely kept; used only to
# decide what to log as "unknown" for review.
SPORTS_HINT_RE = re.compile(
    r"^(MLB|NFL|NBA|NHL|NCAA|CFB|EPL|UCL|UEL|MLS|PGA|LPGA|ATP|WTA|MUSO|WUSO|"
    r"UFC|BOX|F1|NASCAR|INDYCAR|LALIGA|SERIEA|BUNDES|LIGUE1|WNBA|GOLF|TENNIS|"
    r"SOCCER|NCAAF|NCAAM|NCAAW|LIGAMX|BRASIL)")


def report_url(date: str) -> str:
    return f"{BASE_URL}/mkt_eod_{date.replace('-', '')}.csv"


def cache_path_for(date: str) -> str:
    return os.path.join(RAW_CACHE_DIR, f"{date}.csv")


def download(date: str) -> str:
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    path = cache_path_for(date)
    if os.path.exists(path):
        return path
    req = urllib.request.Request(report_url(date), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  Warning: {date} -> {e.code}")
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: {date} download failed ({e})")
        return ""
    with open(path, "wb") as f:
        f.write(content)
    return path


def _price(row: dict) -> float:
    """Per-market trade price: high/low midpoint, falling back to close/open/
    settlement. Settlement is the resolved 0/1 outcome on an expiry day, so it
    is only a last resort."""
    def num(k):
        try:
            return float(row.get(k) or "")
        except ValueError:
            return None
    hi, lo = num("high"), num("low")
    if hi is not None and lo is not None and (hi > 0 or lo > 0):
        return (hi + lo) / 2
    for k in ("close", "open", "settlement_price"):
        v = num(k)
        if v is not None and 0 < v < 1:
            return v
    return 0.5


def is_non_sports(prefix: str) -> bool:
    return prefix in NON_SPORTS_EXACT or bool(NON_SPORTS_RE.match(prefix))


def parse_report(path: str) -> dict:
    total = 0.0
    notional = 0.0
    by_sport: dict = {}
    unknown: dict = {}
    non_sports = 0.0

    all_total = 0.0          # whole Rothera book - sports + US macro + elections
    all_notional = 0.0

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                vol = float(row.get("volume") or 0)
            except ValueError:
                continue
            if vol <= 0:
                continue
            prefix = (row.get("product") or "").split("-")[0].upper()
            if not prefix:
                continue
            all_total += vol
            all_notional += vol * _price(row)
            if is_non_sports(prefix):
                non_sports += vol
                continue
            total += vol
            notional += vol * _price(row)
            by_sport[prefix] = by_sport.get(prefix, 0.0) + vol
            if not SPORTS_HINT_RE.match(prefix):
                unknown[prefix] = unknown.get(prefix, 0.0) + vol

    return {
        "total_volume": total,
        "notional_est": notional,
        "non_sports_volume": non_sports,
        "all_volume": all_total,
        "all_notional_est": all_notional,
        "by_sport": by_sport,
        "unknown": unknown,
    }


def main():
    vc.drop_stale_cache(lambda d: [cache_path_for(d)])

    totals_rows = []
    sport_rows = []
    unknown_rows = []

    for date in vc.daterange():
        path = download(date)
        if not path:
            continue
        parsed = parse_report(path)
        if parsed["total_volume"] <= 0:
            continue
        totals_rows.append({
            "date": date,
            "total_volume": round(parsed["total_volume"], 2),
            "notional_est": round(parsed["notional_est"], 2),
            "all_volume": round(parsed["all_volume"], 2),
            "all_notional_est": round(parsed["all_notional_est"], 2),
        })
        for sport, vol in parsed["by_sport"].items():
            sport_rows.append({"date": date, "sport": sport, "volume": round(vol, 2)})
        for pfx, vol in parsed["unknown"].items():
            unknown_rows.append({"date": date, "prefix": pfx, "volume": round(vol, 2)})
        print(f"  {date}: {parsed['total_volume']:,.0f} contracts "
              f"(+{parsed['non_sports_volume']:,.0f} non-sports excluded)")

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "notional_est", "all_volume", "all_notional_est"],
                 totals_rows)
    vc.write_csv(BY_SPORT_CSV, ["date", "sport", "volume"], sport_rows)
    if unknown_rows:
        vc.write_csv(UNKNOWN_CSV, ["date", "prefix", "volume"], unknown_rows)
        print(f"\nNote: {len(unknown_rows)} unrecognised-prefix rows -> {UNKNOWN_CSV}")

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")


if __name__ == "__main__":
    main()
