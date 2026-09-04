"""
Crypto.com / Nadex (CDNA) daily sports event-contract volume + combo tracker.

North American Derivatives Exchange (Nadex) is a CFTC-registered DCM/DCO owned
by Crypto.com. It trades under two brands - "OG Prediction Markets" and
"Crypto.com | Derivatives North America (CDNA)" - and since mid-2026 clears ALL
sports flow from the FanDuel Predicts app as well. So this one column captures
Crypto.com's own prediction market + FanDuel Predicts + anything else routed to
the CDNA book.

Source: Nadex publishes a full end-of-day bulletin PDF at
    https://s3.amazonaws.com/market-data-prod.nadex.com/YYYYMMDD_dailybulletin.pdf
The first ~16-25 pages are a "Daily Bulletin Summary" table: one row per
contract series with that day's total traded Volume, columns
    Date | Instrument Type | Resource Class | Resource Description | Volume
Resource Class "EVENTS" is the prediction-market book (sports champions,
game/match markets, F1, plus a small non-sports tail of elections / climate /
culture / crypto-price events). "DIGITAL_CURRENCIES" and the rest are the
legacy Nadex crypto/fx/index binaries - excluded.

Each EVENTS row's Resource Description is a code like `CFB-00002-260829-M`,
`UFC-00001-260829-M`, `COMBOS`, `NFL-00001-2026`. The leading token is the
league/sport. `COMBOS` = the FanDuel-style multi-leg parlay product.

Contract face value: the EVENTS contracts are the $1 tier (the bulletin detail
prices every EVENTS strike between 0.01 and 0.99). Same "contracts x $1 face"
basis as Kalshi / Polymarket US / DKeX / UDX / Novig. The legacy $10 / $100
Nadex binaries are other resource classes and are not counted.

This script:
  1. For each date in [START_DATE, yesterday], downloads that day's bulletin PDF
     (cached in cdna_raw_cache/; trailing RECHECK_DAYS re-pulled).
  2. Extracts the first 40 pages with pdftotext -layout.
  3. Parses the Summary section (stops at "Daily Bulletin Detail"), keeps
     Resource Class == EVENTS, drops the non-sports prefix tail.
  4. Sums to sports total + combo (COMBOS prefix); breaks out by league.
  5. Writes cdna_daily_totals.csv (date, total_volume, straight_volume,
     combo_volume, combo_pct) and cdna_daily_by_sport.csv (date, sport, volume).

Needs `pdftotext` (poppler) on PATH.

Usage:
    python cdna_daily_volume.py
    python cdna_daily_volume.py --keep-pdf   # don't delete the big PDFs after parsing

Re-running rebuilds the whole range from cache - safe to run daily.
"""

