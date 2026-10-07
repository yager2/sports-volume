"""
Kalshi taker hold for the other major leagues - NBA, MLB, NHL, college
football (FBS), men's college basketball. Exact, trade-by-trade, same engine
as nfl_hold.py (which stays the NFL pipeline).

For every finished game: list all of its markets across the league's
per-game series (moneyline, spreads, totals, halves/quarters/periods/innings,
player and game props), page EVERY trade in every market, and roll up
taker $, taker P&L, fees, and pre-game expected P&L at closing prices.

Storage is compact on purpose: one small gzipped JSON per game with totals by
(product, pre/in-game, ticket-size bucket) - not per-market rows - so a full
MLB season doesn't add hundreds of MB to the repo. A game is re-pulled until
every market has settled (complete=true), then never again.

Game time (for the pre-game / in-game split and closing prices):
  * NBA, NHL, college football: Kalshi's rule voids a game not started
    within 48h of its scheduled start, and expiration_time is set to exactly
    that -> start = expiration_time - 48h (same rule nfl_hold.py uses).
  * MLB: each market's rules state the first pitch ("at 6:00 PM EDT").
  * Anything else: the moneyline market's close (game end) minus a typical
    game length. Only shifts the pre/in-game line; actual hold is unaffected.

Usage
-----
    python sports_hold.py                          # daily: all leagues, games finished in the last 10 days
    python sports_hold.py --league mlb --since 2025-08-01 [--until 2025-12-31]
    python sports_hold.py --league nba --game 26OCT06NOPOKC
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import nfl_hold as nh

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "sports_hold_data")

LEAGUES = {
    # prefix: every series of the league starts with it; ml: moneyline series;
    # game_h: typical game length (fallback start-time estimate only)
    "nba": {"name": "NBA", "prefix": "KXNBA", "ml": "KXNBAGAME", "game_h": 2.4},
    "mlb": {"name": "MLB", "prefix": "KXMLB", "ml": "KXMLBGAME", "game_h": 2.8},
    "nhl": {"name": "NHL", "prefix": "KXNHL", "ml": "KXNHLGAME", "game_h": 2.6},
    "cfb": {"name": "College football", "prefix": "KXNCAAF", "ml": "KXNCAAFGAME", "game_h": 3.4},
    "cbb": {"name": "Men's college basketball", "prefix": "KXNCAAMB", "ml": "KXNCAAMBGAME", "game_h": 2.1},
}
# Combo / parlay products are never straights.
EXCLUDE_SERIES_RE = re.compile(r"SGP|COMBO|PREPACK|MVE|PARLAY")
PERIOD_RE = re.compile(r"^(1H|2H|1Q|2Q|3Q|4Q|1P|2P|3P|F3|F5|F7|INNING|FIRST\d|2ML)")
PLAYER_PROP = {
    "PTS", "REB", "AST", "3PT", "BLK", "STL", "PRA", "PR", "PA", "RA", "DD", "TD", "2D", "3D",
    "HIT", "HITS", "HR", "HRR", "KS", "RBI", "TB", "SB", "RUNS", "SOG", "GOAL", "ANYGOAL", "FIRSTGOAL",
    "SAVES", "SAVE", "PASSYDS", "RSHYDS", "RECYDS", "REC", "PASSTDS", "ANYTD", "FIRSTTD", "BENCHPTS",
}
BUCKETS = ["a:<$25", "b:$25-100", "c:$100-1k", "d:>$1k"]
DAILY_LOOKBACK_DAYS = 10


def code_re(prefix: str) -> re.Pattern:
    # SERIES-26OCT06NOPOKC ; MLB carries first-pitch time: SERIES-26OCT061800LADATL
    return re.compile(rf"^({prefix}[A-Z0-9]*)-(\d{{2}}[A-Z]{{3}}\d{{2}}(?:\d{{4}})?[A-Z]{{3,18}})$")


def product_of(prefix: str, series: str) -> str:
    s = series[len(prefix):]
    if s == "GAME":
        return "moneyline"
    if s == "SPREAD":
        return "spread"
    if s == "TOTAL":
        return "total"
    if PERIOD_RE.match(s):
        return "period"
    if s in PLAYER_PROP:
        return "player_prop"
    return "game_prop"


def league_dir(lg: str) -> str:
    return os.path.join(DATA_DIR, lg)


# ---------- series discovery ----------

def load_series(lg: str, refresh: bool = False) -> dict:
    cfg = LEAGUES[lg]
    path = os.path.join(league_dir(lg), "series.json")
    if os.path.exists(path) and not refresh:
        cached = json.load(open(path, encoding="utf-8"))
        if (datetime.now(timezone.utc) - datetime.fromisoformat(cached["as_of"])).days < nh.SERIES_MAX_AGE_DAYS:
            return cached["series"]
    print(f"[{lg}] discovering per-game series...")
    rx = code_re(cfg["prefix"])
    allseries = list(nh.paginate("/series", {"category": "Sports", "limit": 1000}, "series"))
    cand = [s for s in allseries if s["ticker"].startswith(cfg["prefix"]) and not EXCLUDE_SERIES_RE.search(s["ticker"])]

    def probe(s):
        evs = nh.kalshi("/events", {"series_ticker": s["ticker"], "limit": 25}).get("events") or []
        return s, bool(evs) and all(rx.match(e["event_ticker"]) for e in evs)

    out = {}
    with ThreadPoolExecutor(nh.WORKERS) as ex:
        for s, ok in ex.map(probe, cand):
            if ok:
                out[s["ticker"]] = {
                    "product": product_of(cfg["prefix"], s["ticker"]),
                    "fee_type": s.get("fee_type") or "quadratic",
                    "fee_multiplier": float(s.get("fee_multiplier") if s.get("fee_multiplier") is not None else 1),
                    "title": s.get("title", ""),
                }
    if cfg["ml"] not in out:
        raise SystemExit(f"[{lg}] {cfg['ml']} not found among per-game series")
    os.makedirs(league_dir(lg), exist_ok=True)
    json.dump({"as_of": datetime.now(timezone.utc).isoformat(), "series": out},
              open(path, "w", encoding="utf-8"), indent=1, sort_keys=True)
    print(f"[{lg}] {len(out)} per-game series (of {len(cand)})")
    return out


# ---------- game time ----------

_FIRST_PITCH = re.compile(r"at (\d{1,2}):(\d{2}) (AM|PM) (EDT|EST|CDT|CST|MDT|MST|PDT|PST)")
_TZ = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8}


def game_start(lg: str, code: str, ml: list[dict]) -> tuple[float | None, str]:
    m = ml[0] if ml else None
    if m is None:
        return None, "none"
    rules = (m.get("rules_secondary") or "") + " " + (m.get("rules_primary") or "")
    if "within 48 hours" in rules and m.get("expiration_time"):
        return nh.ts(m["expiration_time"]) - 48 * 3600, "expiration-48h"
    fp = _FIRST_PITCH.search(rules)
    if fp:
        h, mi, ap, tz = int(fp.group(1)) % 12, int(fp.group(2)), fp.group(3), fp.group(4)
        h += 12 if ap == "PM" else 0
        day = datetime.strptime(code[:7], "%y%b%d")
        local = day.replace(hour=h, minute=mi, tzinfo=timezone(timedelta(hours=_TZ[tz])))
        return local.timestamp(), "rules-first-pitch"
    close = nh.ts(m.get("close_time"))
    if close:
        return close - LEAGUES[lg]["game_h"] * 3600, "close-minus-game-length"
    return None, "none"


# ---------- trades, fetched in parallel ----------

SLICE_CONTRACTS = 2_000_000      # ~10k trades -> one time slice per ~10 pages


def _slices(m: dict) -> list[tuple[int, int]]:
    """Split a big market's trading life into overlapping time windows.

    One market's trades come back page by page in sequence, so a 200k-trade
    moneyline takes minutes on its own. Cutting its life into windows lets
    them run side by side. Windows overlap by a second and trades are
    de-duplicated by trade_id, so nothing is missed or counted twice.
    """
    vol = float(m.get("volume_fp") or 0)
    n = max(1, min(64, int(vol // SLICE_CONTRACTS)))
    if n == 1:
        return [(0, 0)]
    a = int(nh.ts(m.get("open_time")) or 0) - 60
    b = int(max(nh.ts(m.get("close_time")) or 0, nh.ts(m.get("settlement_ts")) or 0)) + 86400
    step = (b - a) / n
    return [(int(a + i * step) - 1, int(a + (i + 1) * step) + 1) for i in range(n)]


def fetch_trades(markets: list[dict], cutoff: float, errors: list) -> dict[str, list[dict]]:
    jobs = []
    for m in markets:
        for path in nh.trade_paths(m, cutoff):
            for lo, hi in _slices(m):
                jobs.append((m["ticker"], path, lo, hi))

    def one(job):
        tk, path, lo, hi = job
        p = {"ticker": tk, "limit": 1000}
        if hi:
            p["min_ts"], p["max_ts"] = lo, hi
        return tk, list(nh.paginate(path, p, "trades"))

    out: dict[str, list[dict]] = defaultdict(list)
    bad = set()
    with ThreadPoolExecutor(nh.WORKERS) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for f, j in zip(futs, jobs):
            try:
                tk, ts_ = f.result()
                out[tk].extend(ts_)
            except (Exception, SystemExit) as e:
                bad.add(j[0])
                errors.append(f"{j[0]}: {e}")
    for tk in bad:
        out.pop(tk, None)
    for m in markets:
        out.setdefault(m["ticker"], [])
    return out


# ---------- one game ----------

def normalise_rx(prefix: str) -> re.Pattern:
    return re.compile(rf"^{prefix}(GAME|1H|2H|1Q|2Q|3Q|4Q|1P|2P|3P|F3|F5|F7|1HWINNER|2HWINNER|1QWINNER|2QWINNER|3QWINNER|4QWINNER)$")


def process_game(lg: str, code: str, markets: list[dict], series: dict, cutoff: float) -> dict | None:
    cfg = LEAGUES[lg]
    ml = [m for m in markets if m["event_ticker"].startswith(cfg["ml"] + "-")]
    kickoff, k_src = game_start(lg, code, ml)
    if kickoff is None:
        return None
    traded = [m for m in markets if float(m.get("volume_fp") or 0) > 0]
    results, errors = {}, []
    fetched = fetch_trades(traded, cutoff, errors)
    for m in traded:
        if m["ticker"] in fetched:
            try:
                results[m["ticker"]] = nh.roll_up_market(m, kickoff, cutoff,
                                                         nh.fee_rates(series[m["event_ticker"].split("-")[0]]),
                                                         trades=fetched[m["ticker"]])
            except (Exception, SystemExit) as e:
                errors.append(f"{m['ticker']}: {e}")
    rows = []
    for m in markets:
        r = results.get(m["ticker"], {})
        sv = m.get("settlement_value_dollars")
        rows.append({"ticker": m["ticker"], "event": m["event_ticker"], "series": m["event_ticker"].split("-")[0],
                     "settle": float(sv) if sv not in (None, "") else None, "status": m.get("status"),
                     "close_yes": r.get("close_yes"), "agg": r.get("agg", []), "n_trades": r.get("n_trades", 0)})
    # closing prices of multi-way winner markets sum to 1
    nrx = normalise_rx(cfg["prefix"])
    by_ev = defaultdict(list)
    for r in rows:
        if nrx.match(r["series"]):
            by_ev[r["event"]].append(r)
    for rs in by_ev.values():
        tot = sum(r["close_yes"] or 0 for r in rs)
        ok = tot > 0 and all(r["close_yes"] is not None for r in rs)
        for r in rs:
            r["close_norm"] = r["close_yes"] / tot if ok else r["close_yes"]
    for r in rows:
        r.setdefault("close_norm", r["close_yes"])

    # compact totals: (product, phase, bucket) -> sums ; public-side lean per product
    acc = defaultdict(lambda: [0.0] * 10)   # trades, contracts, $, pnl, tfee, mfee, exp$, exp_pnl, void$, block$
    lean = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        prod = series[r["series"]]["product"]
        sv = r["settle"]
        for tk, phase, side, bucket, block, n, c, d, tf, mf in r["agg"]:
            a = acc[(prod, phase, bucket)]
            if sv is None or sv not in (0.0, 1.0):
                a[8] += d
                continue
            if block:
                a[9] += d
                continue
            s = sv if side == "yes" else 1 - sv
            a[0] += n
            a[1] += c
            a[2] += d
            a[3] += c * s - d
            a[4] += tf
            a[5] += mf
            if phase == "pre" and r["close_norm"] is not None:
                q = r["close_norm"] if side == "yes" else 1 - r["close_norm"]
                a[6] += d
                a[7] += c * q - d
                lean[r["ticker"]][0 if side == "yes" else 1] += d
    pub = defaultdict(lambda: [0.0, 0.0, 0.0])
    for r in rows:
        y, n_ = lean.get(r["ticker"], (0, 0))
        w = abs(y - n_)
        if not w:
            continue
        pop_yes = y > n_
        p = pub[series[r["series"]]["product"]]
        p[0] += w
        p[1] += w * (r["settle"] if pop_yes else 1 - r["settle"])
        p[2] += w * (r["close_norm"] if pop_yes else 1 - r["close_norm"])

    teams = re.sub(r"^\d+", "", code[7:])
    sides = [m["ticker"].rsplit("-", 1)[1] for m in ml]
    away = next((a for a in sides if teams.startswith(a) and teams[len(a):] in sides), sides[0] if sides else "?")
    home = teams[len(away):] if teams.startswith(away) else (sides[1] if len(sides) > 1 else "?")
    complete = (not errors and all(r["status"] in ("finalized", "settled") and r["settle"] is not None for r in rows))
    return {
        "league": lg, "game": code, "date": datetime.strptime(code[:7], "%y%b%d").date().isoformat(),
        "away": away, "home": home, "kickoff_utc": nh.iso(kickoff), "kickoff_source": k_src,
        "complete": complete, "errors": errors[:10],
        "markets": len(markets), "traded_markets": len(traded), "trades": sum(r["n_trades"] for r in rows),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_cols": ["product", "phase", "bucket", "trades", "contracts", "taker_dollars", "taker_pnl",
                     "taker_fee", "maker_fee", "exp_dollars", "exp_pnl", "void_dollars", "block_dollars"],
        "rows": [[*k, *[round(x, 4) for x in v]] for k, v in acc.items()],
        "pub_cols": ["product", "w", "w_hit", "w_q"],
        "pub": [[k, *[round(x, 4) for x in v]] for k, v in pub.items()],
    }


# ---------- files ----------

def game_path(lg: str, code: str) -> str:
    d = datetime.strptime(code[:7], "%y%b%d")
    return os.path.join(league_dir(lg), d.strftime("%Y-%m"), f"{code}.json.gz")


def existing_complete(lg: str, code: str) -> bool:
    p = game_path(lg, code)
    if not os.path.exists(p):
        return False
    try:
        return json.load(gzip.open(p, "rt", encoding="utf-8")).get("complete", False)
    except (OSError, ValueError):
        return False


def save(g: dict) -> None:
    p = game_path(g["league"], g["game"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with gzip.open(p + ".tmp", "wt", encoding="utf-8") as f:
        json.dump(g, f, separators=(",", ":"))
    os.replace(p + ".tmp", p)


def summarize(g: dict) -> str:
    d = pnl = fee = 0.0
    for prod, phase, b, n, c, dol, p, tf, mf, ed, ep, vd, bd in g["rows"]:
        d += dol
        pnl += p
        fee += tf
    return f"taker ${d:,.0f}, hold {-(pnl - fee) / d:.1%} incl fees" if d else "no taker $"


# ---------- run ----------

def finished_games(lg: str, min_close: float | None, max_close: float | None, archive: bool) -> list[str]:
    """Game codes whose moneyline has fully settled, from the moneyline series alone (cheap)."""
    cfg = LEAGUES[lg]
    rx = code_re(cfg["prefix"])
    ms = {}
    if archive:
        for m in nh.paginate("/historical/markets", {"series_ticker": cfg["ml"], "limit": 1000}, "markets"):
            ms[m["ticker"]] = m
    p = {"series_ticker": cfg["ml"], "limit": 1000}
    if min_close:
        p["min_close_ts"] = int(min_close)
    for m in nh.paginate("/markets", p, "markets"):
        ms[m["ticker"]] = m
    games = defaultdict(list)
    for m in ms.values():
        mm = rx.match(m.get("event_ticker") or "")
        if mm:
            games[mm.group(2)].append(m)
    out = []
    for code, mls in games.items():
        close = max((nh.ts(m.get("close_time")) or 0) for m in mls)
        if (min_close and close < min_close) or (max_close and close >= max_close):
            continue
        if all(m.get("status") in ("finalized", "settled") for m in mls):
            out.append(code)
    return sorted(out, key=lambda c: (datetime.strptime(c[:7], "%y%b%d"), c))


def series_games(lg: str, series: dict) -> dict[str, set]:
    """{series: set of game codes it listed}, from each series' event list.

    Lets each game query only the series that actually had it (one request
    per real event) instead of every series - Kalshi's ~4/s request budget
    is the bottleneck, and most series didn't exist for older games.
    """
    rx = code_re(LEAGUES[lg]["prefix"])

    def one(s):
        codes = set()
        for e in nh.paginate("/events", {"series_ticker": s, "limit": 200}, "events"):
            mm = rx.match(e.get("event_ticker") or "")
            if mm:
                codes.add(mm.group(2))
        return s, codes

    with ThreadPoolExecutor(nh.WORKERS) as ex:
        return dict(ex.map(one, series))


def markets_for_game(code: str, series_codes: dict[str, set], in_archive: bool) -> list[dict]:
    path = "/historical/markets" if in_archive else "/markets"
    todo = [s for s, codes in series_codes.items() if code in codes]

    def one(s):
        ms = list(nh.paginate(path, {"event_ticker": f"{s}-{code}", "limit": 1000}, "markets"))
        if not ms and in_archive:      # straddles the archive cutoff
            ms = list(nh.paginate("/markets", {"event_ticker": f"{s}-{code}", "limit": 1000}, "markets"))
        return ms

    with ThreadPoolExecutor(nh.WORKERS) as ex:
        return [m for ms in ex.map(one, todo) for m in ms]


def run_league(lg: str, args, cutoff: float) -> None:
    series = load_series(lg, args.refresh_series)
    if args.game:
        todo = [args.game]
        archive = True
    else:
        if args.since:
            min_close = nh.ts(args.since + "T00:00:00Z")
            max_close = nh.ts(args.until + "T00:00:00Z") + 86400 if args.until else None
        else:
            min_close, max_close = time.time() - DAILY_LOOKBACK_DAYS * 86400, None
        archive = min_close < cutoff
        codes = finished_games(lg, min_close, max_close, archive)
        todo = [c for c in codes if args.redo or not existing_complete(lg, c)]
        print(f"[{lg}] {len(codes)} finished games in window, {len(todo)} to pull", flush=True)
    print(f"[{lg}] reading which games each series listed...", flush=True)
    sc = series_games(lg, series)
    t_start = time.time()
    for i, code in enumerate(todo, 1):
        t0 = time.time()
        # Kalshi's archive only holds markets that settled before its cutoff.
        game_day = datetime.strptime(code[:7], "%y%b%d").replace(tzinfo=timezone.utc).timestamp()
        in_archive = archive and game_day < cutoff + 3 * 86400
        try:
            ms = markets_for_game(code, sc, in_archive)
            g = process_game(lg, code, ms, series, cutoff)
        except (Exception, SystemExit) as e:
            print(f"  [{lg} {i}/{len(todo)}] {code}: FAILED ({e}) - will retry next run", flush=True)
            continue
        if g is None:
            print(f"  [{lg} {i}/{len(todo)}] {code}: no start time, skipped", flush=True)
            continue
        save(g)
        el = time.time() - t_start
        print(f"  [{lg} {i}/{len(todo)}] {code}: {g['markets']} mkts, {g['trades']:,} trades, {time.time() - t0:.0f}s, "
              f"{summarize(g)}, complete={g['complete']}  (elapsed {el / 60:.0f}m, ~{el / i * (len(todo) - i) / 60:.0f}m left)",
              flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=sorted(LEAGUES), help="default: all leagues")
    ap.add_argument("--since", help="YYYY-MM-DD (games closing on/after)")
    ap.add_argument("--until", help="YYYY-MM-DD (games closing on/before)")
    ap.add_argument("--game", help="one game code (needs --league)")
    ap.add_argument("--refresh-series", action="store_true")
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    if args.game and not args.league:
        raise SystemExit("--game needs --league")
    # Kalshi's public API sustains ~4-5 requests/s per IP; stay just under it.
    nh.WORKERS = 8
    nh._limiter = nh._RateLimiter(nh.REQS_PER_SEC)
    cutoff = nh.ts(nh.kalshi("/historical/cutoff").get("trades_created_ts")) or 0
    nh._keep_awake(True)
    try:
        for lg in ([args.league] if args.league else list(LEAGUES)):
            run_league(lg, args, cutoff)
    finally:
        nh._keep_awake(False)


if __name__ == "__main__":
    sys.exit(main())
