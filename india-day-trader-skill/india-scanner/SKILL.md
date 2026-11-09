---
name: india-scanner
description: >-
  Chart Scanner for Indian stocks. Scans thousands of NSE charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the list of NSE
  equities from NSE's EQUITY_L.csv (series EQ; trade-to-trade BE/BZ flagged as
  not allowed intraday), optionally only the NIFTY 50 or NIFTY 500, with each
  stock's price band from sec_list.csv and whether it is in NSE's closing
  auction (CAS: F&O stocks end continuous trading at 15:15, Zerodha squares
  off MIS at 15:12). Runs a pre-market scan of daily closes
  (20-day closing-high breakout, bull flag, reclaimed the 20-day average,
  oversold, oversold bounce) and an intraday opening-range breakout scan on
  5-minute bars (9:15 to 9:30 IST range), marks stocks near their upper
  circuit, then writes trader-in/watchlist.json for india-news and
  india-entry. Free Yahoo data (.NS), bar by bar; the shortlist can be
  confirmed with Zerodha Kite's live price (get_ltp) when Kite is connected. Use when the user says "scan
  the market", "scan NSE", "find me setups", "what is breaking out", "opening
  range breakouts", "ORB scan", "scan the Nifty 50", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of an Indian trading day before /india-news.
argument-hint: "[universe | daily | orb] [--index nifty50|nifty500] [--at HH:MM]"
---

# india-scanner

The Chart Scanner is the first step of a day trade on NSE. It reads thousands
of free charts and lists the ones that match a rule. That is all it does. A
scan is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. SEBI's own study found that most
individual intraday traders in equities lose money; the scanner does not change
that. It only saves you from flipping through 2,000 charts by hand.

