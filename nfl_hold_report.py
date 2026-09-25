"""
Kalshi NFL taker-hold tracker - report step.

Reads every per-game file nfl_hold.py wrote (nfl_hold_data/games/*/*.json.gz)
and rebuilds, from scratch each run:

  nfl_hold_by_game.csv          one row per game: hold (pre-game / in-game / all),
                                expected hold + luck, and the outcome factors
                                (favourite won? over hit? favourite covered?
                                did the public's side beat its price?)
  nfl_hold_by_game_product.csv  same, split by product (moneyline / spread /
                                total / player_prop / period / game_prop)
  nfl_hold_weekly.csv           NFL-week rollup of the above
  nfl_hold.xlsx                 formatted workbook (make via nfl_hold_excel.py)
  nfl_hold_dashboard.html       interactive charts (via the HTML template)

Definitions (full detail in NFL_HOLD_METHODOLOGY.md):
  taker $            = contracts x price the taker paid
  taker P&L          = contracts x (payout of taker's side) - taker $
  hold               = -(taker P&L) / taker $          (+ = takers lost, like a book's hold)
  hold incl. fees    = -(taker P&L - taker fees) / taker $   <- headline, sportsbook-comparable
  expected hold      = same, but paying out at the CLOSING price instead of the result
                       (pre-game trades only - there is no "close" for in-game bets)
  luck (bps)         = (actual - expected) x 10,000 on pre-game trades.
                       + = results went the house's way; - = the public's way.
  public-side edge   = for every market, which side takers net-bought pre-game, and
                       did that side win more often than its closing price implied.
                       + = public beat the prices (bad weekend for books/parlays).

Block trades and voided markets (settled at a "fair price" - inactive player,
postponement) are excluded from hold, the way a sportsbook refunds a void.

    python nfl_hold_report.py
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GAMES_GLOB = os.path.join(HERE, "nfl_hold_data", "games", "*", "*.json.gz")
BY_GAME_CSV = os.path.join(HERE, "nfl_hold_by_game.csv")
BY_GAME_PRODUCT_CSV = os.path.join(HERE, "nfl_hold_by_game_product.csv")
WEEKLY_CSV = os.path.join(HERE, "nfl_hold_weekly.csv")
SUMMARY_JSON = os.path.join(HERE, "nfl_hold_data", "summary.json")
TEMPLATE_HTML = os.path.join(HERE, "nfl_hold_dashboard_template.html")
DASHBOARD_HTML = os.path.join(HERE, "nfl_hold_dashboard.html")

PRODUCTS = ["moneyline", "spread", "total", "player_prop", "period", "game_prop"]
PHASES = ["pre", "live"]
ESPN_ALIAS = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR"}
ET = timedelta(hours=-4)   # labels only; a Sunday-night game stays on Sunday either way

METRICS = ["trades", "contracts", "notional", "pnl", "taker_fee", "maker_fee",
           "exp_notional", "exp_pnl", "void_notional", "block_notional",
           "pub_w", "pub_hit", "pub_q"]


def new_acc():
    return dict.fromkeys(METRICS, 0.0)


def add(acc, other):
    for k in METRICS:
        acc[k] += other[k]


def derived(a: dict) -> dict:
    """Rates from an accumulator (None where the denominator is empty)."""
    n, en = a["notional"], a["exp_notional"]
    act = -a["pnl"] / n if n else None
    act_f = -(a["pnl"] - a["taker_fee"]) / n if n else None
    exp = -a["exp_pnl"] / en if en else None
    return {
        "hold": act, "hold_incl_fees": act_f, "exp_hold": exp,
        # luck is measured on the pre-game slice only; callers pass a pre-only acc
        "pub_edge": (a["pub_hit"] - a["pub_q"]) / a["pub_w"] if a["pub_w"] else None,
        "kalshi_fees": a["taker_fee"] + a["maker_fee"],
    }


# ---------- per game ----------

def load_games() -> list[dict]:
    out = []
    for p in sorted(glob.glob(GAMES_GLOB)):
        try:
            out.append(json.load(gzip.open(p, "rt", encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"  skip unreadable {p}: {e}")
    return out


def _ladder_line(points: list[tuple[float, float]]) -> float | None:
    """Strike where P(over strike) crosses 50%, linear between rungs."""
    pts = sorted((s, p) for s, p in points if s is not None and p is not None)
    for (s0, p0), (s1, p1) in zip(pts, pts[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return round(s0 + (p0 - 0.5) / (p0 - p1) * (s1 - s0), 1)
    return None


def game_factors(g: dict) -> dict:
    mk = g["markets"]
    e = g.get("espn") or {}
    score = {}
    if e:
        score[e["away"]], score[e["home"]] = e["away_score"], e["home_score"]
    pts = lambda team: score.get(ESPN_ALIAS.get(team, team))
    f = {"fav": None, "fav_prob": None, "fav_won": None, "total_line": None, "total_pts": None,
         "over_hit": None, "spread_line": None, "fav_margin": None, "fav_covered": None}
    ml = [r for r in mk if r["series"] == "KXNFLGAME" and r["close_norm"] is not None]
    if len(ml) == 2:
        fav = max(ml, key=lambda r: r["close_norm"])
        team = fav["ticker"].rsplit("-", 1)[1]
        dog = [r for r in ml if r is not fav][0]["ticker"].rsplit("-", 1)[1]
        f.update(fav=team, fav_prob=round(fav["close_norm"], 4), fav_won=fav["settle"])
        if pts(team) is not None and pts(dog) is not None:
            f["fav_margin"] = pts(team) - pts(dog)
            f["total_pts"] = pts(team) + pts(dog)
        sp = []
        for r in mk:
            m = re.search(r"-([A-Z]+)(\d+)$", r["ticker"])
            if r["series"] == "KXNFLSPREAD" and m and m.group(1) == team and r["close_yes"] is not None:
                sp.append((float(r["floor_strike"]), r["close_yes"]))
        f["spread_line"] = _ladder_line(sp)
        if f["spread_line"] is not None and f["fav_margin"] is not None:
            f["fav_covered"] = 1.0 if f["fav_margin"] > f["spread_line"] else 0.0
    tl = [(float(r["floor_strike"]), r["close_yes"]) for r in mk
          if r["series"] == "KXNFLTOTAL" and r.get("floor_strike") is not None and r["close_yes"] is not None]
    f["total_line"] = _ladder_line(tl)
    if f["total_line"] is not None and f["total_pts"] is not None:
        f["over_hit"] = 1.0 if f["total_pts"] > f["total_line"] else 0.0
    return f


def game_accs(g: dict) -> dict:
    """{(product, phase, bucket): acc} for one game, plus the public-side index."""
    mk = {r["ticker"]: r for r in g["markets"]}
    accs = defaultdict(new_acc)
    lean = defaultdict(lambda: [0.0, 0.0])     # ticker -> [yes $, no $] pre-game, non-block
    for tk, phase, side, bucket, block, n, c, d, tf, mf in g["agg"]:
        r = mk[tk]
        a = accs[(r["product"], phase, bucket)]
        sv = r["settle"]
        if sv is None or sv not in (0.0, 1.0):
            a["void_notional"] += d
            continue
        if block:
            a["block_notional"] += d
            continue
        s = sv if side == "yes" else 1 - sv
        a["trades"] += n
        a["contracts"] += c
        a["notional"] += d
        a["pnl"] += c * s - d
        a["taker_fee"] += tf
        a["maker_fee"] += mf
        if phase == "pre" and r["close_norm"] is not None:
            q = r["close_norm"] if side == "yes" else 1 - r["close_norm"]
            a["exp_notional"] += d
            a["exp_pnl"] += c * q - d
            lean[tk][0 if side == "yes" else 1] += d
    # Public side: whichever side takers net-bought pre-game, weighted by the net $.
    for tk, (y, n_) in lean.items():
        r = mk[tk]
        w = abs(y - n_)
        if not w:
            continue
        pop_yes = y > n_
        hit = r["settle"] if pop_yes else 1 - r["settle"]
        q = r["close_norm"] if pop_yes else 1 - r["close_norm"]
        a = accs[(r["product"], "pre", "_pub")]
        a["pub_w"] += w
        a["pub_hit"] += w * hit
        a["pub_q"] += w * q
    return accs


def game_meta(g: dict) -> dict:
    e = g.get("espn") or {}
    k = datetime.fromisoformat(g["kickoff_utc"].replace("Z", "+00:00"))
    et_day = (k + ET).date()
    season = e.get("season") or (et_day.year if et_day.month >= 7 else et_day.year - 1)
    stype = e.get("season_type") or ("pre" if et_day.month == 8 else "reg")
    week = e.get("week")
    tag = {"pre": "PRE ", "post": "POST ", "reg": ""}.get(stype, "")
    return {
        "game": g["game"], "date": et_day.isoformat(), "kickoff_utc": g["kickoff_utc"],
        "season": season, "season_type": stype, "week": week,
        "week_label": f"{season} {tag}W{week:02d}" if week else f"{season} ?",
        "month": et_day.strftime("%Y-%m"),
        "away": g["away"], "home": g["home"],
        "away_score": e.get("away_score"), "home_score": e.get("home_score"),
        "complete": g.get("complete", False),
    }


# ---------- rollups ----------

def rollup(games):
    """Return (game_rows, game_product_rows, weekly_rows, summary_dict)."""
    per_game = []
    for g in games:
        meta = game_meta(g)
        meta.update(game_factors(g))
        per_game.append((meta, game_accs(g)))
    per_game.sort(key=lambda x: x[0]["kickoff_utc"])

    def pick(accs, product=None, phase=None, bucket=None, pub=False):
        out = new_acc()
        for (p, ph, b), a in accs.items():
            if (b == "_pub") != pub:
                continue
            if product and p != product:
                continue
            if phase and ph != phase:
                continue
            if bucket and b != bucket:
                continue
            add(out, a)
        return out

    def row_for(accs, product=None):
        row = {}
        for ph in ("pre", "live", None):
            a = pick(accs, product, ph)
            if ph == "pre":
                a2 = pick(accs, product, "pre", pub=True)
                a["pub_w"], a["pub_hit"], a["pub_q"] = a2["pub_w"], a2["pub_hit"], a2["pub_q"]
            d = derived(a)
            tag = ph or "all"
            row[f"{tag}_taker_dollars"] = round(a["notional"], 2)
            row[f"{tag}_trades"] = int(a["trades"])
            row[f"{tag}_taker_pnl"] = round(a["pnl"], 2)
            row[f"{tag}_hold"] = _r(d["hold"])
            row[f"{tag}_hold_incl_fees"] = _r(d["hold_incl_fees"])
            row[f"{tag}_kalshi_fees"] = round(d["kalshi_fees"], 2)
            if ph == "pre":
                row["pre_exp_hold"] = _r(d["exp_hold"])
                row["pre_luck_bps"] = _r((d["hold"] - d["exp_hold"]) * 1e4, 0) if d["exp_hold"] is not None and d["hold"] is not None else None
                row["pre_public_edge"] = _r(d["pub_edge"])
            if ph is None:
                row["void_dollars"] = round(a["void_notional"], 2)
                row["block_dollars"] = round(a["block_notional"], 2)
        return row

    game_rows, gp_rows = [], []
    for meta, accs in per_game:
        game_rows.append({**meta, **row_for(accs)})
        for p in PRODUCTS:
            r = row_for(accs, p)
            if r["all_taker_dollars"] or r["void_dollars"]:
                gp_rows.append({**{k: meta[k] for k in ("game", "date", "season", "season_type", "week_label", "away", "home")},
                                "product": p, **r})

    # week rollup: sum accumulators, and outcome factors as simple rates
    weeks = defaultdict(lambda: {"accs": defaultdict(new_acc), "games": []})
    for meta, accs in per_game:
        w = weeks[meta["week_label"]]
        w["games"].append(meta)
        for k, a in accs.items():
            add(w["accs"][k], a)
    weekly_rows = []
    for label, w in sorted(weeks.items(), key=lambda kv: kv[1]["games"][0]["kickoff_utc"]):
        gs = w["games"]
        row = {"week_label": label, "season": gs[0]["season"], "season_type": gs[0]["season_type"],
               "first_date": gs[0]["date"], "last_date": gs[-1]["date"], "games": len(gs),
               **row_for(w["accs"]), **_factor_rates(gs)}
        weekly_rows.append(row)

    summary = build_summary(per_game, pick, row_for, weekly_rows, game_rows)
    return game_rows, gp_rows, weekly_rows, summary


def _factor_rates(gs):
    def mean(key, sub=None):
        vals = [g[key] for g in gs if g.get(key) is not None and (sub is None or g.get(sub) is not None)]
        return round(sum(vals) / len(vals), 4) if vals else None
    tot = [g["total_pts"] - g["total_line"] for g in gs if g.get("total_pts") is not None and g.get("total_line") is not None]
    return {
        "fav_win_rate": mean("fav_won"), "fav_implied": mean("fav_prob", "fav_won"),
        "fav_cover_rate": mean("fav_covered"), "over_rate": mean("over_hit"),
        "pts_vs_total_line": round(sum(tot) / len(tot), 1) if tot else None,
    }


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def build_summary(per_game, pick, row_for, weekly_rows, game_rows):
    """Compact JSON the dashboard embeds."""
    games = []
    for meta, accs in per_game:
        prod = {}
        for p in PRODUCTS:
            for ph in PHASES:
                a = pick(accs, p, ph)
                if ph == "pre":
                    a2 = pick(accs, p, "pre", pub=True)
                    a["pub_w"], a["pub_hit"], a["pub_q"] = a2["pub_w"], a2["pub_hit"], a2["pub_q"]
                if a["notional"] or a["pub_w"]:
                    prod[f"{p}|{ph}"] = [round(a[k], 2) for k in
                                         ("notional", "pnl", "taker_fee", "maker_fee", "exp_notional", "exp_pnl",
                                          "pub_w", "pub_hit", "pub_q", "trades")]
        buckets = {}
        for (p, ph, b), a in accs.items():
            if b != "_pub" and a["notional"]:
                k = f"{ph}|{b}"
                cur = buckets.setdefault(k, [0.0, 0.0, 0.0])
                cur[0] += a["notional"]
                cur[1] += a["pnl"]
                cur[2] += a["taker_fee"]
        games.append({
            **{k: meta[k] for k in ("game", "date", "season", "season_type", "week", "week_label", "month",
                                    "away", "home", "away_score", "home_score", "fav", "fav_prob", "fav_won",
                                    "total_line", "total_pts", "over_hit", "spread_line", "fav_margin", "fav_covered")},
            "p": prod,
            "b": {k: [round(x, 2) for x in v] for k, v in buckets.items()},
        })
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "products": PRODUCTS,
        "prod_cols": ["notional", "pnl", "taker_fee", "maker_fee", "exp_notional", "exp_pnl",
                      "pub_w", "pub_hit", "pub_q", "trades"],
        "games": games,
    }


# ---------- writers ----------

def write_csv(path, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def write_dashboard(summary):
    if not os.path.exists(TEMPLATE_HTML):
        print("  (no dashboard template yet - skipped HTML)")
        return
    html = open(TEMPLATE_HTML, encoding="utf-8").read()
    data = json.dumps(summary, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", data, 1)
    open(DASHBOARD_HTML, "w", encoding="utf-8").write(html)


def main():
    games = load_games()
    print(f"{len(games)} game files")
    if not games:
        return
    game_rows, gp_rows, weekly_rows, summary = rollup(games)
    write_csv(BY_GAME_CSV, game_rows)
    write_csv(BY_GAME_PRODUCT_CSV, gp_rows)
    write_csv(WEEKLY_CSV, weekly_rows)
    os.makedirs(os.path.dirname(SUMMARY_JSON), exist_ok=True)
    json.dump(summary, open(SUMMARY_JSON, "w", encoding="utf-8"), separators=(",", ":"))
    write_dashboard(summary)
    try:
        import nfl_hold_excel
        nfl_hold_excel.build(game_rows, gp_rows, weekly_rows, summary)
    except ImportError:
        pass
    for w in weekly_rows[-6:]:
        print(f"  {w['week_label']:14s} games {w['games']:2d}  taker ${w['all_taker_dollars']:>14,.0f}  "
              f"hold {_pct(w['all_hold_incl_fees'])} (incl fees)  pre: actual {_pct(w['pre_hold'])} "
              f"exp {_pct(w['pre_exp_hold'])} luck {w['pre_luck_bps']} bps  public edge {_pct(w['pre_public_edge'])}")


def _pct(x):
    return f"{x:7.2%}" if x is not None else "    n/a"


if __name__ == "__main__":
    main()
