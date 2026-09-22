"""
Polymarket US (QCX LLC d/b/a Polymarket US) daily sports volume + combo tracker.

Source: Polymarket US publishes a manifest of daily per-market CSV reports at
    https://polymarketexchange.com/files/daily-market-report/manifest.json
Each entry lists a filename like "20260828-daily-market-report.csv", fetched
directly at:
    https://polymarketexchange.com/files/daily-market-report/<filename>

IMPORTANT - this exchange is NOT sports-only (unlike DKeX/Novig). It also
lists politics, weather, macro/economic indicators, and culture/entertainment
markets. Sports rows are identified by a symbol-prefix allowlist built from
inspecting a real file (2026-08-28) - see SPORTS_PREFIXES below. Any symbol
prefix NOT in either the sports or non-sports list gets logged as "unknown"
so new product types don't silently get miscounted - check
polymarket_us_unknown_prefixes.csv periodically and update the lists below.

Combo identification: the "caoc" prefix = "Combinatoric Athletic Outcome
Contracts" - Polymarket US's combo/parlay product, confirmed via the
Description field in a real file. It's already sports by definition.
Unlike Novig/DKeX, this file has NO separate combo volume flag/column -
identification is entirely prefix-based.

Usage:
    pip install requests
    python polymarket_us_daily_volume.py
"""

