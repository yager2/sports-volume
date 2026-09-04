"""
Build unified_sports_volume.xlsx from unified_sports_volume.csv.

A formatted Excel copy of the daily sheet. Three tabs:
  - "Daily volume"  : one row per day, every venue's contracts + notional, totals.
  - "Parlay share"  : per-venue combo/parlay volume and % (the venues that run one).
  - "Notes"         : the metric definition and per-venue coverage.

Run after unified_sports_volume.py (run_all.py does this automatically).

    python make_excel.py
"""

import csv
import os
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_IN = os.path.join(HERE, "unified_sports_volume.csv")
XLSX_OUT = os.path.join(HERE, "unified_sports_volume.xlsx")

VENUES = [
    ("kalshi", "Kalshi"),
    ("polymarket_us", "Polymarket US"),
    ("novig", "Novig"),
    ("dkex", "DKeX"),
    ("udx", "UDX"),
    ("cdna", "Crypto.com"),
    ("rothera", "Rothera"),
]
# venues that run a combo / parlay product (Kalshi's % is of its sports book,
# not its whole-exchange column - see Notes)
COMBO_VENUES = [
    ("kalshi", "Kalshi"),
    ("novig", "Novig"),
    ("dkex", "DKeX"),
    ("polymarket_us", "Polymarket US"),
    ("udx", "UDX"),
    ("cdna", "Crypto.com"),
]
# venues folded into the "Daily volume" tab's blended Combo % (Kalshi excluded:
# different denominator)
BLEND_VENUES = [v for v in COMBO_VENUES if v[0] != "kalshi"]

NUMFMT = '#,##0'
USDFMT = '$#,##0'
PCTFMT = '0.0%'
HEAD_FILL = PatternFill("solid", fgColor="1F2A37")
GROUP_FONT = Font(color="FFFFFF", bold=True, size=10)
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
KALSHI_FILL = PatternFill("solid", fgColor="EEF3FB")
TOTAL_FILL = PatternFill("solid", fgColor="F2EEDF")
THIN = Side(style="thin", color="D9DEE7")
BORDER = Border(bottom=THIN, right=THIN)


def num(v):
    v = (v or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def _style_block(ws, last_row, ncols, shade_cols):
    for rr in range(3, last_row + 1):
        for cc in range(1, ncols + 1):
            cell = ws.cell(row=rr, column=cc)
            cell.border = BORDER
            fill = shade_cols.get(cc)
            if fill:
                cell.fill = fill
    ws.column_dimensions["A"].width = 12
    for cc in range(2, ncols + 1):
        ws.column_dimensions[get_column_letter(cc)].width = 14
    ws.freeze_panes = "B3"
    ws.auto_filter.ref = f"A2:{get_column_letter(ncols)}{last_row}"


def sheet_daily(wb, rows):
    ws = wb.active
    ws.title = "Daily volume"

    groups = [
        ("", 1),
        ("Contracts traded  (one-sided, $1 face)", len(VENUES) + 1),
        ("Notional $  (~ USD changed hands)", len(VENUES) + 1),
        ("Parlay", 1),
    ]
    c = 1
    for title, span in groups:
        if title:
            ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + span - 1)
            cell = ws.cell(row=1, column=c, value=title)
            cell.fill = HEAD_FILL
            cell.font = GROUP_FONT
            cell.alignment = Alignment(horizontal="center")
        c += span

    headers = (["Date"]
               + [n for _, n in VENUES] + ["Total"]
               + [n for _, n in VENUES] + ["Total"]
               + ["Combo % (blended)"])
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=j, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center" if j > 1 else "left", wrap_text=True)

    r = 3
    for row in rows:
        ws.cell(row=r, column=1, value=row["date"])
        col = 2
        for k, _ in VENUES:
            ws.cell(row=r, column=col, value=num(row.get(f"{k}_contracts"))).number_format = NUMFMT
            col += 1
        ws.cell(row=r, column=col, value=num(row.get("total_contracts"))).number_format = NUMFMT
        col += 1
        for k, _ in VENUES:
            ws.cell(row=r, column=col, value=num(row.get(f"{k}_notional_est"))).number_format = USDFMT
            col += 1
        ws.cell(row=r, column=col, value=num(row.get("total_notional_est"))).number_format = USDFMT
        col += 1
        cn = cd = 0.0
        for k, _ in BLEND_VENUES:
            cp = num(row.get(f"{k}_combo_pct"))
            cc = num(row.get(f"{k}_contracts"))
            if cp is not None and cc:
                cn += cp / 100 * cc
                cd += cc
        ws.cell(row=r, column=col, value=(cn / cd if cd else None)).number_format = PCTFMT
        r += 1

    ncols = len(headers)
    shade = {
        2: KALSHI_FILL, 2 + len(VENUES) + 1: KALSHI_FILL,       # Kalshi cols
        2 + len(VENUES): TOTAL_FILL, 2 + 2 * (len(VENUES) + 1): TOTAL_FILL,  # Totals
    }
    _style_block(ws, r - 1, ncols, shade)
    ws.column_dimensions[get_column_letter(ncols)].width = 16


