"""
Run every sports-volume puller, then rebuild the unified sheet.

Order: the four report-based pullers first (fast), then Kalshi (slow - pages the
trade feed), then the merge so it picks up everything from the same run.

Scheduled once daily. Each puller is independent and idempotent: if one source
is down, the others still update and the unified sheet just carries that
venue's last-good numbers.

Run manually:  python run_all.py
"""

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _keep_awake(on: bool) -> None:
    """Stop Windows sleeping mid-run (the Kalshi step alone is ~20 min). The
    laptop sleeping during a run is what killed it on 2026-09-02/03."""
    if os.name != "nt":
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if on else ES_CONTINUOUS
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:  # noqa: BLE001
        pass

STEPS = [
    "novig_daily_volume.py",
    "dkex_daily_volume.py",
    "polymarket_us_daily_volume.py",
    "udx_daily_volume.py",
    "cdna_daily_volume.py",         # Crypto.com / Nadex bulletin PDFs (downloads ~40 MB/day)
    "rothera_daily_volume.py",      # Robinhood's exchange - Part-16 daily CSVs
    "kalshi_sports_daily_volume.py",
    "kalshi_total_volume.py",       # whole-exchange total + category mix incl. Combos & exotics
    "unified_sports_volume.py",
    "refresh_dashboard.py",
    "make_excel.py",                # formatted unified_sports_volume.xlsx
    "publish_dashboard.py",         # push local dashboard.html to the hosted artifact link
    "push_to_cloud.py",             # copy xlsx/csv to Google Drive for a 2nd computer
]

# On GitHub Actions the last two are handled by the workflow itself (Pages /
# an rclone step with a secret), so skip them there.
if os.environ.get("CI"):
    STEPS = [s for s in STEPS if s not in ("publish_dashboard.py", "push_to_cloud.py")]


def main():
    start = datetime.now(timezone.utc)
    print(f"=== volume pull run {start.isoformat()} ===\n")
    _keep_awake(True)
    results = {}
    try:
        for script in STEPS:
            print(f"\n----- {script} -----")
            t0 = time.time()
            proc = subprocess.run([PY, "-u", script], cwd=HERE)
            dt = time.time() - t0
            results[script] = proc.returncode
            print(f"----- {script}: exit {proc.returncode} in {dt:.0f}s -----")
    finally:
        _keep_awake(False)

    print("\n=== summary ===")
    for s, rc in results.items():
        print(f"  {'OK ' if rc == 0 else 'FAIL'} {s}")
    total = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"total {total / 60:.1f} min")
    sys.exit(1 if any(results.values()) else 0)


if __name__ == "__main__":
    main()
