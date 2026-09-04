# Sports volume tracker — methodology & the "contracts vs dollars" question

## Scope

All seven pullers collect **2026-08-01 → yesterday** and refresh daily. The
start date lives in `volume_common.py` (`START_DATE`). Earlier history is
intentionally not collected.

Each run rebuilds the whole date range and rewrites its totals CSV, so a
backfill and a daily update are the same code path and a partial/bad day can
never get frozen in. The report-based sources (Novig, DKeX, Polymarket US, UDX,
Crypto.com/CDNA, Rothera) re-download the trailing `RECHECK_DAYS` (3) in case a
report was still being finalised when first seen. Kalshi uses the immutable
trade feed, so it only re-pulls the most recent day.

## The two volume columns

Every venue here reports volume as a **count of contracts traded** — the
standard CFTC / futures convention where a buy matched with a sell is **one**
contract, not two. It is *not* double-counted. Each contract is a instrument
with a **\$1 maximum payout**.

| Column | What it is | How it's computed |
|---|---|---|
| `*_contracts` | Contracts traded that day. Exact. This is the number the venues themselves publish as "volume". | `sum(trade volume)` straight from each venue's report / trade feed |
| `*_notional_est` | Approx. **USD that actually changed hands** | `sum(contracts × traded price)` |

**Why both:** a contract that trades at \$0.35 moved 35 cents, not a dollar, so
`contracts` runs about **2–3× higher** than dollars-changed-hands.

**Both columns are defined identically for all seven venues:**

- `*_contracts` — "number of contracts traded × \$1 face value", one-sided. This
  is precisely the metric each venue reports as its own volume, and what DeFi
  Rate, DefiLlama and Pew publish. Kalshi calls it "notional taker volume";
  Novig's `dailyVolume` is the same thing (verified: it equals `sum(qty)/2` from
  the trade file, i.e. the one-sided count = total collateral); the CFTC daily
  reports (DKeX / Polymarket US / UDX) use the standard futures "total volume of
  trading" — also one-sided.
- `*_notional_est` — the same contracts re-weighted by their volume-weighted
  traded price (≈ USD that changed hands).

**What the shared definition still can't equalise: market-making.** Kalshi runs
aggressive maker-incentive / volume-rebate programs (the CFTC issued a warning
about them in Aug 2026 and there is open wash-trading concern), so a larger
fraction of its *reported* volume is automated liquidity provision than at the
others. That is a genuine structural difference between the exchanges, measured
consistently — not a counting artifact — and there is no public ex-market-maker
number for any venue to substitute.

### Where each `notional_est` price comes from

| Venue | Price source | Quality |
|---|---|---|
| Kalshi | whole-exchange avg (~\$0.30) from the trade-feed sample × the daily contract total; the sports-only file uses per-candle `mean` (VWAP) | fair (total) / good (sports) |
| Polymarket US | bid/offer midpoint per market (report has no trade-price column populated) | good |
| Novig | per-market `close` (falls back to open, then H/L mid) | good |
| DKeX | per-market `High Price` / `Low Price` midpoint | fair |
| UDX | **no usable price column** (`low` is always 0). Estimated in `unified_sports_volume.py` as `udx_contracts × that day's contract-weighted average price across the priced venues.` | rough |
| Crypto.com / CDNA | **no price in the summary section** of the bulletin. Estimated the same way as UDX (`cdna_contracts × the day's cross-venue average price`). | rough |
| Rothera | per-market `high` / `low` midpoint from the daily report (falls back to `close`/`open`) | good |

Sanity check: Polymarket US `notional_est` lands around **\$37–51M/day** for
August 2026. External estimates put the whole QCX entity (all categories) near
\$1.8B/month in May 2026 and growing, of which sports is the largest slice — so
~\$40M/day sports is the right order of magnitude. The raw `contracts` figure
(~\$100–165M/day) would have been larger than the entire entity's daily volume,
which is what first flagged the issue.

## Per-venue notes

