"""
Unified daily volume comparison across the seven regulated US prediction-market
exchanges. Produces two aligned sheets:

  unified_sports_volume.csv  - each venue's SPORTS volume + parlay/combo share.
                               Apples-to-apples on sports.
  unified_total_volume.csv   - each venue's WHOLE-EXCHANGE volume (every category
                               - sports, crypto, politics, econ, ...). Apples-to-
                               apples on total platform activity.

Source files (from the per-venue pullers):
    kalshi_total_volume.py         -> kalshi_daily_total.csv        (whole exchange)
                                      kalshi_sports_daily_totals.csv (sports single-leg)
                                      kalshi_daily_by_category.csv   ("Parlay" row)
    novig_daily_volume.py          -> novig_daily_totals.csv        (100% sports)
    dkex_daily_volume.py           -> dkex_daily_totals.csv         (100% sports)
    polymarket_us_daily_volume.py  -> polymarket_us_daily_totals.csv (sports + all_volume)
    udx_daily_volume.py            -> udx_daily_totals.csv          (100% sports)
    cdna_daily_volume.py           -> cdna_daily_totals.csv         (sports EVENTS + all_volume)
    rothera_daily_volume.py        -> rothera_daily_totals.csv      (sports + all_volume)

Novig / DKeX / UDX are sports-only exchanges, so their sports and total figures
are identical.

Two measures per venue (see VOLUME_METHODOLOGY.md):
  *_contracts    - contracts traded, one-sided, $1 face. What the venues report.
  *_notional_est - contracts x traded-price ~= USD that changed hands. UDX and
                   CDNA have no usable price column, so theirs is estimated with
                   the day's contract-weighted average price across the venues
                   that do report prices.
"""

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SPORTS_CSV = os.path.join(HERE, "unified_sports_volume.csv")
TOTAL_CSV = os.path.join(HERE, "unified_total_volume.csv")

VENUES = ["kalshi", "novig", "dkex", "polymarket_us", "udx", "cdna", "rothera"]


