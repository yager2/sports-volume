"""
Kalshi NFL parlay hold - ingest step (companion to nfl_hold.py).

Sportsbooks make most of their hold on parlays, so this measures the same
thing on Kalshi's parlay products: did parlay buyers (takers) lose more or less
than usual, and does the straight-market "public side vs price" score from
nfl_hold predict it?

How it works
------------
Kalshi parlays live in the KXMVE* series (plus KXNFLCOMBO / KXNFLPREPACK).
Every ticket is its own market, created on request, so there are millions of
them and no per-sport series to list. The only exact source of what each
customer paid is the exchange-wide trade feed (~15M trades/day, every
product). Reading all of it costs ~20 min per day of trading, so we SAMPLE:

  * 10 minutes of every hour, rotating which 10 (offset moves each hour so
    kickoff rushes at :00/:25 aren't systematically in or out).
  * Every parlay trade in the sampled minutes is kept; each trade's P&L is
    valid on its own (a cash-out is just a later trade on the other side),
    so the sample's hold is an unbiased estimate of the full day's hold.
    Dollar totals are scaled up by 6 (60 / 10) in the report.

Each sampled parlay market is then looked up (100 per call) for its legs and
settlement. A parlay is "nfl" if every leg is a KXNFL market, "nfl_mixed" if
some legs are, and dropped otherwise. Legs are matched to the per-game files
nfl_hold.py wrote for kickoff (pre-game vs in-game), NFL week, and closing
prices - the product of leg closes is the "independent legs" fair price.

Output: nfl_hold_data/parlays/<trade day UTC>.json.gz, re-pulled until every
NFL parlay in it has settled (complete=true), then never again.

Usage
-----
    python nfl_parlays.py                        # daily: last 4 trade days not yet complete
    python nfl_parlays.py --since 2026-09-01     # backfill
    python nfl_parlays.py --day 2026-09-20 --full  # exact (unsampled) day, for validation
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import nfl_hold as nh

WORKERS = 8                      # concurrent requests; nh's shared limiter still caps the rate

PARLAY_DIR = os.path.join(nh.DATA_DIR, "parlays")
SAMPLE_MIN = 10                  # minutes sampled per hour
DAILY_DAYS = 4                   # daily run re-checks this many trailing trade days
PARLAY_PREFIXES = ("KXMVE",)
PARLAY_SERIES = {"KXNFLCOMBO", "KXNFLPREPACK", "KXNFLPREPACK1HFT"}
# Entertainment MVE products (never NFL) - skip without a lookup.
NON_SPORT_MVE = ("KXMVEGRAMMYS", "KXMVEOSCARS", "KXMVEMENTION", "KXMVEEMMY")
LEG_GAME_RE = re.compile(r"^(KXNFL[A-Z0-9]*)-(\d{2}[A-Z]{3}\d{2}[A-Z]{4,8})$")
LEG_BUCKETS = [(2, "2"), (3, "3"), (4, "4"), (5, "5"), (7, "6-7"), (10, "8-10"), (999, "11+")]


def is_parlay(ticker: str) -> bool:
    s = ticker.split("-", 1)[0]
    if s.startswith(NON_SPORT_MVE):
        return False
    return s.startswith(PARLAY_PREFIXES) or s in PARLAY_SERIES


def leg_bucket(n: int) -> str:
    for hi, lab in LEG_BUCKETS:
        if n <= hi:
            return lab
    return LEG_BUCKETS[-1][1]


def windows(day: datetime, full: bool) -> list[tuple[int, int]]:
    """Sampled [start, end) epoch windows for one UTC day."""
    out = []
    for h in range(24):
        start = int((day + timedelta(hours=h)).timestamp())
        if full:
            out.append((start, start + 3600))
            continue
        hour_index = start // 3600
        off = (hour_index * 23) % (60 - SAMPLE_MIN + 1)
        out.append((start + off * 60, start + (off + SAMPLE_MIN) * 60))
    return out


def sweep(day: datetime, full: bool) -> list[dict]:
    """Every parlay trade in the day's sampled windows."""
    def one(w):
        a, b = w
        return [t for t in nh.paginate("/markets/trades", {"min_ts": a, "max_ts": b, "limit": 1000}, "trades")
                if is_parlay(t["ticker"])]
    with ThreadPoolExecutor(WORKERS) as ex:
        return [t for ts_ in ex.map(one, windows(day, full)) for t in ts_]


