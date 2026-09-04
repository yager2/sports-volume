# Claude Code Handoff — Sports Volume Tracker, Final Build
Copy this whole message into Claude Code, in your mention-edge project.

---

## 1. Copy these 6 files into the project

Put them all in the same folder (project root or a `data_sources/volume_tracking/` subfolder):

- `kalshi_sports_daily_volume.py` — Kalshi, sports-only, via public candlestick API. **Not yet verified against live data — see step 3.**
- `novig_daily_volume.py` — Novig, tested, includes combo split.
- `dkex_daily_volume.py` — DraftKings DKeX, tested against your real file, includes combo split.
- `polymarket_us_daily_volume.py` — Polymarket US, tested against your real file, sports-filtered via symbol-prefix allowlist, includes combo split.
- `udx_daily_volume.py` — Underdog Exchange (UDX/Aristotle), tested, 100% sports, includes combo split.
- `unified_sports_volume.py` — merges all five into one sheet.

## 2. Run each source script, confirm real output

Paste this to Claude Code:

> Run these in order, showing me the first few rows of each output CSV after it runs: novig_daily_volume.py, dkex_daily_volume.py, polymarket_us_daily_volume.py, udx_daily_volume.py, then kalshi_sports_daily_volume.py last. Fix anything that errors.

**What to expect:**
- **Novig** — should just work, same as before.
- **DKeX** — backfills ~3 months (June 11 onward) in one run.
- **Polymarket US** — backfills back to October 30, 2025. Will also produce `polymarket_us_unknown_prefixes.csv` if it hits any symbol prefix not yet classified as sports or non-sports — check that file periodically; a handful of low-volume prefixes (`aqc`, `rtc`, `gsc`, `scc`, `stsc`) are known-ambiguous and intentionally excluded rather than guessed at.
- **Underdog/UDX** — backfills from when real volume starts (~mid-to-late July 2026); earlier manifest dates with zero rows are skipped automatically.
- **Kalshi** — run this one last and carefully. It has NOT been tested against the live API. Tell Claude Code to run it for a single date first: `python kalshi_sports_daily_volume.py 2026-08-30`, then compare that day's total against what Kalshi's own site shows for sports volume that day, before letting it do a full backfill. If `category=Sports` doesn't return series, check `GET /search/tags_by_categories` for the exact category string Kalshi uses.

## 3. Spot-check accuracy (per your stated priority)

> Pick 2 random dates from each of the five totals CSVs. Pull up the equivalent numbers on each platform's own site/app for those dates and tell me if our totals are in the right ballpark. Flag anything that looks off.

## 4. Merge into one sheet

> Run unified_sports_volume.py. Confirm it produces unified_sports_volume.csv with columns for date, kalshi_sports, novig, dkex, polymarket_us, and udx (total/combo/combo_pct for the four that have combo data).

## 5. Schedule it

Once everything above is confirmed accurate:

> Add all five scripts to the existing scheduler, running once daily. Novig publishes shortly after midnight ET. DKeX updates by ~12PM ET. Polymarket US updates around midnight ET. Underdog and Kalshi can run anytime since they're pulled live/on-demand. Run unified_sports_volume.py last, after the others, so it picks up same-day data.

---

## Where things stand — final status

| Platform | Sports-only | Combo data | Confidence | History depth |
|---|---|---|---|---|
| Kalshi | Yes, by series | N/A (no combo product) | **Needs live verification (step 2)** | Configurable start date |
| Novig | 100% sports | Yes, real | High — tested | Since Aug 4, 2026 (launch) |
| DKeX | 100% sports | Yes, real (33.8% combo share on sample day) | High — tested against your real file | Since June 11, 2026 |
| Polymarket US | Prefix-filtered (not all markets are sports) | Yes, real (22.7% combo share on sample day) | High — tested against your real file | Since Oct 30, 2025 |
| Underdog/UDX | 100% sports | Yes, real | High — tested against real sample rows | Real volume from ~mid-July 2026 |
| Rothera | — | — | **No public path exists** — FCM-gated FIX API only | — |
| ProphetX, Crypto.com | — | — | Not built — separate future session, Tier 2 | — |

**Rothera note:** genuinely no free public daily-report path was found (checked their site directly — only institutional FCM access exists, same onboarding as their trading API). This is a real barrier, not something a script can route around. If you want Rothera data, the realistic paths are: (a) partnering with/becoming an FCM, or (b) waiting for a third-party tracker to start publishing it as the exchange scales — it's already doing real volume ($118M ADV in July), so that may happen naturally over time.

That's everything for this build. The only open task is Kalshi's live-data verification in step 2/3 — once that's confirmed, all five automated sources are done, tested, and merging into one sheet.