This is education and tooling, not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                        # -> trader-in/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty50        # or nifty500
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                           # before 9:15 AM -> trader-in/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                             # after 9:35 AM IST -> trader-in/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                  # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader-in/watchlist.json --json
```

Symbols are NSE symbols as you type them (`RELIANCE`, `M&M`, `BAJAJ-AUTO`).
`RELIANCE.BO`, `BSE:RELIANCE` or a 6-digit BSE code (`500325`) means BSE;
`RELIANCE.NS` or `NSE:RELIANCE` is NSE, the default.

Flags: `--min-price 20` (rupees), `--min-turnover` (daily: 5 crore rupees
traded last session, price x volume), `--min-volume` (shares, 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--include-t2t`,
`--bands FILE` (a sec_list.csv), `--fo-list FILE` (NSE's fo_mktlots.csv: the
CAS stocks), `--cas yes|no` (treat every scanned stock as CAS / non-CAS),
`--kite-ltp FILE` (Kite get_ltp / get_quotes output: adds the live price to
each hit), `--kite-bars FILE` (orb, repeatable: Kite candles), `--now "2026-10-19 10:15"` (India time) to pin
the clock, `--offline PATH` to read saved files, `--no-write`, `--show N`,
`--json`. Exit code 0 when the scan found something, 1 when it found nothing
or could not run (the reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/universe.txt` | written by `universe`: one `SYMBOL\|Name\|Series\|Band\|CAS` a line (CAS is yes / no / unknown), read by `daily` and `orb` (symbols, series, price bands, CAS) |
| `trader-in/fo_list.txt` | NSE's F&O stock list (= the CAS stocks), first line `# fetched YYYY-MM-DD`; written by `universe`, refreshed when older than 7 days |
| `trader-in/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `currency` "INR", `hits[]`. Each hit has `symbol` and `exchange` (india-news and india-entry read them), plus `series`, `band`, `prev_close`, `upper_circuit`, `lower_circuit`, `near_upper_circuit`, `cas`, `session_close`, `square_off`, `flat_by_default`, `no_entries_after_default` (india-entry and india-exit read these), `source` (yahoo / kite) and, with `--kite-ltp`, `ltp` and `ltp_source` "kite" |
| `trader-in/config.json` | read by `daily` and `orb`: `no_entries_after` (default 14:45), `cas_symbols_extra`, `non_cas_symbols` |
| `trader-in/holidays.json` | NSE holiday cache, read if present; `orb` fills it from NSE's holiday API when a year is not in the built-in list |

## The universe

`universe` downloads NSE's `EQUITY_L.csv` (every listed equity: SYMBOL, NAME
OF COMPANY, SERIES, ...) and `sec_list.csv` (Symbol, Series, Band) from
nsearchives.nseindia.com, with a browser user agent first (NSE does not
answer some script user agents). It keeps a row when:

1. The series is `EQ` (normal rolling settlement, intraday allowed).
2. Series `BE` and `BZ` are trade-to-trade: every trade must be delivered, so
   no intraday (MIS) at all. They are left out, and counted; `--include-t2t`
   keeps them with their series so a daily scan can show them, marked
   "trade-to-trade: no intraday". `orb` always skips them.
3. With `--index nifty50` or `--index nifty500`, only the members of that
   index (NSE's `ind_nifty50list.csv` / `ind_nifty500list.csv`).

SME stocks (series SM, ST), ETFs and bonds are not in `EQUITY_L.csv`. On the
Oct 23 2026 files: 2,613 rows, 2,359 kept (EQ), 221 BE and 33 BZ left out;
2,141 of the kept have a 2, 5, 10 or 20 % price band and 218 have "No Band"
(stocks with F&O contracts, which have no fixed circuit). All of the NIFTY 50
are "No Band".

## The closing auction (CAS) and each stock's session

Since Aug 3 2026 NSE runs a Closing Auction Session from 15:15 to 15:35 for
stocks that have F&O contracts. For those "CAS stocks" continuous trading ends
at 15:15, and Zerodha auto squares off MIS positions at 15:12 (non-CAS stocks:
trading to 15:30, square-off 15:25; ₹50 + GST per auto-squared order). The
CAS list is the stock rows of NSE's `fo_mktlots.csv` (index rows such as
NIFTY and BANKNIFTY skipped); `universe` fetches it and writes the CAS column.
All of the NIFTY 50 are CAS stocks; on Oct 23 2026 the list had 213 stocks.
Every stock gets its own rules for the day (the same `session_rules` the
whole pack uses):

| | CAS stock | non-CAS stock |
| --- | --- | --- |
| continuous trading ends | 15:15 (72 five-minute bars) | 15:30 (75 bars) |
| Zerodha MIS square-off | 15:12 | 15:25 |
| `flat_by_default` | 15:00 | 15:10 |
| `no_entries_after_default` | 14:45 | 14:45 |

Each watchlist hit carries these so india-entry and india-exit use the right
clock. With no F&O list (offline, no cache) the status is unknown and the
stricter CAS rules apply; the scan says so. Sessions before Aug 3 2026 were 75
bars for every stock. Config `cas_symbols_extra` / `non_cas_symbols` and
`--cas yes|no` override the list.

## Price bands and the upper circuit

A stock with a band cannot trade beyond the previous close x (1 +/- band); at
the limit it is "locked" in circuit and often cannot be bought at all. The
scanner works out both limits on the tick (upper rounded down, lower rounded
up) and marks a hit `near_upper_circuit` when its price is within 1 % of the
upper circuit (printed "NEAR UPPER CIRCUIT"). india-risk blocks a long entry
there. In `daily` the check is the last close against that day's limits (it
closed near or at its upper circuit). The previous close comes from the
previous session's last 5-minute bar (orb on a replay) or Yahoo's previous
close (orb live); NSE's official close is the weighted average of the last
30 minutes, so the limits can be a few ticks off. Bands change: re-run
`universe` every week.

## The daily scan (before the open)

Yahoo's spark endpoint returns 1-day closes for 20 symbols a request, so
2,359 symbols take 118 requests. Liquidity comes from the spark meta: last
price at least `--min-price` (20 rupees) and last session's traded value at
least `--min-turnover` (5 crore rupees). Run it before 9:15 AM or after the
3:30 PM close: during the session the volume is the day's so far, and
today's daily bar is left out until 3:30 PM. The setups use the 1-day bars'
closes, never 5-minute bars (Yahoo leaves some NSE 5-minute bars empty).

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

NSE's normal session is 9:15 AM to 3:30 PM IST (pre-open 9:00 to 9:08). The
opening range is the first `--or-minutes` (15): the 9:15, 9:20 and 9:25
five-minute bars, 9:15 to 9:30. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time. The first possible hit
is the 9:30 bar, done at 9:35 AM.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range (a necessary condition, so no
   breakout is lost).
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (200) candidates,
   the most traded first, and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

A chart is usable when its three range bars are there and at most 3 bars
from the range to the cutoff (or the stock's own close: 15:15 for a CAS
stock, 15:30 otherwise) are empty. For a CAS stock the 15:15 to 15:25 bars are
the closing auction, not continuous trading: they are not counted, and
Yahoo's empty 3:20 PM bar there is expected, not a gap. Other empty bars are
skipped and never end a session. More gaps than that is a halt or a thin stock. A bar off
the 5-minute grid (the live tick) is dropped, and a bar counts only once it is
complete (start + 5 minutes). Before 9:35 AM, on a weekend or NSE holiday,
after the close, or after `no_entries_after` (default 14:45, an expired
setup) it prints one plain line and writes nothing.

Holidays: NSE's 2026 capital-market list is built in (read from NSE on
2026-10-23). Diwali Laxmi Pujan (Sun Nov 8 2026) has only a short Muhurat
session at a time NSE announces; the scanner does not scan it. NSE has not
published 2027 yet: the tool reads `trader-in/holidays.json` and, when a year
is missing, asks NSE's holiday API (with a cookie from nseindia.com) and
caches the answer. If that fails it says so and treats only weekends as
closed.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag (NSE data
  on Yahoo is delayed). Bars are 5-minute bars, never tick by tick. query1
  first, query2 on a 429 or 5xx, one retry after a 5-second wait on a 429.
- Every price line says its source: `(Yahoo, free, can lag)`, `(Kite, live)` or
  `(Kite candles)`. The scanner never calls Kite and always works on free data
  alone; it never fails because Kite is absent.
- Partial results are said as partial: "Fetched 2,300 of 2,359 charts (59
  failed or had no data)". Never round a partial scan up to "the whole market".

## With Zerodha Kite connected

The full-market scan stays on Yahoo: thousands of charts cannot go through
Claude's context, and Kite's data comes one instrument at a time. Kite is for
the shortlist (the exchange's own data, through your broker):

1. Check the login with a cheap read: `get_ltp` for `NSE:RELIANCE`. If it says
   to log in first, call `login` and give the user the link; they sign in on
   Zerodha's own page. Never ask for a password or TOTP.
2. After `scan.py orb`, call `get_ltp` (or `get_quotes`) for the hits, e.g.
   `["NSE:RELIANCE", "NSE:TCS"]`, and save the answer VERBATIM to
   `trader-in/kite/ltp-<date>.json`.
3. Re-run with `--kite-ltp trader-in/kite/ltp-<date>.json --watchlist
   trader-in/watchlist.json`: each hit gets `ltp` with `ltp_source` "kite", and
   the output prints "Last price now (Kite, live): ...". A hit whose live price
   has run far past its breakout bar is stale.
4. To check one chart's bars on Kite's data: `search_instruments` for
   `NSE:SYMBOL` (the instrument token; RELIANCE = 738561), then
   `get_historical_data` for that token, today 09:15 to now, interval
   `5minute`. Save it verbatim to `trader-in/kite/<SYMBOL>-5minute-<date>.json`
   and pass `--kite-bars FILE` (repeatable; chunks are merged by date). Those
   charts skip Yahoo and print "(Kite candles)".

Read-only Kite tools only; orders stay in india-broker. Kite and Yahoo bars
differ a little: on Oct 19 RELIANCE's range was 1,207.10 to 1,223.60 on Kite
(9:45 bar close 1,227.70, 9:15 volume 6,15,358) against 1,207.30 to 1,223.40
on Yahoo (close 1,227.80, 9:15 volume 0). Prefer Kite for the trade itself.

## Steps

1. Once a week or so: `scan.py universe` (or `--index nifty500` for a
   faster, more liquid list).
2. Before 9:15 AM: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup, and any NEAR UPPER CIRCUIT.
3. After 9:35 AM IST (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/india-news SYMBOL` (what is moving
   it: announcements, results), then `/india-entry SYMBOL` (the entry bar by
   bar). Never pick for them.

## Example: Monday Oct 19 2026, the NIFTY 50 at 10:15 AM

`tests/fixtures/india-scanner/nifty50-5m-2026-10-19.json.gz` holds the real
Yahoo 5-minute charts of all 50 NIFTY 50 stocks for that day:

```
python3 scan.py orb --offline nifty50-5m-2026-10-19.json.gz --now "2026-10-19 10:15"
Fetched 50 of 50 charts; 50 usable; 24 candidates, 24 confirmed with real highs and lows.
8 of 50 charts closed above their opening-range high by 10:15 AM:
   9:35 AM  SBIN             ₹978.95  range 960.90 to 977.70  +0.13% over
   9:35 AM  ULTRACEMCO    ₹11,056.00  range 10,941.00 to 11,043.00  +0.12% over
   9:40 AM  HCLTECH        ₹1,396.50  range 1,359.40 to 1,388.00  +0.61% over
   9:40 AM  MARUTI        ₹11,587.00  range 11,450.00 to 11,570.00  +0.15% over
   9:45 AM  TCS            ₹2,489.60  range 2,448.30 to 2,486.80  +0.11% over
   9:50 AM  RELIANCE       ₹1,227.80  range 1,207.30 to 1,223.40  +0.36% over
   9:50 AM  TECHM          ₹1,794.00  range 1,755.20 to 1,792.00  +0.11% over
   9:50 AM  TITAN          ₹4,489.00  range 4,445.10 to 4,488.20  +0.02% over
```

RELIANCE's range is the 9:15, 9:20 and 9:25 bars: high 1,223.40 (the 9:15
bar), low 1,207.30 (the 9:25 bar). The 9:30, 9:35 and 9:40 bars closed
1,215.00, 1,218.80 and 1,221.80, still inside; the 9:45 bar closed 1,227.80,
0.36 % over the high, at 9:50 AM. RELIANCE has "No Band" (it has F&O
contracts), so no circuit flag; it is a CAS stock, so its hit carries
`session_close` 15:15, `square_off` 15:12 and `flat_by_default` 15:00. The same numbers come out of the pack's shared
chart `tests/fixtures/shared/RELIANCE-60d5m.json.gz`. The user picks RELIANCE
and Claude hands it to `/india-news RELIANCE`, then `/india-entry RELIANCE`.

The pre-market scan that morning (40 NIFTY 50 names, closes through Fri Oct
16) tags INFY, BHARTIARTL, ICICIBANK, AXISBANK and HCLTECH a 20-day breakout,
BAJFINANCE and M&M a reclaim of the 20-day average, and MAXHEALTH oversold
(RSI 24.5).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- Say when a hit is trade-to-trade (no intraday) or near its upper circuit.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- No order without your yes: nothing in this pack places an order except india-broker, and only when all
  three hold: india-risk passed that exact order today, the user typed an explicit yes for that one order
  in the chat, and the user clicked Allow once on Claude's own card because the Kite order tool is set to
  "Needs approval". Never set an order tool to "Always allow".
- Long only. No options, short selling or MIS margin (leverage) unless the config allows it and the user confirms.
- Broker keys and Kite sessions live in the Claude connector or environment variables, never in the chat.
- Tax: intraday profit is speculative business income in India. Not tax advice.
