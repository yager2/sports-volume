"""
Kalshi NFL taker-hold tracker - ingest step.

Question it answers: on Kalshi's NFL markets, did the people who crossed the
spread (takers - the closest thing Kalshi has to sportsbook customers) lose
more or less than the closing prices said they should have? That is a
sportsbook-style "hold", available the morning after every game instead of
weeks later in a state gaming report. See NFL_HOLD_METHODOLOGY.md.

What this script does
---------------------
  1. Finds every NFL series whose events are single games (moneyline, spread,
     total, player props, halves/quarters, other game props). Season-long
     futures (MVP, division winners) are excluded automatically: a series only
     counts if its event tickers look like  SERIES-26SEP20INDKC  and that game
     code also exists as a moneyline game (KXNFLGAME). Parlay products
     (KXNFLCOMBO / KXNFLPREPACK / KXMVE*) are a separate later phase.
  2. For each finished game, lists all of its markets across those series.
  3. Pages every trade in every market (public /markets/trades, plus
     /historical/trades for trades older than Kalshi's archive cutoff) and
     rolls them up per market by (pre-game/live, taker side, ticket-size
     bucket, block trade). The rollup keeps every number the hold report needs
     (contracts, dollars paid, fees) - raw trades are not stored.
  4. Records each market's closing price (last 30 min before kickoff) and its
     settlement value, plus kickoff / final score / week from ESPN.
  5. Writes one gzipped JSON per game to nfl_hold_data/games/<season>/. A game
     whose file exists and is marked complete is never re-pulled, so any run
     can be interrupted and resumed.

nfl_hold_report.py turns those per-game files into the CSV / Excel / HTML.

Usage
-----
    python nfl_hold.py                       # daily: games finished in the last 10 days
    python nfl_hold.py --game 26SEP20INDKC   # one game, prints a summary
    python nfl_hold.py --backfill            # every NFL game Kalshi ever listed
    python nfl_hold.py --since 2026-09-01    # games since a date (uses the archive too)
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "nfl_hold_data")
GAMES_DIR = os.path.join(DATA_DIR, "games")
ESPN_DIR = os.path.join(DATA_DIR, "espn")
SERIES_JSON = os.path.join(DATA_DIR, "series.json")
UNMATCHED_CSV = os.path.join(DATA_DIR, "unmatched_games.csv")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mention-edge nfl-hold)", "Accept": "application/json"}

MONEYLINE = "KXNFLGAME"
# Parlay / pre-packaged combo products - their own phase, never blended with straights.
PARLAY_SERIES = {"KXNFLCOMBO", "KXNFLPREPACK", "KXNFLPREPACK1HFT"}
SERIES_MAX_AGE_DAYS = 7
DAILY_LOOKBACK_DAYS = 10
WORKERS = 4                 # parallel market pulls
REQS_PER_SEC = 12.0         # shared across workers; Kalshi's public tier allows ~20/s
CLOSE_WINDOW_MIN = 30       # closing price = VWAP of the final 30 pre-kickoff minutes...
CLOSE_MIN_TRADES = 3        # ...if at least this many trades, else the last pre-kickoff trade
TAKER_FEE_RATE = 0.07       # Kalshi quadratic fee: rate * mult * C * P * (1-P)
MAKER_FEE_RATE = 0.0175     # 'quadratic_with_maker_fees' series: makers pay 25% of the taker rate

# Ticket-size buckets on taker dollars per trade (retail proxy = small tickets).
BUCKETS = [(25, "a:<$25"), (100, "b:$25-100"), (1000, "c:$100-1k"), (float("inf"), "d:>$1k")]

GAME_CODE_RE = re.compile(r"^(KXNFL[A-Z0-9]*)-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,8})$")

# Product = how the report groups markets. Anything not listed is "game_prop".
PLAYER_PROPS = {
    "TD", "ANYTD", "2TD", "FIRSTTD", "PASSYDS", "RSHYDS", "RECYDS", "REC", "PASSTDS",
    "RRYDS", "RSHATT", "PASSATT", "PASSCOMP", "PASSINT", "FFPTS", "FFPTSLADDER",
    "LADDERREC", "LADDERRECYDS", "LADDERRSHYDS", "ESCALATORREC", "ESCALATORRECYDS",
    "ESCALATORRSHYDS", "LONGREC", "LONGRSH", "MOSTRECYDS", "MOSTRSHYDS", "FANTASYMOST",
}
PERIOD_RE = re.compile(r"^(1H|2H|1Q|2Q|3Q|4Q)")
# Multi-way winner markets whose YES prices should sum to 1 - closing prices are
# normalised across the event so a 51/51 two-sided close becomes 50/50.
NORMALISE = re.compile(r"^KXNFL(GAME|1H|2H|1Q|2Q|3Q|4Q|1HWINNER|2HWINNER|1QWINNER|2QWINNER|3QWINNER|4QWINNER)$")

# Kalshi -> ESPN team code where they differ.
ESPN_ALIAS = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR"}


def product_of(series: str) -> str:
    s = series[len("KXNFL"):]
    if s == "GAME":
        return "moneyline"
    if s == "SPREAD":
        return "spread"
    if s == "TOTAL":
        return "total"
    if s in PLAYER_PROPS:
        return "player_prop"
    if PERIOD_RE.match(s):
        return "period"
    return "game_prop"


def bucket_of(dollars: float) -> str:
    for hi, label in BUCKETS:
        if dollars < hi:
            return label
    return BUCKETS[-1][1]


def ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


# ---------- HTTP ----------

class _RateLimiter:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.gap
        if at > now:
            time.sleep(at - now)


_limiter = _RateLimiter(REQS_PER_SEC)
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
        s.headers.update(HEADERS)
    return s


def http_get(url: str, params: dict | None = None, limit: bool = True) -> dict:
    backoff = 2.0
    for attempt in range(8):
        if limit:
            _limiter.wait()
        try:
            # ESPN's CDN 403s our custom User-Agent; its default one is fine.
            r = (_session().get(url, params=params, timeout=60) if limit
                 else requests.get(url, params=params, timeout=60))
        except (requests.ConnectionError, requests.Timeout, OSError):
            r = None
        if r is not None and r.status_code == 200:
            return r.json()
        if r is not None and r.status_code == 404:
            return {}
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            raise SystemExit(f"HTTP {r.status_code} on {r.url}: {r.text[:300]}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
    raise SystemExit(f"retries exhausted on {url} {params}")


def kalshi(path: str, params: dict | None = None) -> dict:
    return http_get(BASE_URL + path, params)


def paginate(path: str, params: dict, key: str):
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = kalshi(path, p)
        items = data.get(key) or []
        yield from items
        cursor = data.get("cursor")
        if not cursor or not items:
            break


# ---------- series discovery ----------

def load_series(refresh: bool = False) -> dict:
    """{series_ticker: {product, fee_type, fee_multiplier, title}} for per-game NFL series."""
    if os.path.exists(SERIES_JSON) and not refresh:
        cached = json.load(open(SERIES_JSON, encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["as_of"])).days
        if age < SERIES_MAX_AGE_DAYS:
            return cached["series"]
    print("Discovering per-game NFL series...")
    allseries = list(paginate("/series", {"category": "Sports", "limit": 1000}, "series"))
    nfl = [s for s in allseries if s["ticker"].startswith("KXNFL") and s["ticker"] not in PARLAY_SERIES]

    def probe(s):
        evs = kalshi("/events", {"series_ticker": s["ticker"], "limit": 25}).get("events") or []
        if not evs:
            evs = kalshi("/historical/events", {"series_ticker": s["ticker"], "limit": 25}).get("events") or []
        ok = bool(evs) and all(GAME_CODE_RE.match(e["event_ticker"]) for e in evs)
        return s, ok

    out = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for s, ok in ex.map(probe, nfl):
            if ok:
                out[s["ticker"]] = {
                    "product": product_of(s["ticker"]),
                    "fee_type": s.get("fee_type") or "quadratic",
                    "fee_multiplier": float(s.get("fee_multiplier") if s.get("fee_multiplier") is not None else 1),
                    "title": s.get("title", ""),
                }
    if MONEYLINE not in out:
        raise SystemExit("KXNFLGAME not found among per-game series - API shape changed?")
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump({"as_of": datetime.now(timezone.utc).isoformat(), "series": out},
              open(SERIES_JSON, "w", encoding="utf-8"), indent=1, sort_keys=True)
    print(f"  {len(out)} per-game NFL series (of {len(nfl)} NFL series)")
    return out


# ---------- markets ----------

def game_code(event_ticker: str) -> str | None:
    m = GAME_CODE_RE.match(event_ticker or "")
    return m.group(2) + m.group(3) if m else None


def list_series_markets(series: str, min_close: float | None, archive: bool) -> list[dict]:
    out = {}
    if archive:
        for m in paginate("/historical/markets", {"series_ticker": series, "limit": 1000}, "markets"):
            out[m["ticker"]] = m
    p = {"series_ticker": series, "limit": 1000}
    if min_close:
        p["min_close_ts"] = int(min_close)
    for m in paginate("/markets", p, "markets"):
        out[m["ticker"]] = m          # live copy wins over the archived one
    return list(out.values())


def markets_by_game(series: dict, min_close: float | None, archive: bool) -> dict[str, list[dict]]:
    by_game = defaultdict(list)
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(list_series_markets, s, min_close, archive): s for s in series}
        for f in as_completed(futs):
            for m in f.result():
                code = game_code(m.get("event_ticker"))
                if code:
                    by_game[code].append(m)
    # Only game codes that are real moneyline games (drops look-alike tickers).
    return {g: ms for g, ms in by_game.items() if any(m["event_ticker"].startswith(MONEYLINE + "-") for m in ms)}


def markets_for_one_game(series: dict, code: str, archive: bool = True) -> list[dict]:
    def one(s):
        ev = f"{s}-{code}"
        ms = ({m["ticker"]: m for m in paginate("/historical/markets", {"event_ticker": ev, "limit": 1000}, "markets")}
              if archive else {})
        for m in paginate("/markets", {"event_ticker": ev, "limit": 1000}, "markets"):
            ms[m["ticker"]] = m
        return list(ms.values())
    with ThreadPoolExecutor(WORKERS) as ex:
        return [m for ms in ex.map(one, series) for m in ms]


# ---------- ESPN: kickoff, score, week ----------

def espn_day(date_yyyymmdd: str) -> list[dict]:
    """ESPN scoreboard for one ET date, cached once every game on it is final."""
    os.makedirs(ESPN_DIR, exist_ok=True)
    path = os.path.join(ESPN_DIR, f"{date_yyyymmdd}.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    d = http_get(ESPN_URL, {"dates": date_yyyymmdd}, limit=False)
    games = []
    for e in d.get("events") or []:
        c = e["competitions"][0]
        t = {x["homeAway"]: x for x in c["competitors"]}
        games.append({
            "kickoff_utc": e["date"],
            "season": e["season"]["year"],
            "season_type": {1: "pre", 2: "reg", 3: "post"}.get(e["season"]["type"], str(e["season"]["type"])),
            "week": (e.get("week") or {}).get("number"),
            "away": t["away"]["team"]["abbreviation"], "home": t["home"]["team"]["abbreviation"],
            "away_score": int(t["away"].get("score") or 0), "home_score": int(t["home"].get("score") or 0),
            "final": c["status"]["type"].get("completed", False),
        })
    if games and all(g["final"] for g in games):
        json.dump(games, open(path, "w", encoding="utf-8"), indent=1)
    return games


def match_espn(code: str, away: str, home: str) -> dict | None:
    day = datetime.strptime(code[:7], "%y%b%d")
    want = {ESPN_ALIAS.get(away, away), ESPN_ALIAS.get(home, home)}
    for d in (day, day + timedelta(days=1), day - timedelta(days=1)):
        for g in espn_day(d.strftime("%Y%m%d")):
            if {g["away"], g["home"]} == want:
                return g
    return None


def teams_from(code: str, ml_markets: list[dict]) -> tuple[str, str]:
    """Away/home from the game code + the two moneyline tickers (…-IND, …-KC)."""
    teams_part = code[7:]
    sides = [m["ticker"].rsplit("-", 1)[1] for m in ml_markets]
    for a in sides:
        if teams_part.startswith(a) and teams_part[len(a):] in sides:
            return a, teams_part[len(a):]
    return (sides + ["?", "?"])[0], (sides + ["?", "?"])[1]


# ---------- trades -> rollup ----------

def fee_rates(meta: dict) -> tuple[float, float]:
    mult = meta.get("fee_multiplier", 1.0)
    maker = MAKER_FEE_RATE if meta.get("fee_type") == "quadratic_with_maker_fees" else 0.0
    return TAKER_FEE_RATE * mult, maker * mult


def roll_up_market(m: dict, kickoff: float, cutoff: float, rates: tuple[float, float]) -> dict:
    """Page every trade in one market; return per-bucket sums + closing price."""
    t_rate, m_rate = rates
    paths = []
    if (ts(m.get("open_time")) or 0) < cutoff:
        paths.append("/historical/trades")
    if (ts(m.get("close_time")) or 1e12) >= cutoff:
        paths.append("/markets/trades")
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])   # n, contracts, taker $, taker fee, maker fee
    seen = set()
    last_pre = (0.0, None)
    win_num = win_den = 0.0
    win_n = 0
    side_mismatch = 0
    win_start = kickoff - CLOSE_WINDOW_MIN * 60
    for path in paths:
        for t in paginate(path, {"ticker": m["ticker"], "limit": 1000}, "trades"):
            if t["trade_id"] in seen:
                continue
            seen.add(t["trade_id"])
            c = float(t.get("count_fp") or t.get("count") or 0)
            if c <= 0:
                continue
            yes_p = float(t["yes_price_dollars"])
            side = t.get("taker_outcome_side") or t["taker_side"]
            if t.get("taker_side") and side != t["taker_side"]:
                side_mismatch += 1
            p = yes_p if side == "yes" else float(t["no_price_dollars"])
            when = ts(t["created_time"])
            phase = "pre" if when < kickoff else "live"
            key = (phase, side, bucket_of(c * p), bool(t.get("is_block_trade")))
            a = agg[key]
            a[0] += 1
            a[1] += c
            a[2] += c * p
            q = c * yes_p * (1 - yes_p)
            a[3] += t_rate * q
            a[4] += m_rate * q
            if phase == "pre":
                if when > last_pre[0]:
                    last_pre = (when, yes_p)
                if when >= win_start:
                    win_num += c * yes_p
                    win_den += c
                    win_n += 1
    if win_n >= CLOSE_MIN_TRADES and win_den > 0:
        close, method = win_num / win_den, "vwap30"
    elif last_pre[1] is not None:
        close, method = last_pre[1], "last"
    else:
        close, method = None, "none"
    return {
        "agg": [[m["ticker"], *k, *[round(x, 6) for x in v]] for k, v in agg.items()],
        "close_yes": close, "close_method": method,
        "close_age_min": round((kickoff - last_pre[0]) / 60, 1) if last_pre[1] is not None else None,
        "n_trades": len(seen), "side_mismatch": side_mismatch,
    }


def process_game(code: str, markets: list[dict], series: dict, cutoff: float, verbose: bool = False) -> dict | None:
    ml = [m for m in markets if m["event_ticker"].startswith(MONEYLINE + "-")]
    away, home = teams_from(code, ml)
    espn = match_espn(code, away, home)
    if espn:
        kickoff, k_src = ts(espn["kickoff_utc"]), "espn"
    else:
        # Kalshi's NFL rules: a game not started within 48h of its scheduled
        # start resolves at fair price, and expiration_time is set to exactly that.
        exp = ts(ml[0].get("expiration_time")) if ml else None
        if not exp:
            return None
        kickoff, k_src = exp - 48 * 3600, "kalshi_expiration-48h"
        _log_unmatched(code, away, home)

    traded = [m for m in markets if float(m.get("volume_fp") or 0) > 0]
    results, errors = {}, []
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(roll_up_market, m, kickoff, cutoff,
                          fee_rates(series[m["event_ticker"].split("-")[0]])): m for m in traded}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                results[futs[f]["ticker"]] = f.result()
            except (Exception, SystemExit) as e:   # one bad market -> game incomplete, retried next run
                errors.append(f"{futs[f]['ticker']}: {e}")
            if verbose and i % 100 == 0:
                print(f"    {i}/{len(traded)} markets, {time.time() - t0:.0f}s", flush=True)

    rows, agg = [], []
    for m in markets:
        r = results.get(m["ticker"], {})
        s = m["event_ticker"].split("-")[0]
        sv = m.get("settlement_value_dollars")
        rows.append({
            "ticker": m["ticker"], "event": m["event_ticker"], "series": s, "product": series[s]["product"],
            "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"),
            "floor_strike": m.get("floor_strike"), "status": m.get("status"), "result": m.get("result"),
            "settle": float(sv) if sv not in (None, "") else None,
            "volume": float(m.get("volume_fp") or 0),
            "close_yes": r.get("close_yes"), "close_method": r.get("close_method", "none"),
            "close_age_min": r.get("close_age_min"), "n_trades": r.get("n_trades", 0),
            "side_mismatch": r.get("side_mismatch", 0),
            "rules": m.get("rules_primary"),
        })
        agg.extend(r.get("agg", []))
    _normalise_closes(rows)
    complete = all(r["status"] in ("finalized", "settled") and r["settle"] is not None for r in rows)
    complete = complete and not errors
    return {
        "game": code, "away": away, "home": home, "complete": complete,
        "kickoff_utc": iso(kickoff), "kickoff_source": k_src, "espn": espn,
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "errors": errors[:20],
        "markets": rows,
        "agg_cols": ["ticker", "phase", "side", "bucket", "block", "n", "contracts", "taker_dollars", "taker_fee", "maker_fee"],
        "agg": agg,
    }


def _normalise_closes(rows: list[dict]) -> None:
    by_ev = defaultdict(list)
    for r in rows:
        if NORMALISE.match(r["series"]):
            by_ev[r["event"]].append(r)
    for ev, rs in by_ev.items():
        tot = sum(r["close_yes"] or 0 for r in rs)
        ok = all(r["close_yes"] is not None for r in rs) and tot > 0
        for r in rs:
            r["close_norm"] = r["close_yes"] / tot if ok else r["close_yes"]
    for r in rows:
        r.setdefault("close_norm", r["close_yes"])


def _log_unmatched(code, away, home):
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(UNMATCHED_CSV)
    with open(UNMATCHED_CSV, "a", encoding="utf-8") as f:
        if new:
            f.write("logged_at,game,away,home\n")
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{code},{away},{home}\n")


# ---------- files ----------

def game_path(code: str, season) -> str:
    return os.path.join(GAMES_DIR, str(season), f"{code}.json.gz")


def season_guess(code: str) -> int:
    d = datetime.strptime(code[:7], "%y%b%d")
    return d.year if d.month >= 7 else d.year - 1


def existing_complete(code: str) -> bool:
    p = game_path(code, season_guess(code))
    if not os.path.exists(p):
        return False
    try:
        return json.load(gzip.open(p, "rt", encoding="utf-8")).get("complete", False)
    except (OSError, ValueError):
        return False


def save_game(g: dict) -> str:
    season = (g.get("espn") or {}).get("season") or season_guess(g["game"])
    p = game_path(g["game"], season)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(g, f, separators=(",", ":"))
    os.replace(tmp, p)
    return p


# ---------- quick per-game summary (for --game and progress lines) ----------

def quick_summary(g: dict) -> dict:
    mk = {r["ticker"]: r for r in g["markets"]}
    out = defaultdict(lambda: defaultdict(float))
    for tk, phase, side, bucket, block, n, c, d, tf, mf in g["agg"]:
        r = mk[tk]
        s_yes = r["settle"]
        if block or s_yes is None or s_yes not in (0.0, 1.0):
            continue
        s = s_yes if side == "yes" else 1 - s_yes
        o = out[(r["product"], phase)]
        o["trades"] += n
        o["contracts"] += c
        o["notional"] += d
        o["pnl"] += c * s - d
        o["fee"] += tf
        if phase == "pre" and r["close_norm"] is not None:
            q = r["close_norm"] if side == "yes" else 1 - r["close_norm"]
            o["exp_notional"] += d
            o["exp_pnl"] += c * q - d
    return out


def print_summary(g: dict) -> None:
    e = g.get("espn") or {}
    print(f"\n{g['game']}: {g['away']} @ {g['home']}  kickoff {g['kickoff_utc']} ({g['kickoff_source']})"
          f"  final {e.get('away_score')}-{e.get('home_score')}  {e.get('season')} {e.get('season_type')} wk{e.get('week')}"
          f"  complete={g['complete']}")
    s = quick_summary(g)
    tot = defaultdict(lambda: defaultdict(float))
    print(f"  {'product':12s} {'phase':5s} {'trades':>9s} {'taker $':>14s} {'actual hold':>11s} {'exp hold':>9s} {'luck bps':>9s}")
    for (prod, phase), o in sorted(s.items()):
        for k, v in o.items():
            tot[phase][k] += v
        _line(prod, phase, o)
    for phase, o in sorted(tot.items()):
        _line("ALL", phase, o)


def _line(prod, phase, o):
    act = -o["pnl"] / o["notional"] if o["notional"] else 0
    exp = -o["exp_pnl"] / o["exp_notional"] if o.get("exp_notional") else None
    luck = f"{(act - exp) * 1e4:9.0f}" if exp is not None else f"{'':9s}"
    exps = f"{exp:9.2%}" if exp is not None else f"{'':9s}"
    print(f"  {prod:12s} {phase:5s} {o['trades']:9,.0f} {o['notional']:14,.0f} {act:11.2%} {exps} {luck}")


# ---------- main ----------

def _keep_awake(on: bool) -> None:
    """Stop Windows sleeping mid-backfill (same trick as run_all.py)."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", help="one game code, e.g. 26SEP20INDKC")
    ap.add_argument("--backfill", action="store_true", help="every game Kalshi ever listed")
    ap.add_argument("--since", help="YYYY-MM-DD; games since this date (reads the archive)")
    ap.add_argument("--refresh-series", action="store_true")
    ap.add_argument("--redo", action="store_true", help="re-pull games even if already complete")
    args = ap.parse_args()
    _keep_awake(True)
    try:
        run(args)
    finally:
        _keep_awake(False)


