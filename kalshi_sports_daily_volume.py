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

Daily runs are incremental (trailing days plus any gap); --full rebuilds. Kalshi has no sports-only combo product, so no combo columns.

Usage:
    python kalshi_sports_daily_volume.py
    python kalshi_sports_daily_volume.py --full             # rebuild from START_DATE
    python kalshi_sports_daily_volume.py --refresh-series   # --full + rediscover series
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge data pull)"}
DELAY = 0.15
SERIES_CACHE_MAX_AGE_DAYS = 7
ACTIVE_CACHE_MAX_AGE_DAYS = 7
# Daily runs re-pull only this many trailing days (plus any gap since the last
# committed date) and keep older days from the committed CSVs. --full rebuilds.
INCREMENTAL_RECHECK_DAYS = 3
# Concurrent API requests; 429s are retried with backoff in api_get().
WORKERS = int(os.environ.get("KALSHI_WORKERS", "4"))
# The batch candlestick endpoint rejects >10,000 candles per request (HTTP 400).
# 100 markets x 100+ days broke every run from 2026-11-09, so long ranges are
# fetched in chunks of at most this many days.
MAX_DAYS_PER_REQUEST = 60


# --- US Eastern time, DST-aware (no tzdata dependency) --------------------
# Kalshi's trading day is an ET calendar day. US DST: 2nd Sunday of March 02:00
# to 1st Sunday of November 02:00 local.

def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7, weeks=n - 1)


def et_offset_at(dt_utc: datetime) -> timedelta:
    """ET offset from UTC at a naive-UTC instant."""
    start = _nth_sunday(dt_utc.year, 3, 2) + timedelta(hours=7)    # 02:00 EST
    end = _nth_sunday(dt_utc.year, 11, 1) + timedelta(hours=6)     # 02:00 EDT
    return timedelta(hours=-4) if start <= dt_utc < end else timedelta(hours=-5)