import csv
import io
import os
import sys
import urllib.request
import urllib.error
import json

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_URL = "https://polymarketexchange.com/files/daily-market-report/manifest.json"
BASE_URL = "https://polymarketexchange.com/files/daily-market-report/"
RAW_CACHE_DIR = os.path.join(HERE, "polymarket_us_raw_cache")
TOTALS_CSV = os.path.join(HERE, "polymarket_us_daily_totals.csv")
UNKNOWN_PREFIXES_CSV = os.path.join(HERE, "polymarket_us_unknown_prefixes.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}

# Confirmed sports-related prefixes (from inspecting 2026-08-28 report plus a
# full-backfill audit of every prefix seen Oct 2025 - Aug 2026).
# "caoc" = Combinatoric Athletic Outcome Contracts = the combo product.
# NOTE: prefixes are matched case-insensitively (see parse_report). Early
# reports (~May 2026 and before) used UPPERCASE symbol prefixes
# (AEC-UFC, TEC-NBA, ASC-MLB, ATC-SOCCER, TSC-MLB) - lowercasing before the
# lookup recovers ~$360M of sports volume that was otherwise dropped on those
# ~8 days.
SPORTS_PREFIXES = {
    "aachc", "aadc", "aec", "arankc", "asc", "astatc", "atc",
    "caoc",  # combo product - always sports
    "fptc", "ftsc", "pntcbk", "pntcbb", "pntcf", "pntcs", "tac", "tec", "tsc",
    # added after backfill audit:
    "aqc",   # "advance/qualify" contracts - fifa/cfb/mlb/nfl/wnba/atp/cbb (~$15M)
    "adpc",  # athlete draft-pick contracts - mlb/nba/nhl drafts (~$0.6M)
    # added 2026-09-21 audit: e.g. "sdcnfl-nfl-atl-2026-09-13-qb-coorus" - an
    # NFL player-prop market. Trivial volume (a few thousand contracts/week)
    # but unambiguously sports.
    "sdcnfl",
    "aoc",  # e.g. "aoc-nfl-ne-sea-2026-09-09-htft-away-tie" - NFL halftime/
            # fulltime combo outcome markets. Also trivial volume.
}
COMBO_PREFIX = "caoc"

# Confirmed NON-sports prefixes (politics, macro/econ, weather, culture,
# companies, geopolitics) - excluded from the sports total.
NON_SPORTS_PREFIXES = {
    "aimc", "aimrc", "apdc", "cbpac", "ccpc", "ccrc", "cpc", "cpic",
    "cranc", "dccc", "dipcc", "enwc", "erac", "ewc", "gdpc", "iarc",
    "ipcc", "kpic", "lawec", "mlaec", "mowc", "nfpc", "nphc", "opdc",
    "paccc", "pandc", "pic", "pintc", "pnwpc", "prdc", "pvwc", "rdc",
    "tc", "tpoyc", "urc", "usgcc", "usgovcc", "usgubewc", "ushrewc",
    "ushsscc", "ussewc", "vmc", "vtc",
    # added after backfill audit:
    "rtc",   # reality TV - Big Brother US / Love Island US / AGT (~$2.8M)
    "stsc",  # geopolitics - Strait of Hormuz etc. (~$1.3M)
    "vsc",   # US gubernatorial (~$0.2M)
    "patc",  # celebrity-attendance novelty markets (~$0.05M)
    "scc", "gsc", "nocc",  # US House / govt-shutdown / misc politics (tiny)
    # added 2026-09-21 audit, all trivial volume (a few thousand contracts):
    "cpoc",     # US Senate/gubernatorial races, e.g. "cpoc-ussegubc-oh-..."
    "rotc",     # weather/temperature markets, e.g. "rotc-t-primet-...-gte80"
    "aaagaspc", # AAA gas price contracts, e.g. "...usgas-day-...-gt4pt30"
    "cmovcuss", # US Senate race closeness contracts
    "cmovcusg", # US gubernatorial race closeness contracts
    "usmayewc", # US mayoral election winner contracts
    "ncpdc",    # cabinet/administration personnel contracts, e.g. Trump cabinet
}
# Prefixes still not confidently classified - not counted either way, logged
# to polymarket_us_unknown_prefixes.csv for periodic review. (All the
# previously-ambiguous large prefixes were resolved in the backfill audit:
# aqc -> sports, rtc -> non-sports. The 2026-09-21 audit resolved 9 more small
# ones - all under ~$0.01M combined - by inspecting example symbols directly.)
AMBIGUOUS_PREFIXES: set = set()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def get_manifest() -> list[dict]:
    data = json.loads(fetch(MANIFEST_URL).decode("utf-8"))
    files = data.get("files", [])
    if not files:
        sys.exit("Manifest returned no files - check the URL/format hasn't changed.")
    return files


def filename_to_date(filename: str) -> str:
    # "20260828-daily-market-report.csv" -> "2026-08-28"
    digits = filename[:8]
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def cache_path_for_filename(filename: str) -> str:
    return os.path.join(RAW_CACHE_DIR, filename)


def get_cached_or_fetch(filename: str) -> str:
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(RAW_CACHE_DIR, filename)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()
    content = fetch(BASE_URL + filename).decode("utf-8")
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(content)
    return content


def _price_dollars(row: dict) -> float:
    """No trade-price columns are populated in these reports; use the bid/offer
    midpoint, falling back to settlement price."""
    def num(k):
        v = (row.get(k) or "").strip()
        try:
            return float(v)
        except ValueError:
            return None
    hb, lo = num("High Bid Price"), num("Low Offer Price")
    lb, ho = num("Low Bid Price"), num("High Offer Price")
    bid = hb if hb is not None else lb
    off = lo if lo is not None else ho
    if bid is not None and off is not None and 0 <= bid <= 1 and 0 <= off <= 1:
        return (bid + off) / 2
    if bid is not None and 0 <= bid <= 1:
        return bid
    s = num("Settlement Price")
    return s if s is not None and 0 <= s <= 1 else 0.5


def parse_report(text: str) -> dict:
    """Returns sports_total, combo_total, sports_notional, and any unknown
    prefixes seen with their row counts (for logging, not included in totals)."""
    reader = csv.DictReader(io.StringIO(text))
    sports_total = 0.0
    combo_total = 0.0
    sports_notional = 0.0
    all_total = 0.0          # every market on QCX, all categories
    all_notional = 0.0
    unknown_prefixes: dict = {}

    for row in reader:
        symbol = row.get("Symbol", "")
        # Match case-insensitively: early reports used UPPERCASE prefixes
        # (AEC-UFC, TEC-NBA, ...), later ones lowercase.
        prefix = (symbol.split("-")[0] if "-" in symbol else symbol).lower()
        vol_str = row.get("Trade Volume", "")
        vol = float(vol_str) if vol_str else 0.0

        all_total += vol
        if vol:
            all_notional += vol * _price_dollars(row)

        if prefix in SPORTS_PREFIXES:
            sports_total += vol
            if vol:
                sports_notional += vol * _price_dollars(row)
            if prefix == COMBO_PREFIX:
                combo_total += vol
        elif prefix in NON_SPORTS_PREFIXES:
            pass  # excluded from sports total, expected
        else:
            # unrecognized prefix - log it, don't count it either way so
            # totals don't silently drift
            unknown_prefixes[prefix] = unknown_prefixes.get(prefix, 0) + 1

    return {
        "sports_total": sports_total,
        "combo_total": combo_total,
        "sports_notional": sports_notional,
        "all_total": all_total,
        "all_notional": all_notional,
        "unknown_prefixes": unknown_prefixes,
    }


def main():
    print("Fetching Polymarket US manifest...")
    files = get_manifest()

    file_by_date_map: dict[str, list[str]] = {}
    for f in files:
        d = filename_to_date(f["filename"])
        file_by_date_map.setdefault(d, []).append(f["filename"])

    vc.drop_stale_cache(
        lambda d: [cache_path_for_filename(fn) for fn in file_by_date_map.get(d, [])]
    )

    target_dates = [d for d in vc.daterange() if d in file_by_date_map]
    missing = [d for d in vc.daterange() if d not in file_by_date_map]
    if missing:
        print(f"Note: Polymarket US manifest has no report for {len(missing)} "
              f"date(s) in range ({missing[0]}..{missing[-1]}) - skipped.")

    totals_rows = []
    unknown_rows = []

    for date in target_dates:
        filename = sorted(file_by_date_map[date])[-1]  # latest if dupes
        print(f"  Processing {date} ({filename})...")

        try:
            text = get_cached_or_fetch(filename)
            result = parse_report(text)
        except Exception as e:
            print(f"    FAILED ({e}) - skipping this run")
            continue

        total = result["sports_total"]
        combo = result["combo_total"]
        combo_pct = round(100 * combo / total, 2) if total > 0 else 0.0

        totals_rows.append({
            "date": date,
            "total_volume": round(total, 2),
            "combo_volume": round(combo, 2),
            "combo_pct": combo_pct,
            "notional_est": round(result["sports_notional"], 2),
            "all_volume": round(result["all_total"], 2),
            "all_notional_est": round(result["all_notional"], 2),
        })
        for prefix, count in result["unknown_prefixes"].items():
            unknown_rows.append({"date": date, "prefix": prefix, "row_count": count})

        print(f"    sports volume = {total:,.0f}  |  combo = {combo:,.0f} ({combo_pct}%)"
              f"  |  notional est = {result['sports_notional']:,.0f}")

    vc.write_csv(TOTALS_CSV,
                 ["date", "total_volume", "combo_volume", "combo_pct", "notional_est",
                  "all_volume", "all_notional_est"],
                 totals_rows)
    vc.write_csv(UNKNOWN_PREFIXES_CSV,
                 ["date", "prefix", "row_count"], unknown_rows)
    if unknown_rows:
        seen = sorted({r["prefix"] for r in unknown_rows})
        print(f"\nNote: unclassified prefixes still seen: {seen}\n"
              f"  logged to {UNKNOWN_PREFIXES_CSV} - classify in SPORTS_PREFIXES "
              f"or NON_SPORTS_PREFIXES if their volume grows.")

    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")


if __name__ == "__main__":
    main()