def lookup(tickers: list[str]) -> dict[str, dict]:
    chunks = [tickers[i:i + 100] for i in range(0, len(tickers), 100)]

    def one(ch):
        return nh.kalshi("/markets", {"tickers": ",".join(ch), "limit": 1000}).get("markets") or []
    out = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for ms in ex.map(one, chunks):
            for m in ms:
                out[m["ticker"]] = m
    return out


class GameIndex:
    """Kickoff / week / closing prices from nfl_hold's per-game files."""

    def __init__(self):
        self.games = {}
        for p in glob.glob(os.path.join(nh.GAMES_DIR, "*", "*.json.gz")):
            code = os.path.basename(p).split(".")[0]
            self.games[code] = p
        self._loaded = {}

    def get(self, code: str) -> dict | None:
        if code not in self._loaded:
            p = self.games.get(code)
            if not p:
                self._loaded[code] = None
            else:
                g = json.load(gzip.open(p, "rt", encoding="utf-8"))
                self._loaded[code] = {
                    "kickoff": nh.ts(g["kickoff_utc"]),
                    "closes": {r["ticker"]: r["close_norm"] for r in g["markets"]},
                }
        return self._loaded[code]


ROW_COLS = ["category", "legs", "last_leg_date", "phase", "side", "bucket",
            "trades", "contracts", "taker_dollars", "taker_pnl", "taker_fee", "exp_dollars", "exp_pnl"]
PENDING_COLS = ["ticker", "created_time", "side", "count", "yes_price", "no_price"]
RESOLVE_DAYS = 21                # keep re-checking a day's unsettled parlays this long


def _nfl_info(m: dict):
    """(category, leg bucket, last NFL leg date, NFL game codes), or None if no NFL legs."""
    legs = m.get("mve_selected_legs") or []
    nfl = [l for l in legs if l["market_ticker"].startswith("KXNFL")]
    if not nfl:
        return None
    codes = []
    for l in nfl:
        mm = LEG_GAME_RE.match(l.get("event_ticker") or "")
        if mm:
            codes.append(mm.group(2))
    last = max((datetime.strptime(c[:7], "%y%b%d").date() for c in codes), default=None)
    return ("nfl" if len(nfl) == len(legs) else "nfl_mixed", leg_bucket(len(legs)),
            last.isoformat() if last else "?", codes)


def _settled(m: dict) -> bool:
    return m.get("status") in ("finalized", "settled") and m.get("settlement_value_dollars") not in (None, "")


def _accumulate(t: list, m: dict, gi: GameIndex, rows, tix, counted: set) -> None:
    """Add one settled parlay trade (compact PENDING_COLS form) to the rollups."""
    ticker, created, side, c, yes_p, no_p = t
    cat, lb, date_key, codes = _nfl_info(m)
    sv = float(m["settlement_value_dollars"])
    if sv not in (0.0, 1.0):
        return                                  # voided / fair-price parlay
    kicks = [g["kickoff"] for g in (gi.get(x) for x in codes) if g]
    when = nh.ts(created)
    phase = ("pre" if when < min(kicks) else "live") if kicks else "unknown"
    # independent-legs fair price, from each leg's closing price
    q_yes = 1.0
    for l in m.get("mve_selected_legs") or []:
        mm = LEG_GAME_RE.match(l.get("event_ticker") or "")
        g = gi.get(mm.group(2)) if mm else None
        cl = g["closes"].get(l["market_ticker"]) if g else None
        if cl is None:
            q_yes = None
            break
        q_yes *= cl if l["side"] == "yes" else 1 - cl
    p = yes_p if side == "yes" else no_p
    s = sv if side == "yes" else 1 - sv
    r = rows[(cat, lb, date_key, phase, side, nh.bucket_of(c * p))]
    r[0] += 1
    r[1] += c
    r[2] += c * p
    r[3] += c * (s - p)
    r[4] += nh.TAKER_FEE_RATE * c * yes_p * (1 - yes_p)
    if q_yes is not None and phase == "pre":
        q = q_yes if side == "yes" else 1 - q_yes
        r[5] += c * p
        r[6] += c * (q - p)
    if ticker not in counted:
        counted.add(ticker)
        tt = tix[(cat, lb, date_key)]
        tt[0] += 1
        tt[1] += sv


