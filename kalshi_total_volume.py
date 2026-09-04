"""
Kalshi WHOLE-EXCHANGE daily volume (all categories) + category mix.

Kalshi does not publish a per-day exchange total, and reconstructing one over
the public API is impractical (~5-6M trades/day; the parlay engine and the
15-minute crypto series each have 40k+ markets, and enumerating ~4,800 series
one at a time takes hours against the rate limit).

So the daily total is assembled from two Kalshi-derived public sources:

  1. HISTORY  <- DeFi Rate's Kalshi volume table
     (defirate.com/prediction-markets/volume/kalshi). Built from Kalshi's own
     API; their basis is "contracts x $1 face" - the same contract-count basis
     as every other column here - and their monthly sum reconciles with Kalshi's
     own reported figure ("$33.7B so far in August"). It lags ~2 days.

  2. THE LAST ~2-3 DAYS DeFi Rate is missing  <- projected from our own
     verified Kalshi *sports* number (kalshi_sports_daily_volume.py, checked
     market-by-market against the trade feed) divided by the sports share of
     the total observed over the most recent days DeFi Rate does have. These
     rows are marked `projected` in the CSV.

  CATEGORY MIX + avg price  <- our own trade-feed sample (see sample_mix).
  total_notional_est = total_contracts x that avg price.

Outputs:
  kalshi_daily_total.csv       date, total_contracts, total_notional_est, source
  kalshi_daily_by_category.csv date, group, contracts, notional_est - the six
                               GROUP_ORDER rows sum to the daily total; an extra
                               "Sports parlay" row is a cross-cut (its contracts
                               already sit inside "Combos & exotics")
  kalshi_exchange_mix.json     sampled split + avg price + sports_parlay (dashboard)

Runtime ~10-15 min (the trade sample; skipped when < 7 days old).

Usage:
    python kalshi_total_volume.py
    python kalshi_total_volume.py --resample
"""

import collections
import csv as csvmod
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import volume_common as vc
import kalshi_sports_daily_volume as ks

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_JSON = os.path.join(HERE, "kalshi_series_catalog.json")
SPORTS_CSV = os.path.join(HERE, "kalshi_sports_daily_totals.csv")
TOTAL_CSV = os.path.join(HERE, "kalshi_daily_total.csv")
BY_CAT_CSV = os.path.join(HERE, "kalshi_daily_by_category.csv")
MIX_JSON = os.path.join(HERE, "kalshi_exchange_mix.json")

DEFIRATE_URL = "https://defirate.com/prediction-markets/volume/kalshi/"
GROUPS = {
    "Sports": "Sports", "Exotics": "Combos & exotics", "Crypto": "Crypto",
    "Politics": "Politics & elections", "Elections": "Politics & elections",
    "Economics": "Economics & financials", "Financials": "Economics & financials",
    "Commodities": "Economics & financials", "Companies": "Economics & financials",
}
GROUP_ORDER = ["Combos & exotics", "Sports", "Crypto",
               "Politics & elections", "Economics & financials", "Other"]


# ---------- DeFi Rate history ----------

def _to_num(s: str):
    s = s.replace("$", "").replace(",", "").strip()
    if not s:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(s[-1:], 1)
    try:
        return float(s[:-1] if s[-1] in "KMB" else s) * mult
    except ValueError:
        return None


DEFIRATE_CACHE = os.path.join(HERE, "defirate_cache.json")


