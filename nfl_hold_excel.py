"""
Build nfl_hold.xlsx - the formatted workbook for the Kalshi NFL taker-hold tracker.

Tabs:
  - "Dashboard"          season-to-date by product + the last 6 weeks, green/red
  - "Week vs week"        Week N of each season side by side (the comp view)
  - "Weekly"             one row per NFL week (hold, expected, luck, outcome factors)
  - "By game"            one row per game
  - "By game x product"  one row per game and product
  - "By ticket size"     hold by taker $-per-trade bucket, per season and phase
  - "Parlays"            weekly NFL parlay hold (all-NFL and NFL+other), by legs
  - "Sportsbook compare" Kalshi hold by calendar month next to blank columns
                         for state-reported sportsbook football hold (paste in)
  - "Charts"             native Excel charts: weekly actual vs expected, luck
  - "Notes"              definitions, coverage, caveats

Called by nfl_hold_report.py (which passes the rows it already computed).
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX_OUT = os.path.join(HERE, "nfl_hold.xlsx")

HEAD_FILL = PatternFill("solid", fgColor="1F2A37")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
GREEN = PatternFill("solid", fgColor="DCF2DF")
RED = PatternFill("solid", fgColor="F8DEDD")
INPUT_FILL = PatternFill("solid", fgColor="FFF7D6")
PCT, PCT2, USD, INT = "0.0%", "0.00%", '"$"#,##0', "#,##0"
PNAME = {"moneyline": "Moneyline", "spread": "Spread", "total": "Total", "player_prop": "Player props",
         "period": "Halves/quarters", "game_prop": "Other game props"}
# States whose monthly reports break out football (or NFL) handle and hold.
STATES = ["NJ", "PA", "NY", "IL", "MD", "CO"]


def _header(ws, row, cols, widths=None):
    for c, name in enumerate(cols, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        if widths:
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1] if c - 1 < len(widths) else 13
    ws.row_dimensions[row].height = 30


def _table(ws, rows, spec, start_row=1, green_red=()):
    """spec: [(header, key, fmt, width)]; green_red: headers where + is green (house won)."""
    _header(ws, start_row, [s[0] for s in spec], [s[3] for s in spec])
    for i, r in enumerate(rows, start_row + 1):
        for c, (_, key, fmt, _) in enumerate(spec, 1):
            v = key(r) if callable(key) else r.get(key)
            cell = ws.cell(row=i, column=c, value=v)
            if fmt:
                cell.number_format = fmt
    last = start_row + len(rows)
    for c, (h, *_rest) in enumerate(spec, 1):
        if h in green_red and rows:
            col = get_column_letter(c)
            rng = f"{col}{start_row + 1}:{col}{last}"
            ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
            ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], fill=RED))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=3)
    return last


def _sum(games, phase=None, product=None, cols=None):
    out = dict.fromkeys(cols, 0.0)
    for g in games:
        for k, v in g["p"].items():
            p, ph = k.split("|")
            if (product and p != product) or (phase and ph != phase):
                continue
            for i, c in enumerate(cols):
                out[c] += v[i]
    n, en = out["notional"], out["exp_notional"]
    out["hold"] = -out["pnl"] / n if n else None
    out["hold_f"] = -(out["pnl"] - out["taker_fee"]) / n if n else None
    out["exp"] = -out["exp_pnl"] / en if en else None
    out["luck"] = (out["hold"] - out["exp"]) * 1e4 if out["hold"] is not None and out["exp"] is not None else None
    out["pub"] = (out["pub_hit"] - out["pub_q"]) / out["pub_w"] if out["pub_w"] else None
    return out


# Headline: hold incl. fees; + = takers (customers) lost = house won -> green.
WEEK_SPEC = [
    ("Week", "week_label", None, 14), ("Games", "games", INT, 7),
    ("First date", "first_date", None, 11),
    ("Taker $ (all)", "all_taker_dollars", USD, 15), ("Hold incl. fees (all)", "all_hold_incl_fees", PCT2, 11),
    ("Hold (all)", "all_hold", PCT2, 10), ("Taker P&L (all)", "all_taker_pnl", USD, 14),
    ("Pre-game taker $", "pre_taker_dollars", USD, 14), ("Pre-game hold", "pre_hold", PCT2, 10),
    ("Pre-game expected hold", "pre_exp_hold", PCT2, 11), ("Luck (bps)", "pre_luck_bps", INT, 9),
    ("Public side vs price (pts)", lambda r: None if r.get("pre_public_edge") is None else round(r["pre_public_edge"] * 100, 2), "0.00", 12),
    ("In-game taker $", "live_taker_dollars", USD, 14), ("In-game hold", "live_hold", PCT2, 10),
    ("Kalshi fees", "all_kalshi_fees", USD, 12),
    ("Fav win rate", "fav_win_rate", PCT, 9), ("Fav implied", "fav_implied", PCT, 9),
    ("Fav cover rate", "fav_cover_rate", PCT, 9), ("Over rate", "over_rate", PCT, 9),
    ("Pts vs closing total", "pts_vs_total_line", "0.0", 10),
    ("Parlay $ (est.)", "parlay_taker_dollars_est", USD, 14), ("Parlay hold incl. fees", "parlay_hold_incl_fees", PCT2, 11),
    ("Parlay hit rate", "parlay_hit_rate", PCT2, 9),
    ("Void $ (excluded)", "void_dollars", USD, 12), ("Block $ (excluded)", "block_dollars", USD, 12),
]
GREEN_RED = {"Parlay hold incl. fees", "Hold incl. fees (all)", "Hold (all)", "Pre-game hold", "In-game hold", "Luck (bps)", "Hold incl. fees", "Hold"}


def sheet_dashboard(wb, summary, weekly_rows):
    ws = wb.active
    ws.title = "Dashboard"
    ws["A1"] = "Kalshi NFL taker hold - a same-week stand-in for sportsbook hold"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = ("Hold = share of taker $ that takers lost (like a book's hold). Green = house won, red = takers won. "
                f"Regular season + playoffs only. Updated {summary['generated_utc']}.")
    ws["A2"].font = Font(italic=True, size=9, color="666666")
    cols = summary["prod_cols"]
    games = [g for g in summary["games"] if g["season_type"] != "pre"]
    seasons = sorted({g["season"] for g in games}, reverse=True)
    r = 4
    for season in seasons:
        gs = [g for g in games if g["season"] == season]
        rows = []
        for p in list(PNAME) + [None]:
            a, pre, live = _sum(gs, None, p, cols), _sum(gs, "pre", p, cols), _sum(gs, "live", p, cols)
            if not a["notional"]:
                continue
            rows.append({"product": PNAME.get(p, "ALL STRAIGHTS"), "n": a["notional"], "hf": a["hold_f"], "h": a["hold"],
                         "pre_h": pre["hold"], "exp": pre["exp"], "luck": pre["luck"], "live_h": live["hold"],
                         "pub": None if pre["pub"] is None else pre["pub"] * 100, "fees": a["taker_fee"] + a["maker_fee"]})
        ws.cell(row=r, column=1, value=f"Season {season} to date ({len(gs)} games)").font = Font(bold=True, size=11)
        r = _table(ws, rows, [
            ("Product", "product", None, 18), ("Taker $", "n", USD, 16), ("Hold incl. fees", "hf", PCT2, 11),
            ("Hold", "h", PCT2, 10), ("Pre-game hold", "pre_h", PCT2, 10), ("Pre-game expected", "exp", PCT2, 10),
            ("Luck (bps)", "luck", INT, 9), ("In-game hold", "live_h", PCT2, 10),
            ("Public side vs price (pts)", "pub", "0.00", 12), ("Kalshi fees", "fees", USD, 13),
        ], start_row=r + 1, green_red=GREEN_RED | {"Pre-game hold"}) + 2
    ws.cell(row=r, column=1, value="Last 6 weeks").font = Font(bold=True, size=11)
    recent = [w for w in weekly_rows if w["season_type"] != "pre"][-6:]
    _table(ws, list(reversed(recent)), WEEK_SPEC[:12], start_row=r + 1, green_red=GREEN_RED)
    ws.freeze_panes = None


def sheet_weekly(wb, weekly_rows):
    _table(wb.create_sheet("Weekly"), weekly_rows, WEEK_SPEC, green_red=GREEN_RED)


def sheet_games(wb, game_rows):
    spec = [("Date", "date", None, 11), ("Game", lambda r: f"{r['away']} @ {r['home']}", None, 12),
            ("Week", "week_label", None, 13),
            ("Score", lambda r: f"{r['away_score']}-{r['home_score']}" if r.get("away_score") is not None else None, None, 8)] + [
        s for s in WEEK_SPEC if s[0] not in ("Week", "Games", "First date", "Fav win rate", "Fav implied",
                                              "Fav cover rate", "Over rate", "Pts vs closing total")] + [
        ("Closing fav", "fav", None, 8), ("Fav prob", "fav_prob", PCT, 8), ("Fav won", "fav_won", "0.0", 7),
        ("Fav spread line", "spread_line", "0.0", 8), ("Fav margin", "fav_margin", INT, 7), ("Fav covered", "fav_covered", "0", 7),
        ("Total line", "total_line", "0.0", 7), ("Total pts", "total_pts", INT, 7), ("Over", "over_hit", "0", 6),
        ("Complete", "complete", None, 8)]
    _table(wb.create_sheet("By game"), game_rows, spec, green_red=GREEN_RED)


def sheet_game_product(wb, gp_rows):
    spec = [("Date", "date", None, 11), ("Game", lambda r: f"{r['away']} @ {r['home']}", None, 12),
            ("Product", lambda r: PNAME.get(r["product"], r["product"]), None, 15)] + [
        s for s in WEEK_SPEC if s[0] not in ("Week", "Games", "First date", "Fav win rate", "Fav implied",
                                              "Fav cover rate", "Over rate", "Pts vs closing total")]
    _table(wb.create_sheet("By game x product"), gp_rows, spec, green_red=GREEN_RED)


def sheet_buckets(wb, summary):
    labels = {"a:<$25": "< $25", "b:$25-100": "$25-100", "c:$100-1k": "$100-1k", "d:>$1k": "> $1k"}
    acc = defaultdict(lambda: [0.0, 0.0, 0.0])
    for g in summary["games"]:
        if g["season_type"] == "pre":
            continue
        for k, v in g["b"].items():
            ph, b = k.split("|")
            for key in ((g["season"], ph, b), (g["season"], "all", b)):
                for i in range(3):
                    acc[key][i] += v[i]
    rows = [{"season": s, "phase": {"pre": "Pre-game", "live": "In-game", "all": "All"}[ph], "bucket": labels[b],
             "n": d, "pnl": pnl, "hf": -(pnl - f) / d if d else None, "h": -pnl / d if d else None}
            for (s, ph, b), (d, pnl, f) in sorted(acc.items(), key=lambda kv: (-kv[0][0], kv[0][1], kv[0][2]))]
    _table(wb.create_sheet("By ticket size"), rows, [
        ("Season", "season", None, 8), ("Bets", "phase", None, 10), ("Taker $ per trade", "bucket", None, 12),
        ("Taker $", "n", USD, 16), ("Taker P&L", "pnl", USD, 14), ("Hold incl. fees", "hf", PCT2, 11), ("Hold", "h", PCT2, 10),
    ], green_red=GREEN_RED)


def sheet_parlays(wb, summary):
    ws = wb.create_sheet("Parlays")
    ws["A1"] = ("Kalshi parlays with NFL legs. $ are ESTIMATES: 10 of every 60 minutes of the trade feed are "
                "sampled and scaled x6; hold % is computed directly from the sampled trades. "
                "'nfl' = every leg NFL; 'nfl_mixed' = some legs from other sports. Only fully-settled days count.")
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    rows = sorted(summary.get("parlays") or [], key=lambda r: (r["category"], r["legs"] != "all"))
    _table(ws, rows, [
        ("Week", "week_label", None, 14), ("Type", "category", None, 10), ("Legs", "legs", None, 7),
        ("Parlay stakes (est.)", "taker_dollars_est", USD, 15), ("Cash-out $ (est.)", "cashout_dollars_est", USD, 13), ("Hold incl. fees", "hold_incl_fees", PCT2, 11),
        ("Hold", "hold", PCT2, 10), ("Fair hold if legs independent", "exp_hold_indep_legs", PCT2, 13),
        ("Hit rate", "hit_rate", PCT2, 9), ("Parlays sampled", "parlays_sampled", INT, 10),
        ("Trades sampled", "sampled_trades", INT, 10), ("Pre-game share", "pre_share", PCT, 9),
    ], start_row=3, green_red=GREEN_RED | {"Hold incl. fees"})


def sheet_compare(wb, summary, pasted: dict):
    """Monthly Kalshi hold next to user-pasted state sportsbook football hold."""
    ws = wb.create_sheet("Sportsbook compare")
    ws["A1"] = ("Paste each state's monthly FOOTBALL (or NFL) sportsbook hold % into the yellow cells "
                "(e.g. 7.5% as 7.5%). They are kept across refreshes. Kalshi columns update automatically.")
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    cols = summary["prod_cols"]
    months = defaultdict(list)
    for g in summary["games"]:
        if g["season_type"] != "pre":
            months[g["month"]].append(g)
    heads = ["Month", "Games", "Kalshi taker $", "Kalshi hold incl. fees", "Kalshi pre-game hold",
             "Kalshi pre-game expected", "Luck (bps)"] + [f"{s} book hold" for s in STATES] + ["State avg", "Kalshi minus state avg"]
    _header(ws, 3, heads, [10, 7, 16, 11, 11, 11, 9] + [10] * len(STATES) + [10, 12])
    for i, m in enumerate(sorted(months), 4):
        a, pre = _sum(months[m], None, None, cols), _sum(months[m], "pre", None, cols)
        vals = [m, len(months[m]), a["notional"], a["hold_f"], pre["hold"], pre["exp"], pre["luck"]]
        fmts = [None, INT, USD, PCT2, PCT2, PCT2, INT]
        for c, (v, f) in enumerate(zip(vals, fmts), 1):
            cell = ws.cell(row=i, column=c, value=v)
            if f:
                cell.number_format = f
        first_state = len(vals) + 1
        for j, s in enumerate(STATES):
            cell = ws.cell(row=i, column=first_state + j, value=pasted.get((m, s)))
            cell.fill, cell.number_format = INPUT_FILL, PCT2
        s0, s1 = get_column_letter(first_state), get_column_letter(first_state + len(STATES) - 1)
        avg = ws.cell(row=i, column=first_state + len(STATES), value=f'=IFERROR(AVERAGE({s0}{i}:{s1}{i}),"")')
        avg.number_format = PCT2
        diff = ws.cell(row=i, column=first_state + len(STATES) + 1,
                       value=f'=IF({get_column_letter(first_state + len(STATES))}{i}="","",D{i}-{get_column_letter(first_state + len(STATES))}{i})')
        diff.number_format = PCT2
    ws.freeze_panes = "B4"


def _read_pasted() -> dict:
    """Keep whatever the user typed into the yellow cells on the last build."""
    if not os.path.exists(XLSX_OUT):
        return {}
    try:
        ws = load_workbook(XLSX_OUT, data_only=False)["Sportsbook compare"]
    except (KeyError, OSError, ValueError):
        return {}
    heads = [c.value for c in ws[3]]
    out = {}
    for row in ws.iter_rows(min_row=4, values_only=True):
        for s in STATES:
            h = f"{s} book hold"
            if h in heads and row[heads.index(h)] not in (None, ""):
                out[(row[0], s)] = row[heads.index(h)]
    return out


def sheet_week_compare(wb, summary):
    """Week N of each season side by side - the comp view."""
    ws = wb.create_sheet("Week vs week", 1)
    seasons = summary.get("compare_seasons") or []
    ws["A1"] = ("Same NFL week, season by season. Hold = share of taker $ that takers lost (incl. fees); "
                "green = house won. Luck = pre-game results luck in bps. Parlay $ are sampled estimates; "
                "Kalshi parlays started in Week 4 of 2025.")
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    metrics = [("Taker $", "taker_dollars", USD, 14), ("Hold incl. fees", "hold_incl_fees", PCT2, 10),
               ("Luck (bps)", "luck_bps", INT, 8), ("Public side (pts)", "public_edge", "0.0", 9),
               ("Parlay $ (est.)", "parlay_dollars", USD, 13), ("Parlay hold", "parlay_hold", PCT2, 10)]
    # two header rows: season group, then metric
    ws.cell(row=3, column=1, value="Week").fill = HEAD_FILL
    ws.cell(row=3, column=1).font = HEAD_FONT
    ws.cell(row=4, column=1).fill = HEAD_FILL
    ws.column_dimensions["A"].width = 12
    col = 2
    for season in seasons:
        ws.merge_cells(start_row=3, start_column=col, end_row=3, end_column=col + len(metrics) - 1)
        c = ws.cell(row=3, column=col, value=f"{season} season")
        c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, Alignment(horizontal="center")
        for j, (name, _, _, width) in enumerate(metrics):
            h = ws.cell(row=4, column=col + j, value=name)
            h.fill, h.font = HEAD_FILL, HEAD_FONT
            h.alignment = Alignment(wrap_text=True)
            ws.column_dimensions[get_column_letter(col + j)].width = width
        col += len(metrics)
    ws.row_dimensions[4].height = 30
    for i, r in enumerate(summary.get("week_compare") or [], 5):
        ws.cell(row=i, column=1, value=r["week"]).font = Font(bold=True, size=10)
        col = 2
        for season in seasons:
            w = r.get(str(season))
            for j, (_, key, fmt, _) in enumerate(metrics):
                v = None if w is None else w.get(key)
                if key == "public_edge" and v is not None:
                    v = round(v * 100, 1)
                cell = ws.cell(row=i, column=col + j, value=v)
                cell.number_format = fmt
            col += len(metrics)
    last = 4 + len(summary.get("week_compare") or [])
    col = 2
    for season in seasons:
        for j, (_, key, _, _) in enumerate(metrics):
            if key in ("hold_incl_fees", "luck_bps", "parlay_hold"):
                L = get_column_letter(col + j)
                rng = f"{L}5:{L}{last}"
                ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
                ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], fill=RED))
        col += len(metrics)
    ws.freeze_panes = "B5"


def sheet_charts(wb, weekly_rows):
    ws = wb.create_sheet("Charts")
    rows = [w for w in weekly_rows if w["season_type"] != "pre"]
    _header(ws, 1, ["Week", "Pre-game actual hold", "Pre-game expected hold", "Luck (bps)", "Hold incl. fees (all)"], [14, 12, 12, 10, 12])
    for i, w in enumerate(rows, 2):
        for c, v in enumerate([w["week_label"], w["pre_hold"], w["pre_exp_hold"], w["pre_luck_bps"], w["all_hold_incl_fees"]], 1):
            ws.cell(row=i, column=c, value=v).number_format = PCT2 if c in (2, 3, 5) else "0"
    n = len(rows) + 1
    if len(rows) < 1:
        return
    lc = LineChart()
    lc.title, lc.height, lc.width = "Actual vs expected taker hold (pre-game)", 8, 18
    lc.y_axis.number_format, lc.y_axis.title = "0%", "Hold"
    lc.add_data(Reference(ws, min_col=2, max_col=3, min_row=1, max_row=n), titles_from_data=True)
    lc.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n))
    ws.add_chart(lc, "G2")
    bc = BarChart()
    bc.title, bc.height, bc.width = "Results luck by week (bps; + = house lucky)", 8, 18
    bc.add_data(Reference(ws, min_col=4, min_row=1, max_row=n), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n))
    ws.add_chart(bc, "G19")
    hc = BarChart()
    hc.title, hc.height, hc.width = "Taker hold incl. fees by week (all straights)", 8, 18
    hc.y_axis.number_format = "0%"
    hc.add_data(Reference(ws, min_col=5, min_row=1, max_row=n), titles_from_data=True)
    hc.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n))
    ws.add_chart(hc, "G36")


def sheet_notes(wb, summary, game_rows):
    ws = wb.create_sheet("Notes")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 100
    firsts = defaultdict(str)
    for g in game_rows:
        key = f"{g['season']} {g['season_type']}"
        firsts[key] = min(firsts[key] or g["date"], g["date"])
    incomplete = [g["game"] for g in game_rows if not g.get("complete")]
    lines = [
        ("Data pulled", summary["generated_utc"]),
        ("Source", "Kalshi public trade tape (every trade in every NFL game market) + ESPN scoreboard for kickoff, score, week."),
        ("Games", f"{len(game_rows)} ({', '.join(f'{k}: from {v}' for k, v in sorted(firsts.items()))})"),
        ("Earliest Kalshi NFL game market", min((g["date"] for g in game_rows), default="-")),
        ("Taker", "The side of each trade that crossed the spread. Treated as the 'customer'."),
        ("Hold incl. fees (headline)", "-(taker P&L - taker fees) / taker $. The customer's all-in loss rate: the closest match to a sportsbook's hold."),
        ("Expected hold", "Same, but paying out at the closing price (VWAP of the last 30 pre-kickoff minutes, else last trade; two-way/three-way winner markets normalised to sum to 100%). Pre-game trades only."),
        ("Luck (bps)", "(actual - expected pre-game hold) x 10,000. + = results broke for the house."),
        ("Public side vs price", "For each pre-game market: the side takers net-bought, weighted by net $; its win rate minus its closing-implied probability, in points. + = the public's picks beat their prices (bad for books, worst for parlays)."),
        ("Products", "Moneyline, spread, total, player props (TD scorer, yards, receptions, ladders...), halves/quarters, other game props (overtime, win margin, team totals...). Season-long futures excluded; parlays (KXNFLCOMBO / KXMVE*) are a separate later phase."),
        ("Excluded from hold", "Block trades, and markets settled at a 'fair price' (inactive player, postponement) - the way a book refunds a void. Their $ are shown in the Void/Block columns."),
        ("Fees", "Kalshi's published formula per series: taker 0.07 x mult x C x P x (1-P), makers 25% of that on 'with maker fees' series. Computed per fill without Kalshi's per-order rounding up, so fees run very slightly low."),
        ("Ticket size", "Taker $ per trade. One large order that fills against several resting orders shows up as several smaller trades, so buckets skew small."),
        ("Preseason", "Stored but excluded from every tab except By game / By game x product / Weekly."),
        ("Incomplete games", ", ".join(incomplete) if incomplete else "none (every market in every game is settled)"),
        ("Caveat", "Kalshi's NFL mix is not a sportsbook's: most taker $ is in-game, parlays are tracked separately, and makers include professional market makers. Use for direction and size of swings, not exact level."),
    ]
    for i, (k, v) in enumerate(lines, 1):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True, size=10)
        c = ws.cell(row=i, column=2, value=v)
        c.alignment = Alignment(wrap_text=True, vertical="top")


def build(game_rows, gp_rows, weekly_rows, summary):
    pasted = _read_pasted()
    wb = Workbook()
    sheet_dashboard(wb, summary, weekly_rows)
    sheet_weekly(wb, weekly_rows)
    sheet_games(wb, game_rows)
    sheet_game_product(wb, gp_rows)
    sheet_buckets(wb, summary)
    sheet_parlays(wb, summary)
    sheet_week_compare(wb, summary)
    sheet_compare(wb, summary, pasted)
    sheet_charts(wb, weekly_rows)
    sheet_notes(wb, summary, game_rows)
    tmp = XLSX_OUT + ".tmp.xlsx"
    wb.save(tmp)
    try:
        os.replace(tmp, XLSX_OUT)
    except PermissionError:
        print(f"  nfl_hold.xlsx is open in Excel - wrote {os.path.basename(tmp)} instead")