def _finish(d: dict, rows, tix, pending: list, info: dict) -> dict:
    """Write rollups back into the day dict and set the completeness flag.

    A day is complete once no ALL-NFL parlay on an already-played game is
    unsettled. Parlays waiting on future games, or on another sport's leg,
    stay in `pending_trades` and are folded in later by resolve() - they
    don't hold the day open.
    """
    today_iso = datetime.now(timezone.utc).date().isoformat()
    blocking = sum(1 for t in pending
                   if t[0] in info and info[t[0]][0] == "nfl" and info[t[0]][2] < today_iso)
    d["rows"] = [[*k, *[round(x, 4) for x in v[:7]]] for k, v in rows.items()]
    d["tickets"] = [[*k, v[0], v[1]] for k, v in tix.items()]
    d["pending_trades"] = pending
    d["pending_info"] = {tk: list(info[tk][:3]) for tk in {t[0] for t in pending} if tk in info}
    d["pending"] = len(pending)
    d["blocking"] = blocking
    d["complete"] = blocking == 0 and d.get("missing", 0) == 0
    d["row_cols"], d["pending_cols"] = ROW_COLS, PENDING_COLS
    d["ticket_cols"] = ["category", "legs", "last_leg_date", "parlays", "parlays_won"]
    return d


def process_day(day: datetime, full: bool, gi: GameIndex) -> dict:
    t0 = time.time()
    trades = sweep(day, full)
    tickers = sorted({t["ticker"] for t in trades})
    mk = lookup(tickers)
    rows = defaultdict(lambda: [0.0] * 7)      # key -> n, contracts, $, pnl, fee, exp_$, exp_pnl
    tix = defaultdict(lambda: [0, 0.0])        # (cat, legs, date) -> parlays, parlays won
    counted, pending, info = set(), [], {}
    missing = 0
    for t in trades:
        m = mk.get(t["ticker"])
        if m is None:
            missing += 1
            continue
        ni = _nfl_info(m)
        if ni is None:
            continue
        info[t["ticker"]] = ni
        side = t.get("taker_outcome_side") or t["taker_side"]
        compact = [t["ticker"], t["created_time"], side, float(t.get("count_fp") or 0),
                   float(t["yes_price_dollars"]), float(t["no_price_dollars"])]
        if _settled(m):
            _accumulate(compact, m, gi, rows, tix, counted)
        else:
            pending.append(compact)
    d = {
        "day": day.strftime("%Y-%m-%d"),
        "sample_frac": 1.0 if full else SAMPLE_MIN / 60,
        "missing": missing,
        "parlay_trades_seen": len(trades), "parlay_markets_seen": len(tickers),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t0),
    }
    return _finish(d, rows, tix, pending, info)


def resolve(d: dict, gi: GameIndex) -> int:
    """Re-check a day's unsettled parlays and fold newly settled ones in. Returns # resolved."""
    pending = d.get("pending_trades") or []
    if not pending:
        return 0
    mk = lookup(sorted({t[0] for t in pending}))
    rows = defaultdict(lambda: [0.0] * 7)
    for r in d["rows"]:
        rows[tuple(r[:6])] = list(r[6:])
    tix = defaultdict(lambda: [0, 0.0])
    for r in d["tickets"]:
        tix[tuple(r[:3])] = [r[3], r[4]]
    counted, still, info = set(), [], {}
    for t in pending:
        m = mk.get(t[0])
        ni = _nfl_info(m) if m is not None else None
        if ni and _settled(m):
            _accumulate(t, m, gi, rows, tix, counted)
        else:
            still.append(t)
            if ni:
                info[t[0]] = ni
            elif t[0] in d.get("pending_info", {}):
                info[t[0]] = tuple(d["pending_info"][t[0]]) + ([],)
    _finish(d, rows, tix, still, info)
    return len(pending) - len(still)