def load_csv(filename: str) -> dict:
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        print(f"  Note: {filename} not found - its columns will be blank.")
        return {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def load_csv_multi(filename: str, subkey: str) -> dict:
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        print(f"  Note: {filename} not found - its columns will be blank.")
        return {}
    out: dict = {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["date"], {})[row[subkey]] = row
    return out


def fnum(row: dict, key: str):
    v = (row.get(key) or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def r2(x):
    return "" if x is None else round(x, 2)


def build_view(dates, contracts_of, notional_of, priced, combo_of=None):
    """Assemble one view. `contracts_of[venue]` / `notional_of[venue]` are
    date->value dicts (notional may be None where it must be estimated).
    `priced` is the set of venues whose notional is real (used to derive the
    blended price for the estimated ones). `combo_of[venue]` -> (vol, pct) dicts
    when the view carries combo columns."""
    rows = []
    for date in dates:
        c = {v: contracts_of[v].get(date) for v in VENUES}
        n = {v: (notional_of[v].get(date) if notional_of[v] else None) for v in VENUES}

        known_c = sum(c[v] for v in priced if c.get(v))
        known_n = sum(n[v] for v in priced if n.get(v))
        avg_price = (known_n / known_c) if known_c else 0.5
        for v in VENUES:
            if n[v] is None and c[v] is not None:
                n[v] = c[v] * avg_price

        cs = [c[v] for v in VENUES if c[v] is not None]
        ns = [n[v] for v in VENUES if n[v] is not None]

        row = {"date": date}
        for v in VENUES:
            row[f"{v}_contracts"] = r2(c[v])
        row["total_contracts"] = round(sum(cs), 2) if cs else ""
        for v in VENUES:
            row[f"{v}_notional_est"] = r2(n[v])
        row["total_notional_est"] = round(sum(ns), 2) if ns else ""

        if combo_of:
            for v in VENUES:
                cv, cp = combo_of.get(v, ({}, {}))
                row[f"{v}_combo"] = r2(cv.get(date)) if isinstance(cv, dict) else ""
                row[f"{v}_combo_pct"] = cp.get(date, "") if isinstance(cp, dict) else ""
        rows.append(row)
    return rows


def write(path, rows, with_combo):
    fields = ["date"]
    fields += [f"{v}_contracts" for v in VENUES] + ["total_contracts"]
    fields += [f"{v}_notional_est" for v in VENUES] + ["total_notional_est"]
    if with_combo:
        for v in VENUES:
            fields += [f"{v}_combo", f"{v}_combo_pct"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    print("Loading source files...")
    k_total = load_csv("kalshi_daily_total.csv")
    k_sports = load_csv("kalshi_sports_daily_totals.csv")
    k_cat = load_csv_multi("kalshi_daily_by_category.csv", "group")
    novig = load_csv("novig_daily_totals.csv")
    dkex = load_csv("dkex_daily_totals.csv")
    poly = load_csv("polymarket_us_daily_totals.csv")
    udx = load_csv("udx_daily_totals.csv")
    cdna = load_csv("cdna_daily_totals.csv")
    roth = load_csv("rothera_daily_totals.csv")

    dates = sorted(set(k_total) | set(k_sports) | set(novig) | set(dkex)
                   | set(poly) | set(udx) | set(cdna) | set(roth))
    if not dates:
        print("No data in any source file. Run the individual scripts first.")
        return

    # ---- Kalshi sports = single-leg sports + parlay (KXMVE engine). The parlay
    # figure comes from the trade-feed sample (kalshi_daily_by_category.csv).
    # Parlay is large by contract count (cheap multi-leg legs) but small by $.
    def k_parlay_row(date):
        c = k_cat.get(date, {})
        return c.get("Parlay") or c.get("Combos & exotics") or {}

    k_sports_c, k_sports_n, k_combo, k_combo_pct = {}, {}, {}, {}
    for date in dates:
        single = fnum(k_sports.get(date, {}), "kalshi_sports_volume")
        single_n = fnum(k_sports.get(date, {}), "kalshi_sports_notional_est")
        prow = k_parlay_row(date)
        par = fnum(prow, "contracts")
        par_n = fnum(prow, "notional_est")
        if single is not None or par is not None:
            tot = (single or 0) + (par or 0)
            k_sports_c[date] = tot
            k_combo[date] = par
            k_combo_pct[date] = round(100 * par / tot, 2) if par and tot else ""
        if single_n is not None or par_n is not None:
            k_sports_n[date] = (single_n or 0) + (par_n or 0)

    def col(src, key):
        return {d: fnum(src.get(d, {}), key) for d in dates}

    def combo_pair(src):
        return (col(src, "combo_volume"),
                {d: (src.get(d, {}).get("combo_pct") or "") for d in dates})

    # ================= SPORTS view =================
    sports_rows = build_view(
        dates,
        contracts_of={
            "kalshi": k_sports_c,
            "novig": col(novig, "total_volume"),
            "dkex": col(dkex, "total_volume"),
            "polymarket_us": col(poly, "total_volume"),
            "udx": col(udx, "total_volume"),
            "cdna": col(cdna, "total_volume"),
            "rothera": col(roth, "total_volume"),
        },
        notional_of={
            "kalshi": k_sports_n,
            "novig": col(novig, "notional_est"),
            "dkex": col(dkex, "notional_est"),
            "polymarket_us": col(poly, "notional_est"),
            "udx": None,
            "cdna": None,
            "rothera": col(roth, "notional_est"),
        },
        priced={"kalshi", "novig", "dkex", "polymarket_us", "rothera"},
        combo_of={
            "kalshi": (k_combo, k_combo_pct),
            "novig": combo_pair(novig),
            "dkex": combo_pair(dkex),
            "polymarket_us": combo_pair(poly),
            "udx": combo_pair(udx),
            "cdna": combo_pair(cdna),
            "rothera": ({}, {}),   # no parlay product
        },
    )
    write(SPORTS_CSV, sports_rows, with_combo=True)

    # ================= TOTAL (whole-exchange) view =================
    # Novig / DKeX / UDX are sports-only, so their total == their sports figure.
    total_rows = build_view(
        dates,
        contracts_of={
            "kalshi": col(k_total, "total_contracts"),
            "novig": col(novig, "total_volume"),
            "dkex": col(dkex, "total_volume"),
            "polymarket_us": col(poly, "all_volume"),
            "udx": col(udx, "total_volume"),
            "cdna": col(cdna, "all_volume"),
            "rothera": col(roth, "all_volume"),
        },
        notional_of={
            "kalshi": col(k_total, "total_notional_est"),
            "novig": col(novig, "notional_est"),
            "dkex": col(dkex, "notional_est"),
            "polymarket_us": col(poly, "all_notional_est"),
            "udx": None,
            "cdna": None,
            "rothera": col(roth, "all_notional_est"),
        },
        priced={"kalshi", "novig", "dkex", "polymarket_us", "rothera"},
    )
    write(TOTAL_CSV, total_rows, with_combo=False)

    print(f"\nDone. {len(dates)} date(s).")
    print(f"  sports -> {SPORTS_CSV}")
    print(f"  total  -> {TOTAL_CSV}")


if __name__ == "__main__":
    main()
