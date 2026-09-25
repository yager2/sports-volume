# Kalshi NFL taker hold - methodology

## What this is for

Sportsbooks' hold (the share of money bet that the book keeps) only becomes
public when states publish monthly reports, weeks after the games. Kalshi's
trade tape is public the moment a trade happens. This tracker reads it after
every NFL game and asks: **did the people who crossed the spread (takers) lose
more or less than the closing prices said they should?** That's a
sportsbook-style hold, available the next morning. It's meant to be
**directionally** right: which weeks are good or bad for books, and roughly how
much. It isn't meant to reproduce a state's number to the decimal.

## Pipeline

| Step | Script | Output |
|---|---|---|
| Pull newly finished games | `nfl_hold.py` | `nfl_hold_data/games/<season>/<game>.json.gz` (one per game, never re-pulled once complete) |
| Rebuild the report | `nfl_hold_report.py` (+ `nfl_hold_excel.py`, `nfl_hold_dashboard_template.html`) | `nfl_hold_by_game.csv`, `nfl_hold_by_game_product.csv`, `nfl_hold_weekly.csv`, `nfl_hold.xlsx`, `nfl_hold_dashboard.html` |
| Schedule | `.github/workflows/nfl_hold.yml` | daily 11:17 UTC (6:17am CDT) |

Sources: Kalshi public REST API only (no key) for markets and trades, plus ESPN's
public scoreboard for kickoff time, final score, season and week.

## Which markets

Every Kalshi NFL series whose events are single games. A series counts if its
event tickers look like `SERIES-26SEP20INDKC` *and* that game code also exists
in the moneyline series `KXNFLGAME`. As of 2026-09-25 that's 88 series. Futures
(MVP, division winners, win totals) drop out automatically, and new prop series
are picked up by the weekly series refresh.

Products, reported separately and never blended with parlays:

| Product | Series (examples) |
|---|---|
| moneyline | `KXNFLGAME` |
| spread | `KXNFLSPREAD` (a ladder: "KC wins by over 7.5", "over 10.5", ...) |
| total | `KXNFLTOTAL` (ladder: "over 45.5", "over 48.5", ...) |
| player_prop | `KXNFLTD`, `KXNFLFIRSTTD`, `KXNFLRECYDS`, `KXNFLRSHYDS`, `KXNFLPASSYDS`, `KXNFLREC`, ladders... |
| period | `KXNFL1H`, `KXNFL1HSPREAD`, `KXNFL1QTOTAL`, ... |
| game_prop | `KXNFLOT`, `KXNFLWINMARGIN`, `KXNFLTEAMTOTAL`, `KXNFLFG`, ... |

**Parlays aren't included yet.** That covers `KXNFLCOMBO`, `KXNFLPREPACK` and
the cross-game `KXMVE*` parlay engine. They're the next phase, reported
separately, and the place where sportsbook hold is really made.

## Per trade

For each trade: `c` = contracts, `p` = the price the **taker** paid for their
side (`yes_price` if they bought YES, `no_price` if they bought NO), and `s` =
the settlement payout of the taker's side (1 or 0).

- taker $ = `c × p`
- taker P&L = `c × (s − p)` (maker P&L is the exact negative)
- taker fee = `0.07 × fee_multiplier × c × P × (1 − P)`; maker fee = 25% of
  that on series whose `fee_type` is `quadratic_with_maker_fees`. The multiplier
  and type come from Kalshi's series endpoint (MLB-style 0.5× series exist;
  every NFL game series is 1× today).
- **Pre-game vs. in-game:** the trade's timestamp vs. ESPN's kickoff. If ESPN
  has no match, kickoff falls back to Kalshi's `expiration_time` minus 48h,
  because NFL rules void a game not started within 48 hours. Unmatched games
  are logged to `nfl_hold_data/unmatched_games.csv`.

Trades are rolled up per market by (pre/in-game, taker side, ticket-size
bucket, block trade). That rollup keeps every sum the report needs without
storing ~1M raw trades per game. Counted contracts reconcile exactly to
Kalshi's own `volume_fp` for every market (checked on the first test game:
774/774 markets).

## Hold measures

- **Hold** = −Σ taker P&L ÷ Σ taker $. Positive means takers lost, which is
  good for the house.
- **Hold incl. fees (headline)** = −(Σ taker P&L − Σ taker fees) ÷ Σ taker $.
  This is the customer's all-in loss rate, the closest match to a sportsbook's
  hold (which keeps both losses and vig).
- **Closing price** of each market = VWAP of its trades in the final 30
  minutes before kickoff if there were at least 3, else the last pre-kickoff
  trade. Winner markets (moneyline, half/quarter winners) are normalised so the
  outcomes sum to 100%.
- **Expected hold** = the same formula, but paying each pre-game trade at the
  closing price instead of the result: `c × (q − p)`, where `q` is the closing
  probability of the taker's side. It's pre-game only; in-game bets have no
  "close".
- **Luck (bps)** = (actual − expected pre-game hold) × 10,000. Positive means
  results broke for the house.

## Outcome factors (the "what happened on the field" layer)

- **Public side vs. price.** For every pre-game market, find the side takers
  net-bought (YES $ − NO $). Compare how often that side won with its closing
  probability, weighted by the net $. Positive means the public's picks beat
  their prices. That's a public-friendly week, which is exactly when sportsbook
  parlays (stacks of the same popular picks) get hurt most. This is the
  planned bridge to parlay hold once parlays are added.
- **Favorites:** the closing moneyline favorite and its normalised win
  probability; did it win.
- **Spread / total lines:** the rung of each ladder where the closing price
  crosses 50%, linearly interpolated. Did the favorite cover it, and did the
  game go over it (ESPN final score).
- **Ticket size:** taker $ per trade in <$25, $25–100, $100–1k, >$1k buckets.
  Small tickets are the retail proxy. A big order that fills against several
  resting orders shows up as several smaller trades, so the buckets skew
  small.

## Exclusions

- **Void-like settlements:** markets settled at anything other than $0 or $1.
  Kalshi settles an inactive player's TD market at a "fair price" (e.g. $0.06),
  and fantasy-point ladders settle scalar. They're left out of hold, the way a
  book refunds a void, and their $ are shown in the Void column.
- **Block trades** (negotiated off the order book) are left out and shown in
  the Block column.
- **Preseason** is stored but excluded from the headline views (dashboard
  toggle to include).

## Known limitations

- **Mix differs from a sportsbook.** About three-quarters of Kalshi NFL taker $
  trades *in-game* (on the first test game, $49M of $64M). There are no
  parlays yet, and makers include professional market makers rather than a
  single house. Read the direction and size of week-to-week swings, not the
  exact level.
- **Taker ≠ retail.** Some takers are sharp. The small-ticket bucket is the
  best available retail proxy.
- **Fees** are computed per fill without Kalshi's per-order round-up, so they
  run very slightly low.
- **Closing prices for thin props** can be stale. `close_method` / `close_age_min`
  are stored per market for checking. Markets with no pre-game trade have no
  close and don't enter expected hold.
- **Stat corrections:** a game is re-pulled for up to 10 days while any of its
  markets is unsettled (`complete=false`).

## Commands

Run these from the repo folder (`OneDrive\Desktop\Mention folder`):

```
python nfl_hold.py                       # daily: finished games from the last 10 days
python nfl_hold.py --game 26SEP20INDKC   # one game, prints a summary table
python nfl_hold.py --since 2026-09-01    # everything since a date
python nfl_hold.py --backfill            # every NFL game Kalshi has listed (hours; resumable)
python nfl_hold_report.py                # rebuild CSV / xlsx / dashboard from stored games
```