def day_path(day: str, full: bool) -> str:
    return os.path.join(PARLAY_DIR, f"{day}{'.full' if full else ''}.json.gz")


def load_day(day: str, full: bool) -> dict | None:
    p = day_path(day, full)
    if not os.path.exists(p):
        return None
    try:
        return json.load(gzip.open(p, "rt", encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(d: dict, full: bool) -> None:
    os.makedirs(PARLAY_DIR, exist_ok=True)
    p = day_path(d["day"], full)
    with gzip.open(p + ".tmp", "wt", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(p + ".tmp", p)


def summarize(d: dict) -> str:
    agg = defaultdict(lambda: [0.0, 0.0, 0.0])
    for cat, legs, date, phase, side, b, n, c, dol, pnl, fee, ed, ep in d["rows"]:
        a = agg[cat]
        a[0] += dol if side == "yes" else 0     # stakes = tickets bought
        a[1] += pnl                             # cash-outs (NO-side trades) count as payouts
        a[2] += fee
    parts = [f"{cat}: stakes ${a[0] / d['sample_frac']:,.0f} est, hold {-(a[1] - a[2]) / a[0]:.1%} incl fees"
             for cat, a in sorted(agg.items()) if a[0]]
    return "; ".join(parts) or "no NFL parlays"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD first trade day (UTC)")
    ap.add_argument("--day", help="single trade day YYYY-MM-DD")
    ap.add_argument("--full", action="store_true", help="read every minute (exact; ~6x slower)")
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.day:
        days = [datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)]
    elif args.since:
        d0 = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days = [d0 + timedelta(days=i) for i in range((today - d0).days)]
    else:
        days = [today - timedelta(days=i) for i in range(DAILY_DAYS, 0, -1)]
    # A day is swept once (the expensive part); its few unsettled parlays are
    # re-checked cheaply by resolve() on later runs. Files from before
    # resolve() existed (no pending_trades) are re-swept.
    def needs_sweep(d):
        got = load_day(d.strftime("%Y-%m-%d"), args.full)
        return args.redo or got is None or "pending_trades" not in got
    todo = [d for d in days if needs_sweep(d)]
    print(f"{len(todo)} trade day(s) to pull ({'full' if args.full else f'{SAMPLE_MIN} min/hour sample'})")
    nh._keep_awake(True)
    try:
        gi = GameIndex()
        for i, d in enumerate(todo, 1):
            try:
                out = process_day(d, args.full, gi)
            except (Exception, SystemExit) as e:
                print(f"  [{i}/{len(todo)}] {d:%Y-%m-%d}: FAILED ({e}) - will retry next run", flush=True)
                continue
            save(out, args.full)
            print(f"  [{i}/{len(todo)}] {out['day']}: {out['parlay_trades_seen']:,} parlay trades sampled, "
                  f"{out['seconds']}s, complete={out['complete']} (open {out['pending']}, blocking {out['blocking']})  "
                  f"{summarize(out)}",
                  flush=True)
        # Resolve pass: fold in parlays that settled since their day was swept.
        cutoff = (today - timedelta(days=RESOLVE_DAYS)).strftime("%Y-%m-%d")
        for p in sorted(glob.glob(os.path.join(PARLAY_DIR, "????-??-??*.json.gz"))):
            day = os.path.basename(p)[:10]
            full = ".full" in p
            d = load_day(day, full)
            if not d or not d.get("pending_trades") or day < cutoff:
                continue
            try:
                n = resolve(d, gi)
            except (Exception, SystemExit) as e:
                print(f"  resolve {day}: FAILED ({e})", flush=True)
                continue
            save(d, full)
            print(f"  resolve {day}: {n} newly settled, {d['pending']} still open "
                  f"({d['blocking']} blocking), complete={d['complete']}", flush=True)
    finally:
        nh._keep_awake(False)


if __name__ == "__main__":
    sys.exit(main())
