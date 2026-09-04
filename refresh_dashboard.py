"""
Re-embed the latest unified data into dashboard.html so the local file always
opens current (no CSV import needed on this machine).

Run by run_all.py as its final step; also fine to run standalone:
    python refresh_dashboard.py

The hosted Artifact copy is separate - refresh that by opening it on this
laptop and clicking "Update data" (pick unified_sports_volume.csv), or ask
Claude to republish.
"""

import csv
import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "unified_sports_volume.csv")
KX_MIX_JSON = os.path.join(HERE, "kalshi_exchange_mix.json")
KX_TOTAL_CSV = os.path.join(HERE, "kalshi_daily_total.csv")
HTML = os.path.join(HERE, "dashboard.html")


def build_kalshi_mix():
    """The kalshi_exchange_mix.json object (category split), or None."""
    if not os.path.exists(KX_MIX_JSON):
        return None
    try:
        return json.load(open(KX_MIX_JSON, encoding="utf-8"))
    except Exception:
        return None


def kalshi_projected_dates():
    """Dates where the Kalshi total is projected from sports (DeFi Rate hadn't
    published yet), not from DeFi Rate directly."""
    if not os.path.exists(KX_TOTAL_CSV):
        return []
    with open(KX_TOTAL_CSV, newline="", encoding="utf-8") as f:
        return [r["date"] for r in csv.DictReader(f) if r.get("source") == "projected"]


def build_rows():
    rows = []
    with open(CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            def g(k):
                v = (r.get(k) or "").strip()
                return round(float(v), 2) if v else None
            rows.append({
                "date": r["date"],
                "c": [g("kalshi_contracts"), g("polymarket_us_contracts"), g("novig_contracts"),
                      g("dkex_contracts"), g("udx_contracts"), g("cdna_contracts"),
                      g("rothera_contracts")],
                "n": [g("kalshi_notional_est"), g("polymarket_us_notional_est"), g("novig_notional_est"),
                      g("dkex_notional_est"), g("udx_notional_est"), g("cdna_notional_est"),
                      g("rothera_notional_est")],
                "cp": [g("polymarket_us_combo_pct"), g("novig_combo_pct"),
                       g("dkex_combo_pct"), g("udx_combo_pct"), g("cdna_combo_pct"),
                       g("kalshi_combo_pct")],
            })
    return rows


def main():
    if not os.path.exists(CSV):
        print("refresh_dashboard: no unified_sports_volume.csv yet - skipped.")
        return
    rows = build_rows()
    through = rows[-1]["date"]
    loaded = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    kmix = build_kalshi_mix()
    kproj = kalshi_projected_dates()

    html = open(HTML, encoding="utf-8").read()
    block = (
        '  var SNAPSHOT = {\n'
        '    through: "' + through + '",\n'
        '    loaded: "' + loaded + '",\n'
        '    rows: ' + json.dumps(rows, separators=(",", ":")) + ',\n'
        '    kalshiMix: ' + json.dumps(kmix, separators=(",", ":")) + ',\n'
        '    kalshiProjected: ' + json.dumps(kproj, separators=(",", ":")) + '\n'
        '  };'
    )
    new_html, n = re.subn(
        r"  var SNAPSHOT = \{.*?\n  \};",
        lambda _m: block,
        html,
        count=1,
        flags=re.S,
    )
    if n != 1:
        print("refresh_dashboard: could not find SNAPSHOT block - dashboard.html unchanged.")
        return
    with open(HTML, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_html)
    mix_note = (f", kalshi mix ({kmix.get('basis', 'sample')})"
                if kmix else ", no kalshi mix yet")
    print(f"refresh_dashboard: embedded {len(rows)} days (through {through}){mix_note}")


if __name__ == "__main__":
    main()