import csv
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://s3.amazonaws.com/market-data-prod.nadex.com"
RAW_CACHE_DIR = os.path.join(HERE, "cdna_raw_cache")
TOTALS_CSV = os.path.join(HERE, "cdna_daily_totals.csv")
BY_SPORT_CSV = os.path.join(HERE, "cdna_daily_by_sport.csv")
UNKNOWN_CSV = os.path.join(HERE, "cdna_unknown_prefixes.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}
PAGES = 40  # summary section is well within the first 40 pages even on a busy CFB Saturday

# The non-sports tail of the EVENTS book: elections, economics, climate,
# culture/entertainment, standalone crypto-price events. ~0.15% of EVENTS
# volume. Everything else in EVENTS is a league/match/tournament market.
NON_SPORTS_PREFIXES = {"ELECT", "ELC", "EC", "CLIM", "CUL", "CRYPT", "POL", "FED",
                       "ECON", "COMPANIES", "COMPANY", "AWARDS", "ENT"}

# Parlay / multi-leg product -> the "combo" column (mirrors DKeX COMBO-, UDXCOMBO).
COMBO_PREFIXES = {"COMBOS", "COMBO"}

# The Volume column is sometimes a bare integer ("239"), sometimes a float
# ("239.0"), depending on the day's report build.
ROW = re.compile(
    r"^(\d{2}-[A-Z]{3}-\d{2})\s+([A-Z_]+)\s+([A-Z_]+)\s+(\S+)\s+([\d,]+(?:\.\d+)?)\s*$")


def pdf_url(date: str) -> str:
    return f"{BASE_URL}/{date.replace('-', '')}_dailybulletin.pdf"


def pdf_cache_path(date: str) -> str:
    return os.path.join(RAW_CACHE_DIR, f"{date}.pdf")


def txt_cache_path(date: str) -> str:
    return os.path.join(RAW_CACHE_DIR, f"{date}.txt")


def ensure_text(date: str) -> str:
    """Return the path to the extracted summary text for `date`, or "" on failure.

    Caches the parsed text (small); the source PDF (~40 MB) is deleted after
    extraction unless --keep-pdf is passed.
    """
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    txt_path = txt_cache_path(date)
    if os.path.exists(txt_path):
        return txt_path

    pdf_path = pdf_cache_path(date)
    if not os.path.exists(pdf_path):
        req = urllib.request.Request(pdf_url(date), headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            print(f"  Warning: no bulletin for {date} ({e.code}) - skipping.")
            return ""
        except Exception as e:  # noqa: BLE001
            print(f"  Warning: download failed for {date} ({e}) - skipping.")
            return ""
        with open(pdf_path, "wb") as f:
            f.write(data)

    exe = shutil.which("pdftotext")
    if not exe:
        sys.exit("pdftotext not found on PATH - install poppler.")
    try:
        subprocess.run([exe, "-layout", "-f", "1", "-l", str(PAGES),
                        pdf_path, txt_path], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"  Warning: pdftotext failed for {date}: {e.stderr.decode('utf-8', 'replace')[:200]}")
        return ""

    if "--keep-pdf" not in sys.argv:
        os.remove(pdf_path)
    return txt_path


def parse_summary(txt_path: str) -> dict:
    """Parse the Daily Bulletin Summary section. Returns sports straight/combo
    volume, per-league volume, and any unrecognised prefixes."""
    lines = open(txt_path, encoding="utf-8").read().splitlines()
    end = next((i for i, l in enumerate(lines) if "Daily Bulletin Detail" in l),
               len(lines))

    straight = 0.0
    combo = 0.0
    by_sport: dict = {}
    unknown: dict = {}
    non_sports = 0.0

    for line in lines[:end]:
        s = line.strip()
        if not s or s.startswith("Date") or "Page" in s or "Bulletin" in s:
            continue
        m = ROW.match(s)
        if not m:
            continue
        _, _itype, cls, desc, vol_raw = m.groups()
        if cls != "EVENTS":
            continue
        try:
            vol = float(vol_raw.replace(",", ""))
        except ValueError:
            continue
        if vol == 0:
            continue

        prefix = re.split(r"[-0-9]", desc, maxsplit=1)[0] or desc

        if prefix in COMBO_PREFIXES:
            combo += vol
            continue
        if prefix in NON_SPORTS_PREFIXES:
            non_sports += vol
            continue

        straight += vol
        by_sport[prefix] = by_sport.get(prefix, 0.0) + vol
        # flag anything not obviously a league code so the allowlist can be reviewed
        if not re.fullmatch(r"[A-Z]{2,6}[0-9]?", prefix):
            unknown[prefix] = unknown.get(prefix, 0.0) + vol

    return {
        "straight_volume": straight,
        "combo_volume": combo,
        "total_volume": straight + combo,
        "non_sports_volume": non_sports,
        "by_sport": by_sport,
        "unknown": unknown,
    }


def main():
    vc.drop_stale_cache(lambda d: [pdf_cache_path(d), txt_cache_path(d)])

    totals_rows = []
    sport_rows = []
    unknown_rows = []

    for date in vc.daterange():
        print(f"  Processing {date}...", flush=True)
        txt_path = ensure_text(date)
        if not txt_path:
            continue
        parsed = parse_summary(txt_path)
        if parsed["total_volume"] <= 0:
            print(f"    no EVENTS volume parsed for {date} - skipping.")
            continue

        total = parsed["total_volume"]
        combo_pct = 100 * parsed["combo_volume"] / total if total else 0
        totals_rows.append({
            "date": date,
            "total_volume": round(total, 2),
            "straight_volume": round(parsed["straight_volume"], 2),
            "combo_volume": round(parsed["combo_volume"], 2),
            "combo_pct": round(combo_pct, 2),
        })
        for sport, vol in parsed["by_sport"].items():
            sport_rows.append({"date": date, "sport": sport, "volume": round(vol, 2)})
        for pfx, vol in parsed["unknown"].items():
            unknown_rows.append({"date": date, "prefix": pfx, "volume": round(vol, 2)})

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "straight_volume", "combo_volume", "combo_pct"],
                 totals_rows)
    vc.write_csv(BY_SPORT_CSV, ["date", "sport", "volume"], sport_rows)
    if unknown_rows:
        vc.write_csv(UNKNOWN_CSV, ["date", "prefix", "volume"], unknown_rows)
        print(f"\nNote: {len(unknown_rows)} unrecognised prefix rows -> {UNKNOWN_CSV}")

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")
    if totals_rows:
        last = totals_rows[-1]
        print(f"  latest {last['date']}: {last['total_volume']:,.0f} contracts "
              f"({last['combo_pct']:.0f}% combo)")


if __name__ == "__main__":
    main()