def sheet_parlay(wb, rows):
    ws = wb.create_sheet("Parlay share")
    nv = len(COMBO_VENUES)

    groups = [("", 1),
              ("Combo / parlay volume  (contracts)", nv),
              ("Combo / parlay share of that venue", nv)]
    c = 1
    for title, span in groups:
        if title:
            ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + span - 1)
            cell = ws.cell(row=1, column=c, value=title)
            cell.fill = HEAD_FILL
            cell.font = GROUP_FONT
            cell.alignment = Alignment(horizontal="center")
        c += span

    headers = ["Date"] + [n for _, n in COMBO_VENUES] + [n for _, n in COMBO_VENUES]
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=j, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center" if j > 1 else "left", wrap_text=True)

    r = 3
    for row in rows:
        ws.cell(row=r, column=1, value=row["date"])
        col = 2
        for k, _ in COMBO_VENUES:
            ws.cell(row=r, column=col, value=num(row.get(f"{k}_combo"))).number_format = NUMFMT
            col += 1
        for k, _ in COMBO_VENUES:
            cp = num(row.get(f"{k}_combo_pct"))
            ws.cell(row=r, column=col,
                    value=(cp / 100 if cp is not None else None)).number_format = PCTFMT
            col += 1
        r += 1

    _style_block(ws, r - 1, len(headers), {})

    note_row = r + 1
    ws.cell(row=note_row, column=1,
            value=("Kalshi = its KXMVE multi-event parlay engine (leg-checked ~100% sports legs "
                   "despite the 'cross-category' name), from a ~3M-trade sample of the public "
                   "feed scaled to the daily total. Its % is of its WHOLE-EXCHANGE volume "
                   "(matching its main column) - ~48% by contracts / ~11% by $. Sample-based, so "
                   "it moves less day to day; not folded into the Daily tab's blended Combo %. "
                   "Rothera runs no on-exchange parlay product and is omitted here."))
    ws.cell(row=note_row, column=1).font = Font(italic=True, size=9, color="666666")


def sheet_notes(wb):
    n = wb.create_sheet("Notes")
    loaded = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        ("Sports prediction-market volume - daily", True),
        (f"Generated {loaded} from unified_sports_volume.csv", False),
        ("", False),
        ("One comparable metric for all seven venues: contracts traded, one-sided", False),
        ("(a buy matched with a sell counts once), each contract a $1-max-payout", False),
        ("instrument. 'Notional $' re-weights each contract by its traded price", False),
        ("(~ the dollars that changed hands, roughly 1/3 of the contract count).", False),
        ("", False),
        ("Kalshi   = WHOLE exchange, every category (daily total from DeFi Rate; the", False),
        ("           last ~2-3 days are projected - see the 'source' column upstream).", False),
        ("Polymarket US = sports, symbol-prefix filtered.", False),
        ("Novig / DKeX / UDX = 100% sports, from their CFTC Part-16 daily reports.", False),
        ("Crypto.com = Nadex / CDNA 'EVENTS' book - Crypto.com's own prediction market", False),
        ("           plus FanDuel Predicts (all its sports flow clears there), minus a", False),
        ("           ~0.2% non-sports tail. Notional $ uses the cross-venue avg price.", False),
        ("Rothera  = the CFTC exchange behind Robinhood's prediction market (Part-16", False),
        ("           daily report). Prefix-filtered to sports (MLB-heavy); a small", False),
        ("           US-macro / elections tail is dropped.", False),
        ("", False),
        ("Combos: tracked per-venue on the 'Parlay share' tab. Novig / DKeX /", False),
        ("        Polymarket US / UDX / Crypto.com from their combo product line", False),
        ("        (% of that venue's book). Kalshi = its KXMVE multi-event parlay", False),
        ("        engine (leg-checked ~100% sports legs), from a ~3M-trade sample", False),
        ("        scaled to the daily total, as a % of its WHOLE-EXCHANGE volume", False),
        ("        (matching its main column) - ~48% by contracts / ~11% by $. The", False),
        ("        Daily tab's blended Combo % is the five report-based venues only;", False),
        ("        Rothera has no on-exchange parlay product.", False),
        ("", False),
        ("All seven US CFTC sports-prediction venues are covered.", False),
        ("Full detail: VOLUME_METHODOLOGY.md", False),
    ]
    for i, (text, bold) in enumerate(lines, start=1):
        cell = n.cell(row=i, column=1, value=text)
        if bold:
            cell.font = Font(bold=True, size=12)
    n.column_dimensions["A"].width = 92


def main():
    if not os.path.exists(CSV_IN):
        raise SystemExit("unified_sports_volume.csv not found - run unified_sports_volume.py first.")
    with open(CSV_IN, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    wb = Workbook()
    sheet_daily(wb, rows)
    sheet_parlay(wb, rows)
    sheet_notes(wb)
    wb.save(XLSX_OUT)
    print(f"wrote {XLSX_OUT}  ({len(rows)} days, through {rows[-1]['date']})")


if __name__ == "__main__":
    main()
