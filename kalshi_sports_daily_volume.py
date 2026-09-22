"""
Kalshi sports-only daily volume puller (v5 - per-active-series candlesticks).

Kalshi's market-data endpoints are public, no API key required.
Base: https://external-api.kalshi.com/trade-api/v2

Why this shape
--------------
Kalshi is huge: ~3,600 sports series, 100k+ traded sports markets, millions of
trades/day. A global /markets sweep, a full per-series crawl, and the raw trade
feed are all too slow for a daily job. What *is* fast:

  1. GET /events (open + settled-since-START_DATE) -> the few hundred sports
     series that actually have activity in our window. Cached 7 days.
  2. GET /markets?series_ticker=X&min_close_ts=START_DATE for each active series
     -> the markets to price. Keep those with lifetime volume over
     MIN_MARKET_VOLUME (tiny markets are <2% of volume and 10x the market count).
  3. Batch GET /markets/candlesticks (100/call, period_interval=1440) over
     [START_DATE, now]; bucket each daily candle's volume_fp by ET date.

  contracts    = sum(volume_fp)                  (one-sided contract count)
  notional_est = sum(volume_fp * candle close)   (~USD that changed hands)

See VOLUME_METHODOLOGY.md.

First run ~15 min; daily run ~2-3 min (series list cached, only recent candles
refetched). Kalshi has no sports-only combo product, so no combo columns.

Usage:
    python kalshi_sports_daily_volume.py
    python kalshi_sports_daily_volume.py --refresh-series   # force step 1
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import volume_common as vc

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
TOTALS_CSV = os.path.join(HERE, "kalshi_sports_daily_totals.csv")
BY_SERIES_CSV = os.path.join(HERE, "kalshi_sports_daily_by_series.csv")
SERIES_CACHE_JSON = os.path.join(HERE, "kalshi_sports_series_cache.json")
SERIES_CACHE_BUILT = os.path.join(HERE, "kalshi_sports_series_cache.built")
ACTIVE_CACHE_JSON = os.path.join(HERE, "kalshi_active_series.json")
ACTIVE_CACHE_BUILT = os.path.join(HERE, "kalshi_active_series.built")
MARKET_CACHE_JSON = os.path.join(HERE, "kalshi_market_cache.json")

ET_OFFSET = timedelta(hours=-4)  # EDT, valid for the Aug-Sep window
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}
DELAY = 0.15
SERIES_CACHE_MAX_AGE_DAYS = 7
ACTIVE_CACHE_MAX_AGE_DAYS = 7
MARKET_CACHE_MAX_AGE_DAYS = 3


def _age_days(date_str) -> float:
    """Days since `date_str` (YYYY-MM-DD). NOT file-mtime-based: both
    kalshi_sports_series_cache.json and kalshi_active_series.json are committed
    to git, and a fresh `git checkout` (every CI run) resets mtimes to "now" -
    an mtime check would look permanently fresh and never actually refresh.
    That was a real bug here: the series/active caches were frozen at whatever
    they were on first commit, silently missing any series that started
    trading afterward (new weekly matchup series, a new sport added, etc.)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 999
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400


def _read_built(marker_path: str) -> str | None:
    return open(marker_path, encoding="utf-8").read().strip() if os.path.exists(marker_path) else None


def _write_built(marker_path: str) -> None:
    open(marker_path, "w", encoding="utf-8").write(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
# Skip markets below this lifetime contract volume. They are the bulk of the
# market *count* (Kalshi lists thousands of micro player-props) but a low-single-
# digit % of traded volume. Lower it for more completeness at the cost of a
# longer run.
MIN_MARKET_VOLUME = 5000


def api_get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    backoff = 2.0
    for attempt in range(7):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=HEADERS), timeout=45
            ) as resp:
                time.sleep(DELAY)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 6:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise SystemExit(f"Kalshi API {e.code} on {url}: {e.read().decode('utf-8')[:300]}")
        except (urllib.error.URLError, TimeoutError, OSError):
            # OSError also catches ConnectionResetError/ConnectionAbortedError,
            # which surface here (not as URLError) when the connection drops
            # mid-read rather than at connect time.
            if attempt < 6:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise
    raise SystemExit(f"Kalshi API: retries exhausted on {url}")