def et_midnight_ts(date_str: str) -> int:
    """UTC epoch seconds of 00:00 Eastern on `date_str`."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for off in (timedelta(hours=-4), timedelta(hours=-5)):
        utc = d - off
        if et_offset_at(utc) == off:
            return int(utc.replace(tzinfo=timezone.utc).timestamp())
    raise ValueError(date_str)             # unreachable: transitions are at 02:00


def _shift(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


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


def active_sports_series(start_ts: int, full: bool = False, force: bool = False) -> list[str]:
    """Sports series with an event closing at/after `start_ts`. Only a full run
    uses the committed cache; an incremental run scans just its short window."""
    if full and not force and os.path.exists(ACTIVE_CACHE_JSON):
        built = _read_built(ACTIVE_CACHE_BUILT)
        if built is not None and _age_days(built) < ACTIVE_CACHE_MAX_AGE_DAYS:
            with open(ACTIVE_CACHE_JSON, "r", encoding="utf-8") as f:
                return json.load(f)

    cat = sports_category_series()
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
    if full:
        with open(ACTIVE_CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        _write_built(ACTIVE_CACHE_BUILT)
    return result


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def _series_markets(series: str, start_ts: int) -> list[str]:
    out = []
    for m in _paginate("/markets",
                       {"series_ticker": series, "limit": 1000, "min_close_ts": start_ts},
                       "markets"):
        try:
            if float(m.get("volume_fp") or m.get("volume") or 0) >= MIN_MARKET_VOLUME:
                out.append(m["ticker"])
        except (TypeError, ValueError):
            pass
    return out


def discover_markets(series_list: list[str], start_ts: int) -> list[str]:
    tickers: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, got in enumerate(ex.map(lambda s: _series_markets(s, start_ts), series_list), 1):
            tickers.extend(got)
            if i % 200 == 0:
                print(f"  discovered {i}/{len(series_list)} series -> {len(tickers)} markets", flush=True)
    print(f"  discovered {len(series_list)} series -> {len(tickers)} markets to price", flush=True)
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
    """ET date of a candle; it covers the interval just BEFORE end_period_ts."""
    dt = datetime.fromtimestamp(end_period_ts - 1, tz=timezone.utc).replace(tzinfo=None)
    return (dt + et_offset_at(dt)).strftime("%Y-%m-%d")


def _segments(first_date: str, last_date: str) -> list[tuple[str, str, str]]:
    """Split [first_date, last_date] into requests. Daily candles are fixed 24h
    blocks from start_ts, which only line up with ET midnight while the UTC
    offset is constant, so a DST-change day (23h/25h) is fetched hourly. Daily
    runs are capped at MAX_DAYS_PER_REQUEST."""
    out: list[tuple[str, str, str]] = []
    run: list[str] = []

    def flush():
        for i in range(0, len(run), MAX_DAYS_PER_REQUEST):
            part = run[i:i + MAX_DAYS_PER_REQUEST]
            out.append(("daily", part[0], part[-1]))
        run.clear()

    d = first_date
    while d <= last_date:
        nxt = _shift(d, 1)
        if et_midnight_ts(nxt) - et_midnight_ts(d) != 86400:
            flush()
            out.append(("hourly", d, d))
        else:
            run.append(d)
        d = nxt
    flush()
    return out


def collect(tickers: list[str], first_date: str, last_date: str):
    totals, notional, by_series = {}, {}, {}
    for kind, a, b in _segments(first_date, last_date):
        interval = 1440 if kind == "daily" else 60
        s_ts, e_ts = et_midnight_ts(a), et_midnight_ts(_shift(b, 1))
        per_market = (e_ts - s_ts) // (interval * 60) + 1     # +1: inclusive start
        size = max(1, min(100, 10000 // per_market))
        batches = [tickers[i:i + size] for i in range(0, len(tickers), size)]

        def fetch(batch):
            return api_get("/markets/candlesticks", {
                "market_tickers": ",".join(batch), "period_interval": interval,
                "start_ts": s_ts, "end_ts": e_ts}).get("markets", [])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for n, markets in enumerate(ex.map(fetch, batches), 1):
                for market in markets:
                    ser = series_of(market.get("market_ticker", ""))
                    for c in market.get("candlesticks", []):
                        vol = float(c.get("volume_fp") or c.get("volume") or 0)
                        if vol <= 0:
                            continue
                        d = et_date_of(c["end_period_ts"])
                        # The API also returns the candle ending AT start_ts (the
                        # previous day, only partly covered) - drop anything
                        # outside this segment.
                        if not (a <= d <= b) or not vc.in_range(d):
                            continue
                        totals[d] = totals.get(d, 0.0) + vol
                        notional[d] = notional.get(d, 0.0) + vol * _candle_price(c)
                        by_series[(d, ser)] = by_series.get((d, ser), 0.0) + vol
                if n % 100 == 0 or n == len(batches):
                    print(f"  candlesticks [{kind} {a}..{b}] batch {n}/{len(batches)}", flush=True)
    return totals, notional, by_series


def _read_csv_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    force_series = "--refresh-series" in sys.argv
    full = "--full" in sys.argv or force_series
    last_date = vc.yesterday()

    old_totals = _read_csv_rows(TOTALS_CSV)
    old_series = _read_csv_rows(BY_SERIES_CSV)
    first_old = min((r["date"] for r in old_totals), default=None)
    if not full and (not old_totals or not old_series or first_old > vc.START_DATE):
        print("No usable history on disk - doing a full rebuild.")
        full = True

    if full:
        win_start = vc.START_DATE
    else:
        newest = max(r["date"] for r in old_totals)
        win_start = max(vc.START_DATE, _shift(newest, -INCREMENTAL_RECHECK_DAYS))
    win_start = min(win_start, last_date)
    print(f"{'FULL' if full else 'Incremental'} run: ET dates {win_start}..{last_date}", flush=True)

    start_ts = et_midnight_ts(win_start)
    series_list = active_sports_series(start_ts, full=full, force=force_series)
    print(f"{len(series_list)} active sports series.", flush=True)
    tickers = discover_markets(series_list, start_ts)
    if not tickers:
        raise SystemExit("No markets to price - leaving existing CSVs untouched.")

    print(f"Fetching candlesticks for {len(tickers)} markets...", flush=True)
    totals, notional, by_series = collect(tickers, win_start, last_date)
    if not totals:
        raise SystemExit("API returned no volume at all - leaving existing CSVs untouched.")

    new_totals = [{"date": d,
                   "kalshi_sports_volume": round(totals[d], 2),
                   "kalshi_sports_notional_est": round(notional.get(d, 0.0), 2)}
                  for d in sorted(totals)]
    new_series = [{"date": d, "series": s, "volume": round(v, 2)}
                  for (d, s), v in sorted(by_series.items())]

    # How re-pulled days moved against the committed values (audit trail).
    prev = {r["date"]: float(r["kalshi_sports_volume"]) for r in old_totals}
    for r in new_totals:
        if prev.get(r["date"]):
            chg = (r["kalshi_sports_volume"] / prev[r["date"]] - 1) * 100
            print(f"  re-pulled {r['date']}: {prev[r['date']]:,.0f} -> "
                  f"{r['kalshi_sports_volume']:,.0f} ({chg:+.2f}%)")
    for r in new_totals:
        print(f"  {r['date']}: contracts = {r['kalshi_sports_volume']:,.0f}  "
              f"|  notional est = {r['kalshi_sports_notional_est']:,.0f}")

    if full:
        totals_rows, series_rows = new_totals, new_series
    else:
        # Older days are settled and immutable: keep them as committed.
        totals_rows = [r for r in old_totals if r["date"] < win_start] + new_totals
        series_rows = [r for r in old_series if r["date"] < win_start] + new_series

    vc.write_csv(TOTALS_CSV,
                 ["date", "kalshi_sports_volume", "kalshi_sports_notional_est"],
                 totals_rows)
    vc.write_csv(BY_SERIES_CSV, ["date", "series", "volume"], series_rows)
    print(f"\nDone. {len(totals_rows)} date(s) -> {TOTALS_CSV}")


if __name__ == "__main__":
    main()
