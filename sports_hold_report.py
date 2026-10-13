"""
All-sports weekly rollup: NFL (from nfl_hold) + NBA, MLB, NHL, college football,
men's college basketball (from sports_hold.py), in New York's sportsbook-report
weeks (Monday-Sunday, labelled by the Sunday).

Called by nfl_hold_report.py, which passes the NFL per-game summaries it has
already built; writes all_sports_hold_weekly.csv and returns the rows for the
workbook's "All sports weekly" tab.

Every number is exact: summed from every trade in every market of every game
(no sampling). Hold = -(taker P&L - taker fees) / taker $  (+ = takers lost,
i.e. the house / makers won).
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "sports_hold_data")
OUT_CSV = os.path.join(HERE, "all_sports_hold_weekly.csv")
MAIN_WEEKLY_CSV = os.path.join(HERE, "kalshi_hold_weekly.csv")     # Big 4: straights, parlays, combined
MAIN_MONTHLY_CSV = os.path.join(HERE, "kalshi_hold_monthly.csv")
MAIN_ROWS = {}
START = "2025-08-03"          # first NY week-ending Sunday covered
BIG4 = {"nfl", "nba", "mlb", "nhl"}   # headline total; college leagues shown separately
# Parlays counted with the Big 4: every leg an NFL/NBA/MLB/NHL game (see nfl_parlays.py).
# Older sampled files only have "nfl" / "nfl_mixed"; "nfl" is all-NFL so it counts.
BIG4_PARLAY_CATS = {"nfl", "nfl_mixed_big4", "nba", "mlb", "nhl", "big4_multi"}
# Parlays in the main view: every leg in the six tracked leagues, matching the
# straights. ("nfl_mixed"/"other" mix college with untracked sports like soccer
# on days processed before 2026-10-12, so they're left out.)
SIX_PARLAY_CATS = BIG4_PARLAY_CATS | {"nfl_mixed_six", "cfb", "cbb", "six_multi"}   # not "wc": no World Cup straights
MIN_PARLAY_STAKES = 250_000           # below this a period's parlay hold is noise

LEAGUES = [("nfl", "NFL"), ("nba", "NBA"), ("mlb", "MLB"), ("nhl", "NHL"),
           ("cfb", "College football"), ("cbb", "College basketball")]


def ny_week_ending(date_iso: str) -> str:
    d = datetime.fromisoformat(date_iso).date()
    return (d + timedelta(days=6 - d.weekday())).isoformat()


def _blank():
    return defaultdict(float)


def load_league_games(lg: str):
    """Per-game totals from sports_hold.py files: (date, complete, sums dict)."""
    out = []
    for p in glob.glob(os.path.join(DATA_DIR, lg, "????-??", "*.json.gz")):
        try:
            g = json.load(gzip.open(p, "rt", encoding="utf-8"))
        except (OSError, ValueError):
            continue
        a = _blank()
        for prod, phase, b, n, c, dol, pnl, tf, mf, ed, ep, vd, bd in g["rows"]:
            a["dollars"] += dol
            a["pnl"] += pnl
            a["fee"] += tf
            a["maker_fee"] += mf
            a["trades"] += n
            if phase == "pre":
                a["pre_dollars"] += dol
                a["pre_pnl"] += pnl
                a["exp_dollars"] += ed
                a["exp_pnl"] += ep
        out.append((g["date"], g.get("complete", False), a))
    return out


def nfl_games_from_summary(summary_games):
    cols = None
    out = []
    for g in summary_games:
        a = _blank()
        for k, v in g["p"].items():
            prod, phase = k.split("|")
            notional, pnl, tf, mf, en, ep, pw, ph, pq, trades = v
            a["dollars"] += notional
            a["pnl"] += pnl
            a["fee"] += tf
            a["maker_fee"] += mf
            a["trades"] += trades
            if phase == "pre":
                a["pre_dollars"] += notional
                a["pre_pnl"] += pnl
                a["exp_dollars"] += en
                a["exp_pnl"] += ep
        out.append((g["date"], True, a))
    return out


def _hold(a):
    return round(-(a["pnl"] - a["fee"]) / a["dollars"], 5) if a["dollars"] else None


def _luck(a):
    if not a["pre_dollars"] or not a["exp_dollars"]:
        return None
    return round((-a["pre_pnl"] / a["pre_dollars"] + a["exp_pnl"] / a["exp_dollars"]) * 1e4, 0)


def period_rollup(per, parlays_all_by_date, period: str):
    """Main view: straights for all six tracked leagues (NFL, NBA, MLB, NHL,
    college football, men's college basketball), every sports parlay, and the
    two combined - plus each league's straights - by
    New York report week (Mon-Sun, labelled by the Sunday) or calendar month.

    Straights: games by their ET date. Parlays: by their last leg's date.
    Hold = -(taker P&L - taker fees) / taker $ (parlays: / stakes = tickets bought).
    """
    key = ny_week_ending if period == "week" else (lambda d: d[:7])
    st = defaultdict(_blank)
    by_lg = defaultdict(lambda: defaultdict(_blank))      # period -> league -> sums
    for lg, games in per.items():
        for date, complete, a in games:
            lb = by_lg[key(date)][lg]
            lb["dollars"] += a["dollars"]
            lb["pnl"] += a["pnl"]
            lb["fee"] += a["fee"]
            b = st[key(date)]
            b["games"] += 1
            b["incomplete"] += 0 if complete else 1
            for k in ("dollars", "pnl", "fee", "pre_dollars", "pre_pnl", "exp_dollars", "exp_pnl"):
                b[k] += a[k]
    pa = defaultdict(_blank)
    for date, cats in (parlays_all_by_date or {}).items():
        b = pa[key(date)]
        for cat, v in cats.items():
            if cat in SIX_PARLAY_CATS:   # same sports as the straights: every leg in the six leagues
                b["stakes"] += v["stakes"]
                b["pnl"] += v["pnl"]
                b["fee"] += v["fee"]
                b["sampled"] += 0 if v.get("exact") else 1
    # every period from Aug 2025 to now, empty or not
    today = datetime.now().date()
    keys = []
    if period == "week":
        d = datetime.fromisoformat(START).date()
        end = today + timedelta(days=6 - today.weekday())
        while d <= end:
            keys.append(d.isoformat())
            d += timedelta(days=7)
    else:
        y, m = 2025, 8
        while (y, m) <= (today.year, today.month):
            keys.append(f"{y}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    rows = []
    for k in keys:
        a, p = st.get(k), pa.get(k)
        s_d = a["dollars"] if a else 0
        s_win = -(a["pnl"] - a["fee"]) if a else 0
        p_ok = bool(p and p["stakes"] >= MIN_PARLAY_STAKES)
        p_win = -(p["pnl"] - p["fee"]) if p_ok else 0
        c_d = s_d + (p["stakes"] if p_ok else 0)
        rows.append({
            "period": k,
            "games": int(a["games"]) if a else None,
            "straight_taker_dollars": round(s_d, 2) if s_d else None,
            "straight_house_win": round(s_win, 2) if s_d else None,
            "straight_hold": round(s_win / s_d, 5) if s_d else None,
            "parlay_stakes": round(p["stakes"], 2) if p and p["stakes"] else None,
            "parlay_house_win": round(-(p["pnl"] - p["fee"]), 2) if p and p["stakes"] else None,
            "parlay_hold": round(p_win / p["stakes"], 5) if p_ok else None,
            "parlay_source": (None if not p or not p["stakes"] else "estimate" if p["sampled"] else "exact"),
            "combined_dollars": round(c_d, 2) if c_d else None,
            "combined_house_win": round(s_win + p_win, 2) if c_d else None,
            "combined_hold": round((s_win + p_win) / c_d, 5) if c_d and p_ok else None,
            **{f"{lg}_{m}": v for lg, _ in LEAGUES
               for m, v in (("taker_dollars", round(by_lg[k][lg]["dollars"], 2) if by_lg.get(k, {}).get(lg) else None),
                            ("hold", _hold(by_lg[k][lg]) if by_lg.get(k, {}).get(lg) else None))},
            "pregame_luck_bps": _luck(a) if a else None,
            "unsettled_games": int(a["incomplete"]) if a and a["incomplete"] else None,
        })
    return rows


def build(nfl_summary_games, parlays_by_date=None, parlays_all_by_date=None):
    per = {"nfl": nfl_games_from_summary(nfl_summary_games)}
    for lg, _ in LEAGUES[1:]:
        per[lg] = load_league_games(lg)
    coverage = {}
    weeks = defaultdict(lambda: defaultdict(_blank))
    for lg, games in per.items():
        dates = sorted(d for d, _, _ in games)
        coverage[lg] = (dates[0], dates[-1], len(games)) if dates else None
        for date, complete, a in games:
            w = weeks[ny_week_ending(date)][lg]
            w["games"] += 1
            w["incomplete"] += 0 if complete else 1
            for k, v in a.items():
                w[k] += v
    # parlays (NFL only for now; sampled estimate until the exact pass lands)
    par = defaultdict(_blank)
    for date, b in (parlays_by_date or {}).items():
        p = par[ny_week_ending(date)]
        p["stakes"] += b["stakes"]
        p["pnl"] += b["pnl"]
        p["fee"] += b["fee"]
    # every sports parlay (all categories) - exact where the full pass has run
    par_all = defaultdict(_blank)
    for date, cats in (parlays_all_by_date or {}).items():
        p = par_all[ny_week_ending(date)]
        for cat, b in cats.items():
            if cat not in BIG4_PARLAY_CATS:
                continue
            p["stakes"] += b["stakes"]
            p["pnl"] += b["pnl"]
            p["fee"] += b["fee"]
            p["sampled"] += 0 if b.get("exact") else 1
    today = datetime.now().date()
    last_sunday = (today + timedelta(days=6 - today.weekday())).isoformat()
    rows = []
    d = datetime.fromisoformat(START).date()
    while d.isoformat() <= last_sunday:
        we = d.isoformat()
        row = {"week_ending": we}
        tot = _blank()
        for lg, _ in LEAGUES:
            a = weeks.get(we, {}).get(lg)
            if a and lg in BIG4:          # headline total = the big pro leagues
                for k in ("games", "dollars", "pnl", "fee", "trades", "pre_dollars", "pre_pnl", "exp_dollars", "exp_pnl", "incomplete"):
                    tot[k] += a[k]
            row[f"{lg}_games"] = int(a["games"]) if a else None
            row[f"{lg}_taker_dollars"] = round(a["dollars"], 2) if a else None
            row[f"{lg}_hold"] = _hold(a) if a else None
        row["all_games"] = int(tot["games"]) or None
        row["all_taker_dollars"] = round(tot["dollars"], 2) if tot["dollars"] else None
        row["all_taker_pnl"] = round(tot["pnl"], 2) if tot["dollars"] else None
        row["all_kalshi_taker_fees"] = round(tot["fee"], 2) if tot["dollars"] else None
        row["all_hold"] = _hold(tot)
        row["all_pregame_luck_bps"] = _luck(tot)
        row["unsettled_games"] = int(tot["incomplete"]) or None
        p = par.get(we)
        row["nfl_parlay_stakes_est"] = round(p["stakes"], 2) if p and p["stakes"] else None
        row["nfl_parlay_hold"] = (round(-(p["pnl"] - p["fee"]) / p["stakes"], 5)
                                  if p and p["stakes"] >= 250_000 else None)
        pa = par_all.get(we)
        row["parlay_stakes"] = round(pa["stakes"], 2) if pa and pa["stakes"] else None
        row["parlay_hold"] = (round(-(pa["pnl"] - pa["fee"]) / pa["stakes"], 5)
                              if pa and pa["stakes"] >= 250_000 else None)
        row["parlay_source"] = (None if not pa or not pa["stakes"] else "estimate" if pa["sampled"] else "exact")
        if row["parlay_hold"] is not None and row["all_taker_dollars"]:
            win = -(tot["pnl"] - tot["fee"]) + -(pa["pnl"] - pa["fee"])
            row["all_with_parlays_hold"] = round(win / (tot["dollars"] + pa["stakes"]), 5)
        else:
            row["all_with_parlays_hold"] = None
        rows.append(row)
        d += timedelta(days=7)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    MAIN_ROWS["week"] = period_rollup(per, parlays_all_by_date, "week")
    MAIN_ROWS["month"] = period_rollup(per, parlays_all_by_date, "month")
    for per_name, out in (("week", MAIN_WEEKLY_CSV), ("month", MAIN_MONTHLY_CSV)):
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(MAIN_ROWS[per_name][0].keys()))
            w.writeheader()
            w.writerows(MAIN_ROWS[per_name])
    return rows, coverage
