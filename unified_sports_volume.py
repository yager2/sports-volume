"""
Unified daily sports volume + combo comparison.

Merges the output of the platform-specific pullers into one sheet:
    kalshi_total_volume.py         -> kalshi_daily_total.csv   (WHOLE exchange, all categories)
    novig_daily_volume.py          -> novig_daily_totals.csv
    dkex_daily_volume.py           -> dkex_daily_totals.csv
    polymarket_us_daily_volume.py  -> polymarket_us_daily_totals.csv
    udx_daily_volume.py            -> udx_daily_totals.csv
    cdna_daily_volume.py           -> cdna_daily_totals.csv   (Crypto.com / Nadex + FanDuel Predicts)
    rothera_daily_volume.py        -> rothera_daily_totals.csv (Robinhood's exchange)

Kalshi's column is its TOTAL exchange volume (every category); the other six
are their sports volume (Novig/DKeX/UDX are 100% sports; Polymarket US is
prefix-filtered; CDNA is the EVENTS resource class minus a tiny non-sports tail;
Rothera is prefix-filtered, dropping its econ/elections tail).

Run AFTER the source scripts. A missing source file just leaves its
columns blank.

Two volume measures per venue (see VOLUME_METHODOLOGY.md):
  *_contracts    - contracts traded that day (exact; what the venues report as
                   "volume"; one-sided; $1 max payout each).
  *_notional_est - contracts x traded-price estimate ~= USD that changed hands.
                   Kalshi/Novig/DKeX/Polymarket use real per-market prices from
                   their own reports. UDX's report has no usable price column,
                   so its notional is estimated with that day's contract-
                   weighted average price across the other venues.

Output: unified_sports_volume.csv
"""

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CSV = os.path.join(HERE, "unified_sports_volume.csv")