def fetch_defirate() -> dict:
    """DeFi Rate's Kalshi daily volume table, merged into a committed cache so a
    failed/blocked fetch (e.g. Cloudflare from a datacenter IP) still leaves
    history to work from."""
    cache = {}
    if os.path.exists(DEFIRATE_CACHE):
        try:
            cache = json.load(open(DEFIRATE_CACHE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(DEFIRATE_URL, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30).read().decode("utf-8", "replace")
        rows = re.findall(
            r'<td class="tide-chart__cell-label">(\d{4}-\d\d-\d\d)</td><td>[^<]*</td><td>([^<]*)</td>',
            html)
        fresh = {d: _to_num(v) for d, v in rows if _to_num(v)}
        if len(fresh) >= 5:
            cache.update(fresh)
            json.dump(cache, open(DEFIRATE_CACHE, "w", encoding="utf-8"),
                      indent=0, sort_keys=True)
        else:
            print("  DeFi Rate: fetch returned too few rows - using cached history.")
    except Exception as e:  # noqa: BLE001
        print(f"  DeFi Rate: fetch failed ({e}) - using cached history.")

    out = {d: v for d, v in cache.items() if v and vc.in_range(d)}
    if not out:
        raise SystemExit("DeFi Rate: no data and no cache - cannot build Kalshi totals.")
    return out


def load_sports() -> dict:
    if not os.path.exists(SPORTS_CSV):
        return {}
    with open(SPORTS_CSV, newline="", encoding="utf-8") as f:
        return {r["date"]: float(r["kalshi_sports_volume"])
                for r in csvmod.DictReader(f) if r.get("kalshi_sports_volume")}


# ---------- category mix from a trade-feed sample ----------

def build_catalog() -> dict:
    if os.path.exists(CATALOG_JSON) and (time.time() - os.path.getmtime(CATALOG_JSON)) / 86400 < 7:
        return json.load(open(CATALOG_JSON, encoding="utf-8"))
    print("Enumerating the Kalshi series catalog...")
    cat = {s["ticker"]: s.get("category", "?")
           for s in ks._paginate("/series", {"limit": 1000}, "series")}
    json.dump(cat, open(CATALOG_JSON, "w", encoding="utf-8"))
    return cat


def group_of(ticker: str, cat: dict) -> str:
    s = ks.series_of(ticker)
    if "COMBO" in s.upper():
        return "Combos & exotics"
    return GROUPS.get(cat.get(s, "?"), "Other")


# --- Kalshi parlay identification --------------------------------------------
# Kalshi's parlay volume is its multi-event ("MVE") engine + any *COMBO series.
# The dominant product is KXMVECROSSCATEGORY - despite the "cross-category" name,
# a leg-composition spot-check (25 traded parlays, 152 legs) found them 100%
# sports legs; parlay bettors build sports parlays. So "Combos & exotics" from
# group_of() is effectively Kalshi's sports-parlay figure (a few % of it is
# entertainment MVE - Grammys / Oscars - and non-sports *COMBO, immaterial).
# `is_parlay` is exposed for the sample so the mix JSON can report the slice
# explicitly as a cross-check.
_MVE_ENTERTAINMENT = ("KXMVEGRAMMYS", "KXMVEOSCARS", "KXMVEMENTION", "KXMVEEMMY")


def is_parlay(ticker: str, cat: dict) -> bool:
    s = ks.series_of(ticker).upper()
    if any(s.startswith(e) for e in _MVE_ENTERTAINMENT):
        return False
    return s.startswith("KXMVE") or "COMBO" in s or "PREPACK" in s or "SGP" in s


def sample_mix(cat: dict) -> dict:
    y = datetime.now(timezone.utc) - timedelta(days=1)
    days = [(y - timedelta(days=1)).strftime("%Y-%m-%d"), y.strftime("%Y-%m-%d")]
    hours = [3, 10, 16, 21]            # overnight / morning / afternoon / evening ET
    by_c, by_n, n = collections.Counter(), collections.Counter(), 0
    parlay_c = parlay_n = 0.0        # parlay slice (cross-cut; sits inside Combos & exotics)
    for d in days:
        mid = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) - ks.ET_OFFSET
        for h in hours:
            lo = int((mid + timedelta(hours=h)).timestamp())
            hi, cursor = lo + 3600, None
            while True:
                p = {"limit": 1000, "min_ts": lo, "max_ts": hi}
                if cursor:
                    p["cursor"] = cursor
                r = ks.api_get("/markets/trades", p)
                ts = r.get("trades", [])
                n += len(ts)
                for t in ts:
                    try:
                        c = float(t.get("count_fp") or 0)
                        px = float(t.get("yes_price_dollars") or 0)
                    except (TypeError, ValueError):
                        continue
                    tk = t.get("ticker", "")
                    g = group_of(tk, cat)
                    by_c[g] += c
                    by_n[g] += c * px
                    if is_parlay(tk, cat):
                        parlay_c += c
                        parlay_n += c * px
                cursor = r.get("cursor")
                if not cursor or not ts:
                    break
            print(f"  sampled {d} {h:02d}:00 ET", flush=True)
    tc, tn = sum(by_c.values()) or 1, sum(by_n.values()) or 1
    sports_c = by_c.get("Sports", 0.0)
    sports_n = by_n.get("Sports", 0.0)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sampled_trades": n,
        "avg_price": round(tn / tc, 4),
        "groups": [{"name": g,
                    "contracts_pct": round(100 * by_c.get(g, 0) / tc, 2),
                    "notional_pct": round(100 * by_n.get(g, 0) / tn, 2)} for g in GROUP_ORDER],
        # Parlay slice, measured directly off the sampled trades (a cross-cut -
        # its contracts already sit in "Combos & exotics"). Shown as a share of
        # the whole exchange and of Kalshi's own sports trading (single-leg
        # "Sports" + parlay), the ratio the other venues' combo % uses.
        "parlay": {
            "contracts_pct": round(100 * parlay_c / tc, 3),
            "notional_pct": round(100 * parlay_n / tn, 3),
            "pct_of_sports_contracts": round(100 * parlay_c / (sports_c + parlay_c), 2)
            if (sports_c + parlay_c) else 0.0,
            "pct_of_sports_notional": round(100 * parlay_n / (sports_n + parlay_n), 2)
            if (sports_n + parlay_n) else 0.0,
        },
    }