def run(args):
    series = load_series(args.refresh_series)
    cutoff = ts(kalshi("/historical/cutoff").get("trades_created_ts")) or 0

    if args.game:
        print(f"Listing markets for {args.game} across {len(series)} series...")
        ms = markets_for_one_game(series, args.game)
        print(f"  {len(ms)} markets, {sum(float(m.get('volume_fp') or 0) > 0 for m in ms)} traded; pulling trades...")
        t0 = time.time()
        g = process_game(args.game, ms, series, cutoff, verbose=True)
        print(f"  done in {time.time() - t0:.0f}s -> {save_game(g)}")
        print_summary(g)
        return

    if args.backfill:
        min_close, archive = None, True
    elif args.since:
        min_close = ts(args.since + "T00:00:00Z")
        archive = min_close < cutoff      # the archive only holds pre-cutoff markets
    else:
        min_close, archive = time.time() - DAILY_LOOKBACK_DAYS * 86400, False

    if archive:
        # Big pulls: one paged listing per series is far cheaper than ~90 calls per game.
        print("Listing markets per series (incl. archive)...")
        games = markets_by_game(series, min_close, archive)
        ml_by_game = {c: [m for m in ms if m["event_ticker"].startswith(MONEYLINE + "-")] for c, ms in games.items()}
    else:
        # Daily: find finished games from the moneyline series alone, then fetch
        # only those games' markets (skips paging every future game's props).
        games = None
        ml_by_game = defaultdict(list)
        for m in list_series_markets(MONEYLINE, min_close, archive=False):
            ml_by_game[game_code(m["event_ticker"])].append(m)
    todo = []
    for code in sorted(ml_by_game, key=lambda c: datetime.strptime(c[:7], "%y%b%d")):
        ml = ml_by_game[code]
        if min_close and not any((ts(m.get("close_time")) or 0) >= min_close for m in ml):
            continue
        if not ml or not all(m.get("status") in ("finalized", "settled") for m in ml):
            continue                      # not played / not settled yet
        if not args.redo and existing_complete(code):
            continue
        todo.append(code)
    print(f"{len(ml_by_game)} games in window, {len(todo)} to pull")
    t_start = time.time()
    for i, code in enumerate(todo, 1):
        t0 = time.time()
        try:
            ms = games[code] if games is not None else markets_for_one_game(series, code, archive=False)
        except (Exception, SystemExit) as e:
            print(f"  [{i}/{len(todo)}] {code}: market listing FAILED ({e}) - will retry next run", flush=True)
            continue
        try:
            g = process_game(code, ms, series, cutoff)
        except (Exception, SystemExit) as e:
            print(f"  [{i}/{len(todo)}] {code}: FAILED ({e}) - will retry next run", flush=True)
            continue
        if g is None:
            print(f"  [{i}/{len(todo)}] {code}: no kickoff, skipped")
            continue
        save_game(g)
        n = sum(r["n_trades"] for r in g["markets"])
        el = time.time() - t_start
        eta = el / i * (len(todo) - i)
        print(f"  [{i}/{len(todo)}] {code} {g['away']}@{g['home']}: {len(g['markets'])} mkts, {n:,} trades, "
              f"{time.time() - t0:.0f}s  complete={g['complete']}  (elapsed {el / 60:.0f}m, ~{eta / 60:.0f}m left)",
              flush=True)


if __name__ == "__main__":
    sys.exit(main())