def load_csv(filename: str) -> dict:
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        print(f"  Note: {filename} not found - its columns will be blank.")
        return {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def load_csv_multi(filename: str, subkey: str) -> dict:
    """For files with several rows per date: {date: {subkey_value: row}}."""
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


def main():
    print("Loading source files...")
    # Kalshi = WHOLE-EXCHANGE total (all categories), per request - kalshi_total_volume.py.
    # Falls back to the sports-only file if the total one isn't built yet.
    kalshi = load_csv("kalshi_daily_total.csv") or load_csv("kalshi_sports_daily_totals.csv")
    # Kalshi parlay daily volume, from kalshi_total_volume.py's trade-feed sample
    # applied to the daily whole-exchange total.
    kalshi_cat = load_csv_multi("kalshi_daily_by_category.csv", "group")
    novig = load_csv("novig_daily_totals.csv")
    dkex = load_csv("dkex_daily_totals.csv")
    poly = load_csv("polymarket_us_daily_totals.csv")
    udx = load_csv("udx_daily_totals.csv")
    cdna = load_csv("cdna_daily_totals.csv")
    rothera = load_csv("rothera_daily_totals.csv")

    all_dates = sorted(set(kalshi) | set(novig) | set(dkex) | set(poly)
                       | set(udx) | set(cdna) | set(rothera))
    if not all_dates:
        print("No data in any source file. Run the individual scripts first.")
        return

    fieldnames = [
        "date",
        # contracts traded (exact)
        "kalshi_contracts", "novig_contracts", "dkex_contracts",
        "polymarket_us_contracts", "udx_contracts", "cdna_contracts",
        "rothera_contracts", "total_contracts",
        # ~USD that changed hands (estimated)
        "kalshi_notional_est", "novig_notional_est", "dkex_notional_est",
        "polymarket_us_notional_est", "udx_notional_est", "cdna_notional_est",
        "rothera_notional_est", "total_notional_est",
        # combo / parlay share
        "kalshi_combo", "kalshi_combo_pct",
        "novig_combo", "novig_combo_pct",
        "dkex_combo", "dkex_combo_pct",
        "polymarket_us_combo", "polymarket_us_combo_pct",
        "udx_combo", "udx_combo_pct",
        "cdna_combo", "cdna_combo_pct",
    ]

    rows_out = []
    for date in all_dates:
        k, n, d, p, u, cd, ro = (x.get(date, {}) for x in
                                 (kalshi, novig, dkex, poly, udx, cdna, rothera))
        _kc = kalshi_cat.get(date, {})
        # Kalshi parlay volume: the sampled "Parlay" cross-cut when available and
        # non-zero, else the "Combos & exotics" category (≈ the same thing - that
        # category is Kalshi's multi-event parlay engine, spot-checked ~100%
        # sports legs).
        _kp = _kc.get("Parlay", {})
        k_parlay_row = _kp if fnum(_kp, "contracts") else _kc.get("Combos & exotics", {})

        kc = fnum(k, "total_contracts") or fnum(k, "kalshi_sports_volume")
        nc = fnum(n, "total_volume")
        dc = fnum(d, "total_volume")
        pc = fnum(p, "total_volume")
        uc = fnum(u, "total_volume")
        cc = fnum(cd, "total_volume")
        rc = fnum(ro, "total_volume")

        kn = fnum(k, "total_notional_est") or fnum(k, "kalshi_sports_notional_est")
        nn = fnum(n, "notional_est")
        dn = fnum(d, "notional_est")
        pn = fnum(p, "notional_est")
        rn = fnum(ro, "notional_est")

        # UDX and CDNA have no usable price data -> estimate their notional with
        # the contract-weighted average price of the venues that do, same day.
        known_c = sum(c for c in (kc, nc, dc, pc, rc) if c)
        known_n = sum(x for x in (kn, nn, dn, pn, rn) if x)
        avg_price = (known_n / known_c) if known_c else 0.5
        un = uc * avg_price if uc is not None else None
        cn = cc * avg_price if cc is not None else None

        contracts = [c for c in (kc, nc, dc, pc, uc, cc, rc) if c is not None]
        notionals = [x for x in (kn, nn, dn, pn, un, cn, rn) if x is not None]

        def r(x):
            return "" if x is None else round(x, 2)

        # Kalshi parlay volume (the KXMVE multi-event engine + prepack/SGP/*COMBO
        # series). Kalshi's column here is the WHOLE exchange, so its combo % is
        # the parlay share of that whole-exchange total (~48% by contracts /
        # ~11% by $) - the most representative figure for how the column is
        # defined. (The other venues' combo % is of their sports book, which for
        # them IS their whole book.)
        k_combo = fnum(k_parlay_row, "contracts")
        k_combo_pct = (round(100 * k_combo / kc, 2)
                       if k_combo is not None and kc else "")

        rows_out.append({
            "date": date,
            "kalshi_contracts": r(kc), "novig_contracts": r(nc),
            "dkex_contracts": r(dc), "polymarket_us_contracts": r(pc),
            "udx_contracts": r(uc), "cdna_contracts": r(cc),
            "rothera_contracts": r(rc),
            "total_contracts": round(sum(contracts), 2) if contracts else "",
            "kalshi_notional_est": r(kn), "novig_notional_est": r(nn),
            "dkex_notional_est": r(dn), "polymarket_us_notional_est": r(pn),
            "udx_notional_est": r(un), "cdna_notional_est": r(cn),
            "rothera_notional_est": r(rn),
            "total_notional_est": round(sum(notionals), 2) if notionals else "",
            "kalshi_combo": r(k_combo), "kalshi_combo_pct": k_combo_pct,
            "novig_combo": n.get("combo_volume", ""), "novig_combo_pct": n.get("combo_pct", ""),
            "dkex_combo": d.get("combo_volume", ""), "dkex_combo_pct": d.get("combo_pct", ""),
            "polymarket_us_combo": p.get("combo_volume", ""),
            "polymarket_us_combo_pct": p.get("combo_pct", ""),
            "udx_combo": u.get("combo_volume", ""), "udx_combo_pct": u.get("combo_pct", ""),
            "cdna_combo": cd.get("combo_volume", ""), "cdna_combo_pct": cd.get("combo_pct", ""),
        })

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)

    print(f"\nDone. {len(rows_out)} date(s) -> {OUTPUT_CSV}")
    print("Columns: *_contracts (exact contract count) and *_notional_est "
          "(~USD changed hands). See VOLUME_METHODOLOGY.md.")


if __name__ == "__main__":
    main()