def main():
    print("Fetching Kalshi daily totals (DeFi Rate)...")
    dr = fetch_defirate()
    sports = load_sports()
    dr_days = sorted(dr)
    print(f"  DeFi Rate: {dr_days[0]}..{dr_days[-1]} ({len(dr_days)} days)")

    # sports share of the total over the most recent ~7 DeFi Rate days
    ratios = [sports[d] / dr[d] for d in dr_days[-7:] if d in sports and dr[d]]
    share = sum(ratios) / len(ratios) if ratios else 0.33
    print(f"  sports is ~{share * 100:.0f}% of the total (recent) -> projecting missing days from it")

    fresh = (os.path.exists(MIX_JSON)
             and (time.time() - os.path.getmtime(MIX_JSON)) / 86400 < 7
             and "--resample" not in sys.argv)
    if fresh:
        mix = json.load(open(MIX_JSON, encoding="utf-8"))
        print(f"  cached category mix ({mix.get('sampled_trades', 0):,} trades).")
    else:
        print("Sampling the trade feed for the category mix...")
        mix = sample_mix(build_catalog())
        json.dump(mix, open(MIX_JSON, "w", encoding="utf-8"), indent=1)
    avg_price = mix["avg_price"]

    rows = []
    for d in vc.daterange():
        if d in dr:
            c, src = dr[d], "defirate"
        elif d in sports and share:
            c, src = sports[d] / share, "projected"
        else:
            continue
        rows.append({"date": d, "total_contracts": round(c, 2),
                     "total_notional_est": round(c * avg_price, 2), "source": src})
    vc.write_csv(TOTAL_CSV, ["date", "total_contracts", "total_notional_est", "source"], rows)

    cat_c = {g["name"]: g["contracts_pct"] / 100 for g in mix["groups"]}
    cat_n = {g["name"]: g["notional_pct"] / 100 for g in mix["groups"]}
    # "Parlay" is a cross-cut of the groups (its contracts sit inside "Combos &
    # exotics"), appended as an extra row - not in GROUP_ORDER, so the group
    # rows still sum to the daily total.
    pl = mix.get("parlay") or {}
    extra = [("Parlay", pl.get("contracts_pct", 0) / 100, pl.get("notional_pct", 0) / 100)]
    cat_rows = [{"date": r["date"], "group": g,
                 "contracts": round(r["total_contracts"] * cat_c.get(g, 0), 2),
                 "notional_est": round(r["total_notional_est"] * cat_n.get(g, 0), 2)}
                for r in rows for g in GROUP_ORDER]
    cat_rows += [{"date": r["date"], "group": g,
                  "contracts": round(r["total_contracts"] * gc, 2),
                  "notional_est": round(r["total_notional_est"] * gn, 2)}
                 for r in rows for g, gc, gn in extra]
    vc.write_csv(BY_CAT_CSV, ["date", "group", "contracts", "notional_est"], cat_rows)

    print(f"\n{mix['sampled_trades']:,} trades sampled | avg price ${avg_price}")
    for g in mix["groups"]:
        print(f"  {g['name']:24}{g['contracts_pct']:>7.1f}% contracts{g['notional_pct']:>8.1f}% $")
    if pl:
        print(f"  {'Parlay (of total)':24}{pl['contracts_pct']:>7.2f}% contracts"
              f"{pl['notional_pct']:>8.2f}% $   "
              f"[{pl.get('pct_of_sports_contracts', 0):.0f}% of Kalshi sports by contracts]")
    proj = [r["date"] for r in rows if r["source"] == "projected"]
    print(f"\n{len(rows)} day(s) -> {TOTAL_CSV}" + (f"  (projected: {proj})" if proj else ""))


if __name__ == "__main__":
    main()
