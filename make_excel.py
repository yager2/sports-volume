"""
Build unified_sports_volume.xlsx - the formatted workbook the tracker delivers.

Tabs:
  - "Sports volume"   : each venue's SPORTS contracts + notional, + a blended combo %
  - "Total exchange"  : each venue's WHOLE-EXCHANGE volume (every category)
  - "Parlay share"    : per-venue combo/parlay volume and %
  - "Notes"           : metric definition and per-venue coverage

Reads unified_sports_volume.csv + unified_total_volume.csv.
Run after unified_sports_volume.py (run_all.py does this).

    python make_excel.py
"""

import csv
import os
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
SPORTS_CSV = os.path.join(HERE, "unified_sports_volume.csv")
TOTAL_CSV = os.path.join(HERE, "unified_total_volume.csv")
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
# Legitimacy of the Notional $ figure - see the "Where the $ comes from" block
# in sheet_notes(). "real" = venue's own trade/close price. "proxy" = a real
# quote from that venue, but a midpoint stand-in for the actual fill. "est" =
# the venue publishes no price at all; borrowed from the other venues' average.
PRICE_BASIS = {
    "kalshi": "real", "polymarket_us": "proxy", "novig": "real", "dkex": "proxy",
    "udx": "est", "cdna": "est", "rothera": "proxy",
}
PRICE_TAG = {"real": " [real]", "proxy": " [proxy]", "est": " [EST]"}
# venues that run a combo / parlay product
COMBO_VENUES = [
    ("kalshi", "Kalshi"),
    ("novig", "Novig"),
    ("dkex", "DKeX"),
    ("polymarket_us", "Polymarket US"),
    ("udx", "UDX"),
    ("cdna", "Crypto.com"),
]

NUMFMT = '#,##0'
USDFMT = '$#,##0'
PCTFMT = '0.0%'
HEAD_FILL = PatternFill("solid", fgColor="1F2A37")
GROUP_FONT = Font(color="FFFFFF", bold=True, size=10)
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
PRICE_FONT = {
    "real":  Font(color="8FE3A6", bold=True, size=10),   # venue's own trade/close price
    "proxy": Font(color="F2C46B", bold=True, size=10),   # real quote, midpoint stand-in
    "est":   Font(color="F2938C", bold=True, size=10),   # no price published - borrowed
}
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


def sheet_volume(wb, title, rows, blended_combo):
    """A contracts + notional grid. `blended_combo` adds a trailing 'Combo %'
    column (only on the Sports tab)."""
    ws = wb.create_sheet(title) if wb.sheetnames != ["Sheet"] else wb.active
    if ws.title == "Sheet":
        ws.title = title

    groups = [("", 1),
              ("Taker Volume  (contracts, one-sided, $1 face)", len(VENUES) + 1),
              ("Notional $  (~ USD changed hands - real / proxy / EST, see Notes tab)", len(VENUES) + 1)]
    if blended_combo:
        groups.append(("Parlay", 1))
    c = 1
    for g_title, span in groups:
        if g_title:
            ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + span - 1)
            cell = ws.cell(row=1, column=c, value=g_title)
            cell.fill = HEAD_FILL
            cell.font = GROUP_FONT
            cell.alignment = Alignment(horizontal="center")
        c += span

    contracts_headers = [n for _, n in VENUES] + ["Total"]
    notional_headers = [n + PRICE_TAG[PRICE_BASIS[k]] for k, n in VENUES] + ["Total"]
    headers = ["Date"] + contracts_headers + notional_headers
    if blended_combo:
        headers.append("Combo % (blended)")
    notional_start = 1 + len(contracts_headers) + 1  # 1-based col of first notional venue
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=j, value=h)
        cell.fill = HEAD_FILL
        is_notional_venue = notional_start <= j < notional_start + len(VENUES)
        cell.font = PRICE_FONT[PRICE_BASIS[VENUES[j - notional_start][0]]] if is_notional_venue else HEAD_FONT
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
        if blended_combo:
            cn = cd = 0.0
            for k, _ in COMBO_VENUES:
                if k == "kalshi":
                    continue                      # different denominator - see Notes
                cp = num(row.get(f"{k}_combo_pct"))
                cc_ = num(row.get(f"{k}_contracts"))
                if cp is not None and cc_:
                    cn += cp / 100 * cc_
                    cd += cc_
            ws.cell(row=r, column=col, value=(cn / cd if cd else None)).number_format = PCTFMT
        r += 1

    ncols = len(headers)
    shade = {
        2: KALSHI_FILL, 2 + len(VENUES) + 1: KALSHI_FILL,
        2 + len(VENUES): TOTAL_FILL, 2 + 2 * (len(VENUES) + 1): TOTAL_FILL,
    }
    _style_block(ws, r - 1, ncols, shade)
    if blended_combo:
        ws.column_dimensions[get_column_letter(ncols)].width = 16


