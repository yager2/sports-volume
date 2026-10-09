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
START = "2025-08-03"          # first NY week-ending Sunday covered
BIG4 = {"nfl", "nba", "mlb", "nhl"}   # headline total; college leagues shown separately

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
    return rows, coverage