def _paginate(path: str, params: dict, key: str):
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = api_get(path, p)
        items = data.get(key, [])
        yield from items
        cursor = data.get("cursor")
        if not cursor or not items:
            break


def sports_category_series() -> set[str]:
    if os.path.exists(SERIES_CACHE_JSON):
        built = _read_built(SERIES_CACHE_BUILT)
        # No .built marker (pre-fix cache) is treated as stale, not fresh -
        # forces one rebuild to start the recorded-date trail.
        if built is not None and _age_days(built) < SERIES_CACHE_MAX_AGE_DAYS:
            with open(SERIES_CACHE_JSON, "r", encoding="utf-8") as f:
                return {s["ticker"] for s in json.load(f)}
    print("Discovering sports-category series (cached after this)...")
    out = [{"ticker": s["ticker"], "title": s.get("title", "")}
           for s in _paginate("/series", {"category": "Sports", "limit": 1000}, "series")]
    if not out:
        raise SystemExit("No series for category=Sports.")
    with open(SERIES_CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    _write_built(SERIES_CACHE_BUILT)
    return {s["ticker"] for s in out}


def active_sports_series(force: bool = False) -> list[str]:
    if not force and os.path.exists(ACTIVE_CACHE_JSON):
        built = _read_built(ACTIVE_CACHE_BUILT)
        if built is not None and _age_days(built) < ACTIVE_CACHE_MAX_AGE_DAYS:
            with open(ACTIVE_CACHE_JSON, "r", encoding="utf-8") as f:
                return json.load(f)

    cat = sports_category_series()
    start_ts = int((datetime.strptime(vc.START_DATE, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc) - ET_OFFSET).timestamp())
    active: set[str] = set()
    # Open events are few; settled/closed are many but the *set* of series
    # saturates fast, so stop a status once 40 straight pages add nothing new.
    for status in ("open", "settled", "closed"):
        n = 0
        pages_since_new = 0
        prev = len(active)
        for ev in _paginate("/events",
                            {"limit": 200, "status": status, "min_close_ts": start_ts},
                            "events"):
            n += 1
            st = ev.get("series_ticker") or ev.get("ticker", "").split("-", 1)[0]
            if st in cat:
                active.add(st)
            if n % 200 == 0:
                pages_since_new = 0 if len(active) > prev else pages_since_new + 1
                prev = len(active)
                if pages_since_new >= 40:
                    break
        print(f"  /events status={status}: ~{n} events -> {len(active)} active series so far")
    result = sorted(active)
    with open(ACTIVE_CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    _write_built(ACTIVE_CACHE_BUILT)
    return result


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def discover_markets(series_list: list[str], start_ts: int) -> list[str]:
    """Enumerate markets to price, cached for MARKET_CACHE_MAX_AGE_DAYS so the
    daily run skips this ~8-min step most days."""
    if os.path.exists(MARKET_CACHE_JSON):
        age = (time.time() - os.path.getmtime(MARKET_CACHE_JSON)) / 86400
        try:
            cached = json.load(open(MARKET_CACHE_JSON, encoding="utf-8"))
        except Exception:
            cached = {}
        if (age < MARKET_CACHE_MAX_AGE_DAYS
                and cached.get("start_date") == vc.START_DATE
                and cached.get("min_volume") == MIN_MARKET_VOLUME):
            print(f"  using cached market list ({len(cached['tickers'])} markets, "
                  f"{age * 24:.0f}h old)")
            return cached["tickers"]

    tickers = _discover_markets_live(series_list, start_ts)
    with open(MARKET_CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump({"start_date": vc.START_DATE, "min_volume": MIN_MARKET_VOLUME,
                   "tickers": tickers}, f)
    return tickers


def _discover_markets_live(series_list: list[str], start_ts: int) -> list[str]:
    tickers = []
    for i, s in enumerate(series_list, 1):
        for m in _paginate("/markets",
                           {"series_ticker": s, "limit": 1000, "min_close_ts": start_ts},
                           "markets"):
            try:
                if float(m.get("volume_fp") or m.get("volume") or 0) >= MIN_MARKET_VOLUME:
                    tickers.append(m["ticker"])
            except (TypeError, ValueError):
                pass
        if i % 100 == 0:
            print(f"  discovered {i}/{len(series_list)} series -> {len(tickers)} markets", end="\r")
    print(f"  discovered {len(series_list)} series -> {len(tickers)} markets to price      ")
    return tickers


def _dollars(v) -> float:
    if v in (None, ""):
        return 0.0
    v = float(v)
    return v / 100.0 if v > 1.5 else v


def _candle_price(c: dict) -> float:
    """Volume-weighted average trade price for the period. Use `mean` (the
    period VWAP) - NOT `close`, which on a settlement day is the 0/1 outcome
    price and badly distorts notional (verified: mean-weighted notional matches
    the exact trade-by-trade figure to <0.1%)."""
    price = c.get("price") or {}
    for k in ("mean_dollars", "mean", "close_dollars", "close", "open_dollars", "open"):
        if price.get(k) not in (None, ""):
            return _dollars(price[k])
    b = (c.get("yes_bid") or {}); a = (c.get("yes_ask") or {})
    bb = b.get("mean_dollars") or b.get("close_dollars") or b.get("close")
    aa = a.get("mean_dollars") or a.get("close_dollars") or a.get("close")
    if bb is not None and aa is not None:
        return (_dollars(bb) + _dollars(aa)) / 2
    return 0.5


def et_date_of(end_period_ts: int) -> str:
    dt_et = datetime.fromtimestamp(end_period_ts, tz=timezone.utc) + ET_OFFSET
    return (dt_et - timedelta(seconds=1)).strftime("%Y-%m-%d")


def collect(tickers: list[str], start_ts: int, end_ts: int):
    totals, notional, by_series = {}, {}, {}
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        data = api_get("/markets/candlesticks", {
            "market_tickers": ",".join(batch),
            "period_interval": 1440,
            "start_ts": start_ts,
            "end_ts": end_ts,
        })
        for market in data.get("markets", []):
            ser = series_of(market.get("market_ticker", ""))
            for c in market.get("candlesticks", []):
                vol = float(c.get("volume_fp") or c.get("volume") or 0)
                if vol <= 0:
                    continue
                d = et_date_of(c["end_period_ts"])
                if not vc.in_range(d):
                    continue
                totals[d] = totals.get(d, 0.0) + vol
                notional[d] = notional.get(d, 0.0) + vol * _candle_price(c)
                by_series[(d, ser)] = by_series.get((d, ser), 0.0) + vol
        print(f"  candlesticks {min(i + 100, len(tickers))}/{len(tickers)}", end="\r")
    print()
    return totals, notional, by_series


def main():
    force_series = "--refresh-series" in sys.argv

    start_ts = int((datetime.strptime(vc.START_DATE, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc) - ET_OFFSET).timestamp())
    end_ts = int((datetime.strptime(vc.yesterday(), "%Y-%m-%d")
                  .replace(tzinfo=timezone.utc) - ET_OFFSET + timedelta(days=1)).timestamp())

    series_list = active_sports_series(force=force_series)
    print(f"{len(series_list)} active sports series.")
    tickers = discover_markets(series_list, start_ts)
    if not tickers:
        print("No markets to price.")
        return

    print(f"Fetching daily candlesticks for {len(tickers)} markets...")
    totals, notional, by_series = collect(tickers, start_ts, end_ts)

    totals_rows = [
        {"date": d,
         "kalshi_sports_volume": round(totals[d], 2),
         "kalshi_sports_notional_est": round(notional.get(d, 0.0), 2)}
        for d in sorted(totals)
    ]
    series_rows = [
        {"date": d, "series": s, "volume": round(v, 2)}
        for (d, s), v in sorted(by_series.items())
    ]
    for r in totals_rows:
        print(f"  {r['date']}: contracts = {r['kalshi_sports_volume']:,.0f}  "
              f"|  notional est = {r['kalshi_sports_notional_est']:,.0f}")

    vc.write_csv(TOTALS_CSV,
                 ["date", "kalshi_sports_volume", "kalshi_sports_notional_est"],
                 totals_rows)
    vc.write_csv(BY_SERIES_CSV, ["date", "series", "volume"], series_rows)
    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")


if __name__ == "__main__":
    main()