- **Kalshi** — sports = every series in Kalshi's "Sports" category (~3,600
  series). Pulled from the public `GET /markets/trades` feed, one ET day at a
  time, filtered to sports series. Kalshi's cross-**category** multi-event
  (parlay) product `KXMVECROSSCATEGORY` is **excluded** — it mixes sports and
  non-sports legs and can't be cleanly attributed.
  *Combo column:* Kalshi's **multi-event parlay** volume. Its parlay engine is
  the `KXMVE*` series (dominated by `KXMVECROSSCATEGORY`) plus `*COMBO` /
  `PREPACK` / `SGP` series. Despite the "cross-category" name, a leg-composition
  spot-check (25 traded parlays, 152 legs) found them **100% sports legs** —
  parlay bettors build sports parlays — so the **"Combos & exotics" category**
  from the trade-feed sample is effectively Kalshi's sports-parlay figure (a
  couple % of it is entertainment MVE — Grammys / Oscars — and non-sports
  `*COMBO`, immaterial). Pricing the parlay markets directly is not feasible
  (`KXMVESPORTSMULTIGAMEEXTENDED` alone runs 40k+ markets that live ~2 weeks
  each, and `KXMVECROSSCATEGORY` far more), so it's the daily parlay figure from
  `kalshi_daily_by_category.csv` (whole-exchange total × the sampled parlay
  share). `kalshi_combo_pct` in the unified sheet = **parlay ÷ Kalshi's
  whole-exchange total** ≈ **48% by contracts / ~11% by dollars** — measured on
  the same base as the Kalshi column itself, which is the representative figure
  (parlay contracts are many and cheap, hence the high contract %). Being
  sample-scaled it moves less day-to-day than the report-based venues.
  `kalshi_total_volume.py` writes both a `Combos & exotics` group row and an
  independent `Parlay` cross-cut row (its own `is_parlay` tag on the sampled
  trades — the two agree to ~1pp); the unified sheet uses `Parlay` when non-zero,
  else `Combos & exotics`. The dashboard table's blended Combo % and the Excel
  "Daily volume" blended column cover the five report-based venues only (whose %
  is of their own book); Kalshi's whole-exchange-based % is on its card and the
  "Parlay share" tab.
- **Novig** — 100% sports. Combo = rows with `reportTicker == "COMBO"`.
  History starts 2026-08-04 (launch); no reports exist for Aug 1–3.
- **DKeX** (DraftKings / Railbird) — 100% sports. Combo = `Symbol` starts with
  `COMBO`. The combo product started ~2026-08-26; CFB season open (Aug 28–30)
  drove a ~10× volume spike that is real. The newest 1–2 days are often partial
  until DKeX finalises the report (~noon ET next day) — the `RECHECK_DAYS`
  re-pull corrects them.
- **Polymarket US** (QCX) — **not** sports-only. Sports rows are identified by a
  symbol-prefix allowlist (`SPORTS_PREFIXES` in the script). A backfill audit
  resolved every prefix seen; `aqc` (advance/qualify markets) and `adpc`
  (draft-pick markets) are sports, `rtc` (reality TV) is not. Prefix matching is
  case-insensitive — early reports used UPPERCASE prefixes and were dropping
  ~\$360M of volume across 8 days in May before that fix. New prefixes get
  logged to `polymarket_us_unknown_prefixes.csv`. `caoc` = the combo product
  (started ~2026-08-06).
- **UDX** (Underdog / Aristotle) — 100% sports. Combo = `report_ticker ==
  "UDXCOMBO"`. Real volume from mid-July 2026; earlier 0-row manifest dates are
  skipped.
- **Crypto.com / CDNA** (`cdna_daily_volume.py`) — North American Derivatives
  Exchange (Nadex), a CFTC DCM/DCO owned by Crypto.com, trading as "OG
  Prediction Markets" and "Crypto.com | Derivatives North America". Since
  mid-2026 it also clears **all** of FanDuel Predicts' sports flow, so this one
  column = Crypto.com's own prediction market **+ FanDuel Predicts** + anything
  else routed to the CDNA book. Source: the end-of-day **Daily Bulletin PDF**
  (`s3.amazonaws.com/market-data-prod.nadex.com/YYYYMMDD_dailybulletin.pdf`) — a
  Part-16 disclosure. Only the first section, "Daily Bulletin Summary" (one row
  per contract series, columns *Date / Instrument Type / Resource Class /
  Resource Description / Volume*), is parsed; the ~3,000-page trade-by-trade
  detail section is ignored. We keep **Resource Class `EVENTS`** (the
  prediction-market book) and drop the legacy Nadex crypto/fx/index/commodity
  binaries (other resource classes). Within EVENTS a small non-sports tail
  (elections, weather, culture, standalone crypto-price events — prefixes
  `ELECT/ELC/EC/CLIM/CUL/CRYPT/COMPANIES/...`, together **~0.15%** of EVENTS
  volume) is excluded; unrecognised prefixes are logged to
  `cdna_unknown_prefixes.csv`. Combo = the `COMBOS` prefix (FanDuel-style
  multi-leg parlays), ~30–40% of the total. **Face value:** the EVENTS contracts
  are the $1 tier — every EVENTS strike in the bulletin detail prices between
  0.01 and 0.99 — so the count is on the same "contracts × $1" basis as the
  other venues. **Caveats:** (1) the bulletin has no exchange-published grand
  total to reconcile against, and Crypto.com OG / FanDuel Predicts / Fanatics
  routing can't be split apart — it's the combined CDNA book; (2) the summary
  "Volume" is taken as one-sided per CFTC Part 16 convention (same as the DKeX /
  Polymarket US / UDX daily reports); (3) some days the report has no EVENTS
  rows at all (early August, before FanDuel routing ramped) — those days are
  simply absent.