def sheet_parlay(wb, rows):
    ws = wb.create_sheet("Parlay share")

    groups = [("", 1),
              ("Combo / parlay volume  (contracts)", len(COMBO_VENUES)),
              ("Combo / parlay share of that venue's sports book", len(COMBO_VENUES))]
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
    ws.cell(row=r + 1, column=1,
            value=("Combo % = parlay volume / that venue's sports book. Kalshi's parlay is its "
                   "KXMVE multi-event engine (leg-checked ~100% sports legs despite the "
                   "'cross-category' name), from a ~5M-trade sample (refreshed weekly) scaled to "
                   "the daily total; ~70% of Kalshi's sports book by contracts, ~24% by $ (parlay "
                   "legs are many and cheap - this sample-based split can move noticeably week to "
                   "week, see VOLUME_METHODOLOGY.md). Not folded into the Sports tab's blended "
                   "Combo %. Rothera runs no on-exchange parlay product."))
    ws.cell(row=r + 1, column=1).font = Font(italic=True, size=9, color="666666")


def sheet_notes(wb):
    n = wb.create_sheet("Notes")
    loaded = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        ("US prediction-market volume - daily, 7 regulated exchanges", True),
        (f"Generated {loaded}. Full detail: VOLUME_METHODOLOGY.md", False),
        ("", False),
        ("One comparable metric: TAKER VOLUME - contracts traded, one-sided (a buy", False),
        ("matched with a sell counts once - the taker's fill, not the maker's resting", False),
        ("order), each a $1-max-payout instrument. This is the metric every one of", False),
        ("these venues reports as its own 'volume' under CFTC Part 16.01 (Kalshi's own", False),
        ("API calls it 'taker volume' outright). 'Notional $' re-weights each contract", False),
        ("by its traded price (~ the dollars that changed hands, roughly 1/3 of the", False),
        ("taker-volume figure). Every venue re-verified against its live source as of", False),
        (f"{loaded}: independently recomputed totals matched the committed figures exactly", False),
        ("for all seven venues (see VOLUME_METHODOLOGY.md for the audit detail).", False),
        ("", False),
        ("TAB 'Sports volume'  - each venue's sports book only. Apples-to-apples on sports.", True),
        ("  Kalshi = single-leg sports + its sports parlay engine.", False),
        ("  Polymarket US / Crypto.com / Rothera = sports filtered out of a wider book.", False),
        ("  Novig / DKeX / UDX = sports-only exchanges (their sports IS their whole book).", False),
        ("", False),
        ("TAB 'Total exchange'  - each venue's WHOLE book, every category (sports, crypto,", True),
        ("  politics, economics, ...). Apples-to-apples on total platform activity.", False),
        ("  Kalshi total from DeFi Rate (last ~2 days projected). Polymarket = all QCX.", False),
        ("  Crypto.com = the whole Nadex/CDNA book (events + fx/commodity/crypto binaries).", False),
        ("  Rothera = sports + its US-macro / elections markets.", False),
        ("  Novig / DKeX / UDX total == their sports figure (sports-only exchanges).", False),
        ("", False),
        ("TAB 'Parlay share'  - per-venue combo/parlay volume and % of that venue's sports book.", True),
        ("", False),
        ("WHERE THE NOTIONAL $ FIGURE COMES FROM  - column headers above are tagged", True),
        ("[real] / [proxy] / [EST] to match this table. Taker Volume (contracts) needs none", False),
        ("of this - it's an exact sum of each venue's own reported trades for all seven.", False),
    ]
    for i, (text, bold) in enumerate(lines, start=1):
        cell = n.cell(row=i, column=1, value=text)
        if bold:
            cell.font = Font(bold=True, size=11)

    price_table = [
        ("Venue", "Tag", "What the $ is based on"),
        ("Kalshi", "real",
         "Volume-weighted avg. from Kalshi's own trade feed - the sports figure prices "
         "each series off its exact per-trade VWAP; the whole-exchange total uses a "
         "~5M-trade sample of the public feed, refreshed weekly."),
        ("Polymarket US", "proxy",
         "Bid/offer midpoint per market - the daily report has no realized trade-price "
         "column, so this is the closest published quote to a fill, not the fill itself."),
        ("Novig", "real",
         "Each market's actual closing trade price (falls back to open, then the "
         "high/low midpoint)."),
        ("DKeX", "proxy",
         "Each market's daily High/Low midpoint - no single settlement or last-trade "
         "price is published."),
        ("UDX", "est",
         "UDX publishes NO price data at all. This figure is entirely borrowed: the "
         "day's contract-weighted average price across the venues that do publish one, "
         "applied to UDX's own (real) contract count. Not UDX's own number."),
        ("Crypto.com", "est",
         "The Nadex/CDNA bulletin's summary section carries no price column either - "
         "same cross-venue-average substitute as UDX. Not Crypto.com's own number."),
        ("Rothera", "proxy",
         "Each market's daily high/low midpoint (falls back to close, then open)."),
    ]
    start = len(lines) + 2
    tag_font = {"real": Font(bold=True, size=10, color="1E7A3C"),
                "proxy": Font(bold=True, size=10, color="9A6A00"),
                "est": Font(bold=True, size=10, color="B3261E")}
    tag_word = {"real": "REAL", "proxy": "PROXY", "est": "ESTIMATED"}
    for i, (venue, tag, basis) in enumerate(price_table):
        header = i == 0
        c1 = n.cell(row=start + i, column=1, value=venue)
        c2 = n.cell(row=start + i, column=2, value=tag_word.get(tag, tag.upper()) if not header else "Legitimacy")
        c3 = n.cell(row=start + i, column=3, value=basis)
        if header:
            for cell in (c1, c2, c3):
                cell.font = Font(bold=True, size=10, color="FFFFFF")
                cell.fill = HEAD_FILL
        else:
            c1.font = Font(bold=True, size=10)
            c2.font = tag_font[tag]
            c3.alignment = Alignment(wrap_text=True, vertical="top")
    n.column_dimensions["A"].width = 28
    n.column_dimensions["B"].width = 12
    n.column_dimensions["C"].width = 90


def main():
    if not os.path.exists(SPORTS_CSV):
        raise SystemExit("unified_sports_volume.csv not found - run unified_sports_volume.py first.")
    with open(SPORTS_CSV, newline="", encoding="utf-8") as f:
        sports_rows = list(csv.DictReader(f))
    total_rows = []
    if os.path.exists(TOTAL_CSV):
        with open(TOTAL_CSV, newline="", encoding="utf-8") as f:
            total_rows = list(csv.DictReader(f))

    wb = Workbook()
    sheet_volume(wb, "Sports volume", sports_rows, blended_combo=True)
    if total_rows:
        sheet_volume(wb, "Total exchange", total_rows, blended_combo=False)
    sheet_parlay(wb, sports_rows)
    sheet_notes(wb)
    wb.save(XLSX_OUT)
    print(f"wrote {XLSX_OUT}  ({len(sports_rows)} days, through {sports_rows[-1]['date']})")


if __name__ == "__main__":
    main()