- **Rothera** (`rothera_daily_volume.py`) — the CFTC DCM/DCO behind **Robinhood's**
  prediction market (formerly LedgerX / MIAX Derivatives Exchange; 90% owned by a
  Robinhood + Susquehanna JV since Jan 2026). A top-3 US venue, overwhelmingly
  sports (MLB-dominated). Source: the CFTC Part-16 **end-of-day market report**
  published daily ~4:15pm CT, listed at `rothera.io/volume-open-interest`, hosted
  at `publicfiles.rotheramarkets.com/clearing/outbound/mkt_eod_YYYYMMDD.csv`. One
  clean row per contract: `date, product, symbol, expiration_date, trade_count,
  volume, oi, block_volume, settlement_price, open, high, low, close, ...`.
  `volume` is one-sided contracts; every contract is a \$1 binary (all prices in
  0.00–1.00). Sports filter: keep everything except a non-sports tail — US macro
  prints (`EC*` prefixes) and elections (`USPRES*`, `*RACE`, `CNTRL*`,
  `VPPARTYNOM`), roughly 1–6% of daily volume (higher in a Fed-meeting week);
  unrecognised prefixes are logged to `rothera_unknown_prefixes.csv`. **Notional
  is real** (per-market `high`/`low` midpoint), not estimated. Rothera runs no
  on-exchange parlay product (checked: 340 distinct products over a sample, none
  combo-shaped; the trade feed is single-leg only), so it has no combo column.

## Kalshi

**Kalshi's column is its WHOLE-EXCHANGE volume — every category, not sports
only** (per request). Kalshi publishes no per-day exchange total, and rebuilding
one over the public API is impractical (~5-6M trades/day; the parlay engine and
15-minute crypto series each have 40k+ markets). So `kalshi_total_volume.py`
assembles it:

- **History** from **DeFi Rate's** Kalshi volume table (built from Kalshi's API;
  "contracts × \$1 face" — same basis as every other column; monthly sum matches
  Kalshi's own "\$33.7B so far in August"). Lags ~2 days.
- **The last ~2-3 days DeFi Rate is missing** are *projected*: our own verified
  Kalshi **sports** figure ÷ sports' recent share of the total (~29-35%). These
  rows carry `source=projected` in `kalshi_daily_total.csv` and show a `~` in the
  dashboard; the dashboard's headline figures still anchor to the last firm day.

`kalshi_sports_daily_volume.py` runs the candlestick method filtered to the
Sports category for the sports slice — **verified**: 21 sample markets across 12
sports each matched their `volume_fp` to the exact trade-by-trade sum (ratio
1.0000).

**Notional:** whole-exchange `notional_est` = total_contracts × the ~\$0.30 avg
traded price from the trade-feed sample. The sports file weights each candle by
its `mean` price (period VWAP), NOT `close` — on a settlement day `close` is the
0/1 outcome, which zeroes out every NO and doubles every YES; mean-weighting
matches the exact trade-by-trade notional to <0.1%.

### Whole-exchange category mix (`kalshi_daily_by_category.csv`)

"Combos & exotics" = Kalshi's "Exotics" category (`KXMVECROSSCATEGORY`, the MVE
multi-event parlay engine, + siblings) plus any `*COMBO` series. Month-to-date:

| Group | by contracts | by \$ traded |
|---|--:|--:|
| Combos & exotics | **~43%** | ~9% |
| Sports | ~35% | ~54% |
| Crypto (15-min BTC/ETH/…) | ~18% | ~33% |
| Economics & financials | ~2% | ~3% |
| Politics / Other | ~2% | ~1% |

Combos & exotics is the biggest slice by contract count but small by dollars — a
multi-leg parlay contract sells for a few cents.

## Not included

- **Rothera** — **now included** (see the per-venue note above). Its Part-16
  daily report lives at `publicfiles.rotheramarkets.com`, linked from
  `rothera.io/volume-open-interest`.
- **Crypto.com / CDNA** — **now included** (see the per-venue note above). The
  public daily-report URL turned out to be the Nadex Daily Bulletin PDF on S3.
- **ProphetX** — not built (small; no confirmed public daily report).
